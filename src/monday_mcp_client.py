"""
Thin async wrapper around the official monday.com MCP server
(`@mondaydotcomorg/monday-api-mcp`, spawned via npx over stdio).

Every read the rest of this app does about Monday.com data goes through
this module. Nothing here caches board data across calls — every method
does a live MCP round trip, per the project's no-static-fallback requirement.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_TOOL_TIMEOUT_SECONDS = 30
DEFAULT_CONNECT_TIMEOUT_SECONDS = 45


class MondayMCPError(Exception):
    """Base class for all errors this client raises. Always human-readable."""


class MondayConnectionError(MondayMCPError):
    """The MCP subprocess couldn't be started or the handshake failed."""


class MondayAuthError(MondayMCPError):
    """The API token was rejected by monday.com."""


class MondayTimeoutError(MondayMCPError):
    """An MCP call didn't complete within the allotted time."""


class MondayBoardNotFoundError(MondayMCPError):
    """No board matched the requested name, or the match was ambiguous."""

    def __init__(self, requested_name: str, candidates: list[dict]):
        self.requested_name = requested_name
        self.candidates = candidates
        if candidates:
            listing = ", ".join(f'"{c["title"]}" (id {c["id"]})' for c in candidates[:10])
            msg = (
                f'No board named "{requested_name}" was found. '
                f"Closest matches on your account: {listing}."
            )
        else:
            msg = (
                f'No board named "{requested_name}" was found, and no similarly '
                f"named boards exist on this monday.com account. Confirm the CSV "
                f"has been imported as a board with this name."
            )
        super().__init__(msg)


class MondayAPIError(MondayMCPError):
    """monday.com returned a tool-level error (bad query, rate limit, etc.)."""


@dataclass
class BoardHandle:
    """A resolved board: id + name, so callers never juggle raw dict shapes."""

    id: str
    name: str


def _looks_like_auth_error(text: str) -> bool:
    lowered = text.lower()
    return any(
        needle in lowered
        for needle in ("unauthorized", "invalid token", "401", "not authenticated", "authentication")
    )


def _looks_like_rate_limit(text: str) -> bool:
    lowered = text.lower()
    return "rate limit" in lowered or "429" in lowered or "complexity" in lowered


class MondayMCPClient:
    """
    Async context manager. Usage:

        async with MondayMCPClient() as client:
            board = await client.find_board_by_name("Work Orders")
            schema = await client.get_board_schema(board.id)
            items = await client.get_all_board_items(board.id)

    Raises MondayConnectionError / MondayAuthError / MondayTimeoutError /
    MondayBoardNotFoundError / MondayAPIError — never a raw stack trace's
    worth of MCP/asyncio internals — so callers (the Streamlit app, the
    query engine) can catch one family and show the user a clean message.
    """

    def __init__(
        self,
        api_token: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ):
        self.api_token = api_token or os.environ.get("MONDAY_API_TOKEN")
        if not self.api_token:
            raise MondayConnectionError(
                "MONDAY_API_TOKEN is not set. Add it to your .env file or export it "
                "in the shell before starting the app."
            )
        self.connect_timeout = connect_timeout
        self.tool_timeout = tool_timeout
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "MondayMCPClient":
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@mondaydotcomorg/monday-api-mcp", "-t", self.api_token],
        )
        try:
            read, write = await asyncio.wait_for(
                self._stack.enter_async_context(stdio_client(params)),
                timeout=self.connect_timeout,
            )
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=self.connect_timeout)
        except asyncio.TimeoutError as exc:
            await self._stack.aclose()
            raise MondayTimeoutError(
                f"Timed out connecting to the monday.com MCP server after "
                f"{self.connect_timeout}s. Check your network connection and that "
                f"`npx` / Node.js is installed and reachable."
            ) from exc
        except FileNotFoundError as exc:
            await self._stack.aclose()
            raise MondayConnectionError(
                "Could not launch the monday.com MCP server: `npx` was not found. "
                "Install Node.js 20+ so `npx` is on PATH."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - deliberately broad, re-raised as our type
            await self._stack.aclose()
            raise MondayConnectionError(
                f"Failed to connect to the monday.com MCP server: {exc}"
            ) from exc
        self._session = session
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        if self._session is None:
            raise MondayConnectionError(
                "MondayMCPClient must be used as `async with MondayMCPClient() as client:` "
                "before calling any method."
            )
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments), timeout=self.tool_timeout
            )
        except asyncio.TimeoutError as exc:
            raise MondayTimeoutError(
                f"monday.com didn't respond to `{name}` within {self.tool_timeout}s. "
                f"The board may be too large for one request, or monday.com is slow "
                f"right now — try again or narrow the request."
            ) from exc

        text_parts = [getattr(block, "text", "") for block in result.content]
        raw_text = "\n".join(part for part in text_parts if part)

        if result.is_error:
            if _looks_like_auth_error(raw_text):
                raise MondayAuthError(
                    "monday.com rejected the API token. Generate a fresh personal "
                    "token (Avatar > Developers > My Access Tokens) and update "
                    "MONDAY_API_TOKEN."
                )
            if _looks_like_rate_limit(raw_text):
                raise MondayAPIError(
                    "monday.com's API rate/complexity limit was hit. Wait a moment "
                    "and try again, or narrow the query (fewer columns / smaller date range)."
                )
            raise MondayAPIError(f"monday.com MCP tool `{name}` failed: {raw_text or 'no detail returned'}")

        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MondayAPIError(
                f"monday.com MCP tool `{name}` returned a response we couldn't parse: {raw_text[:300]!r}"
            ) from exc
        return parsed

    async def find_board_by_name(self, name: str) -> BoardHandle:
        """Exact (case/whitespace-insensitive) match against BOARD search results."""
        payload = await self._call_tool("search", {"searchType": "BOARD", "searchTerm": name})
        candidates = payload.get("data", [])
        target = name.strip().casefold()
        for c in candidates:
            if c.get("title", "").strip().casefold() == target:
                return BoardHandle(id=str(c["id"]), name=c["title"])
        raise MondayBoardNotFoundError(name, candidates)

    async def get_board_schema(self, board_id: str) -> dict:
        return await self._call_tool("get_board_schema", {"boardId": int(board_id)})

    async def get_board_info(self, board_id: str) -> dict:
        return await self._call_tool("get_board_info", {"boardId": int(board_id)})

    async def get_all_board_items(
        self,
        board_id: str,
        include_columns: bool = True,
        page_size: int = 500,
    ) -> list[dict]:
        """Follows the MCP tool's own cursor pagination until has_more is false."""
        items: list[dict] = []
        cursor: str | None = None
        while True:
            args: dict[str, Any] = {
                "boardId": int(board_id),
                "limit": page_size,
                "includeColumns": include_columns,
            }
            if cursor:
                args["cursor"] = cursor
            page = await self._call_tool("get_board_items_page", args)
            data = page.get("data", page)
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor:
                break
        return items
