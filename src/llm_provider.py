"""
Pluggable LLM provider for query parsing and narration — Anthropic (Claude)
or Groq, behind one interface, so the rest of the app doesn't care which is
configured.

This exists because of a real, mid-build constraint: this project started
against Anthropic, then switched to Groq when a paid Anthropic account
wasn't available (see DECISION_LOG.md). Routing every LLM call through this
module means that swap — or any future one — is a one-line config change
(set LLM_PROVIDER, or just set the matching API key), not a rewrite of
query_engine.py / agent.py / leadership_update.py.

Groq's API is OpenAI-style function calling; Anthropic's is its own tool-use
shape. Both are adapted here to the same two functions: create_tool_call
(forced structured output) and create_text (narration). Provider-specific
errors are re-raised as LLMProviderError so callers only need one except
clause regardless of which provider is active.
"""

from __future__ import annotations

import json
import os

ANTHROPIC_MODEL = "claude-sonnet-5"
# openai/gpt-oss-120b is a reasoning model on Groq: reasoning_effort="low" +
# include_reasoning=False keeps it fast and cheap for structured
# extraction/narration (verified live — default effort burned ~90% of the
# token budget on an internal reasoning trace for a trivial prompt).
GROQ_MODEL = "openai/gpt-oss-120b"


class LLMProviderError(Exception):
    """Unified error for any LLM call, regardless of provider."""


def resolve_provider() -> str:
    explicit = os.environ.get("LLM_PROVIDER")
    if explicit:
        return explicit.lower()
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "anthropic"


def _api_key_for(provider: str) -> str:
    env_var = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    key = os.environ.get(env_var)
    if not key:
        raise LLMProviderError(f"{env_var} is not set (required for LLM_PROVIDER={provider}).")
    return key


def create_tool_call(system: str, user_content: str, tool_name: str, tool_description: str, input_schema: dict) -> dict:
    """Forces a call to the named tool; returns its parsed arguments dict."""
    provider = resolve_provider()
    key = _api_key_for(provider)
    try:
        if provider == "groq":
            from groq import APIError as GroqAPIError
            from groq import Groq

            client = Groq(api_key=key)
            tool = {
                "type": "function",
                "function": {"name": tool_name, "description": tool_description, "parameters": input_schema},
            }
            try:
                resp = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                    tools=[tool],
                    tool_choice={"type": "function", "function": {"name": tool_name}},
                    reasoning_effort="low",
                    include_reasoning=False,
                    max_tokens=1500,
                )
            except GroqAPIError as exc:
                raise LLMProviderError(f"Groq API error: {exc}") from exc
            tool_calls = resp.choices[0].message.tool_calls
            if not tool_calls:
                raise LLMProviderError("Groq did not return the expected tool call.")
            return json.loads(tool_calls[0].function.arguments)

        else:
            import anthropic

            client = anthropic.Anthropic(api_key=key)
            tool = {"name": tool_name, "description": tool_description, "input_schema": input_schema}
            try:
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=[{"role": "user", "content": user_content}],
                )
            except anthropic.APIError as exc:
                raise LLMProviderError(f"Anthropic API error: {exc}") from exc
            try:
                tool_use = next(b for b in resp.content if b.type == "tool_use")
            except StopIteration:
                raise LLMProviderError("Anthropic did not return the expected tool call.") from None
            return tool_use.input
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        raise LLMProviderError(f"Couldn't parse the {provider} response: {exc}") from exc


def create_text(system: str, messages: list[dict], max_tokens: int = 800) -> str:
    provider = resolve_provider()
    key = _api_key_for(provider)
    if provider == "groq":
        from groq import APIError as GroqAPIError
        from groq import Groq

        client = Groq(api_key=key)
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system}] + messages,
                reasoning_effort="low",
                include_reasoning=False,
                max_tokens=max_tokens,
            )
        except GroqAPIError as exc:
            raise LLMProviderError(f"Groq API error: {exc}") from exc
        return resp.choices[0].message.content or ""
    else:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        try:
            resp = client.messages.create(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system, messages=messages)
        except anthropic.APIError as exc:
            raise LLMProviderError(f"Anthropic API error: {exc}") from exc
        return "".join(b.text for b in resp.content if b.type == "text")
