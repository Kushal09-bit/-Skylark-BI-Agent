"""
Conversational orchestrator: parses a founder's question, runs the real
Monday.com query it implies, and phrases the answer — the LLM narrator is
only ever handed numbers this app already computed from live data, so it
can't invent figures. Multi-turn context lives in ConversationState, so a
follow-up like "now just Q3" or "break that down by month" reuses the
previous turn's plan instead of the user re-stating the whole question.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import date

import insights as I
from data_normalize import label_key
from llm_provider import LLMProviderError, create_text
from monday_mcp_client import (
    MondayAPIError,
    MondayAuthError,
    MondayBoardNotFoundError,
    MondayConnectionError,
    MondayMCPClient,
    MondayTimeoutError,
)
from query_engine import QueryPlan, parse_query


class NeedsClarification(Exception):
    """Raised when a named filter (sector/status/owner) doesn't match anything
    in the live data — carries the ONE clarifying question to ask back."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


@dataclass
class ConversationState:
    history: list[dict] = field(default_factory=list)
    last_plan: QueryPlan | None = None


def _friendly_mcp_error(exc: Exception) -> str:
    if isinstance(exc, MondayAuthError):
        return f"⚠️ Monday.com rejected the connection: {exc}"
    if isinstance(exc, MondayBoardNotFoundError):
        return f"⚠️ {exc}"
    if isinstance(exc, MondayTimeoutError):
        return f"⚠️ Monday.com took too long to respond: {exc}"
    if isinstance(exc, MondayConnectionError):
        return f"⚠️ Couldn't connect to Monday.com: {exc}"
    if isinstance(exc, MondayAPIError):
        return f"⚠️ Monday.com returned an error: {exc}"
    return f"⚠️ Something went wrong talking to Monday.com: {exc}"


def _filtered(board: I.BoardData, field_name: str, wanted: str) -> I.BoardData:
    matches, ok, available = _match_label(board, field_name, wanted)
    if not ok:
        pretty_field = field_name.replace("_", " ")
        options = ", ".join(available) if available else "(no values found)"
        raise NeedsClarification(
            f"I don't see a {pretty_field} matching \"{wanted}\" on the {board.name} board. "
            f"The values I do see are: {options}. Which did you mean?"
        )
    return dataclasses.replace(board, items=matches)


def _match_label(board: I.BoardData, field_name: str, wanted: str) -> tuple[list[dict], bool, list[str]]:
    from data_normalize import normalize_label

    wanted_key = label_key(wanted) or ""
    available: set[str] = set()
    matches = []
    for item in board.items:
        raw = board.get(item, field_name)
        label = normalize_label(raw)
        if label is None:
            continue
        available.add(label)
        key = label_key(raw) or ""
        if wanted_key and (wanted_key in key or key in wanted_key):
            matches.append(item)
    return matches, len(matches) > 0, sorted(available)


# Safety net alongside plan.boards: some metrics/group_by values unambiguously
# require a specific board regardless of what the LLM's `boards` field said,
# so a parsing slip can't silently skip a fetch and crash (or worse, silently
# compute against an empty board — see _GROUP_BY_BOARDS below, added after a
# live bug where group_by wasn't accounted for here).
_METRIC_BOARDS = {
    "revenue_billed": {"work_orders"}, "revenue_collected": {"work_orders"},
    "revenue_receivable": {"work_orders"}, "pipeline_value": {"deals"},
    "deal_count": {"deals"}, "win_rate": {"deals"}, "avg_deal_size": {"deals"},
    "work_order_count": {"work_orders"}, "completion_rate": {"work_orders"},
    "overdue_work_orders": {"work_orders"}, "at_risk_work_orders": {"work_orders"},
    "pipeline_to_delivery_conversion": {"work_orders", "deals"},
    "sector_breakdown": {"work_orders", "deals"},
    "owner_breakdown": {"deals"}, "deal_stage_breakdown": {"deals"},
    "billing_status_breakdown": {"work_orders"},
}
_GROUP_BY_BOARDS = {
    "sector": {"work_orders", "deals"}, "owner": {"deals"}, "deal_stage": {"deals"},
    "billing_status": {"work_orders"}, "execution_status": {"work_orders"},
    "month": {"deals"}, "quarter": {"deals"},
}


async def _load_boards(plan: QueryPlan, client: MondayMCPClient) -> dict[str, I.BoardData]:
    boards: dict[str, I.BoardData] = {}
    required = _METRIC_BOARDS.get(plan.metric, set()) | _GROUP_BY_BOARDS.get(plan.group_by, set())
    need_wo = "work_orders" in plan.boards or plan.shape == "cross_board_join" or "work_orders" in required
    need_deals = "deals" in plan.boards or plan.shape == "cross_board_join" or "deals" in required
    if need_wo:
        boards["work_orders"] = await I.load_board(client, "Work Orders", I.WORK_ORDER_FIELD_TITLES)
    if need_deals:
        boards["deals"] = await I.load_board(client, "Deals", I.DEALS_FIELD_TITLES)
    return boards


def _apply_common_filters(plan: QueryPlan, boards: dict[str, I.BoardData]) -> dict[str, I.BoardData]:
    out = dict(boards)
    if plan.sector_filter:
        if "work_orders" in out:
            out["work_orders"] = _filtered(out["work_orders"], "sector", plan.sector_filter)
        if "deals" in out:
            out["deals"] = _filtered(out["deals"], "sector", plan.sector_filter)
    if plan.owner_filter:
        if "work_orders" in out:
            out["work_orders"] = _filtered(out["work_orders"], "owner_code", plan.owner_filter)
        if "deals" in out:
            out["deals"] = _filtered(out["deals"], "owner_code", plan.owner_filter)
    if plan.status_filter and "deals" in out and "work_orders" not in out:
        out["deals"] = _filtered(out["deals"], "deal_status", plan.status_filter)
    if plan.status_filter and "work_orders" in out and "deals" not in out:
        out["work_orders"] = _filtered(out["work_orders"], "execution_status", plan.status_filter)
    return out


def _compute(plan: QueryPlan, boards: dict[str, I.BoardData], today: date) -> dict:
    wo = boards.get("work_orders") or _empty_board("work_orders")
    deals = boards.get("deals") or _empty_board("deals")
    start, end = plan.time_range.start, plan.time_range.end

    # `group_by` is checked FIRST and wins over `metric`/`shape` — it's the
    # most specific signal for a "breakdown by X" question. A live bug showed
    # why this matters: "breakdown of work orders by billing status" parsed
    # to shape=sector_breakdown, metric=work_order_count, group_by=
    # billing_status — checking shape/metric first would have silently
    # returned a plain work-order count instead of the requested breakdown.
    group_dispatch = {
        "sector": lambda: I.sector_breakdown(wo, deals),
        "owner": lambda: I.breakdown_by_field(deals, "owner_code", "deal_value"),
        "deal_stage": lambda: I.breakdown_by_field(deals, "deal_stage"),
        "billing_status": lambda: I.breakdown_by_field(wo, "billing_status"),
        "execution_status": lambda: I.breakdown_by_field(wo, "execution_status"),
    }
    if plan.group_by in group_dispatch:
        return group_dispatch[plan.group_by]()

    if plan.shape == "trend_over_time" or plan.group_by in ("month", "quarter"):
        if boards.get("deals") is not None:
            value_field = "deal_value" if plan.metric in ("pipeline_value", "revenue_billed") else None
            return I.trend_over_time(deals, "created_date", value_field, "month")
        return I.trend_over_time(wo, "probable_end_date", "billed_incl_gst", "month")

    if plan.metric == "sector_breakdown" or plan.shape == "sector_breakdown":
        return I.sector_breakdown(wo, deals)

    if plan.metric == "owner_breakdown":
        return I.breakdown_by_field(deals, "owner_code", "deal_value")

    if plan.metric == "deal_stage_breakdown":
        return I.breakdown_by_field(deals, "deal_stage")

    if plan.metric == "billing_status_breakdown":
        return I.breakdown_by_field(wo, "billing_status")

    if plan.shape == "cross_board_join" or plan.metric == "pipeline_to_delivery_conversion":
        return I.cross_board_conversion(deals, wo, today)

    if plan.metric in ("pipeline_value", "deal_count", "win_rate", "avg_deal_size"):
        return I.pipeline_health(deals, start, end)

    if plan.metric in ("completion_rate", "overdue_work_orders", "at_risk_work_orders", "work_order_count"):
        return I.operational_metrics(wo, today)

    # revenue_billed / revenue_collected / revenue_receivable and anything else default here
    return I.revenue_summary(wo, start, end)


def _empty_board(name: str) -> I.BoardData:
    return I.BoardData(name=name, id="", items=[], field_ids={}, artifact_excluded=0, total_raw=0)


NARRATOR_SYSTEM_PROMPT = """You are a business intelligence analyst answering a founder's question about \
their company. You have ALREADY been given the exact, correct numbers computed from live data below — \
never invent, round unusually, or add any number that isn't in the data provided. Your job is only to \
phrase a clear, natural answer:
- Lead with the direct answer to the question.
- Include interpretation: trend direction, a comparison, or a one-line "so what" — not just the number.
- If `disclosures` is non-empty, mention what was excluded and why, in plain language, near the top or end.
- Money values are in INR (masked/anonymized amounts) — format large ones naturally (e.g. "₹12.7 Cr").
- Keep it tight: a founder wants the answer, not a report. A few sentences, not bullet-point walls, unless
  the data is inherently a breakdown/list.
- Never mention column ids, internal function names, or that you were "given" this data — just answer.
- Any list field with a companion "..._note" field is a truncated sample, not the full population — never
  count, categorize, or state a breakdown derived from it; use only explicit count/total fields for numbers.
"""


def _looks_consistent(answer: str, computed: dict) -> bool:
    """Cheap tripwire, not a full fact-check: if `computed` has a scalar
    `interpretation` string containing numbers, at least one of those numbers
    should appear in the narrated answer. Exists because live testing found
    the narration model (a free/open-weight model, not a frontier one) can
    rarely restate a correct number wrong — e.g. asked to phrase "68%
    completion rate, 45 at-risk", it once answered "98%, none at-risk". The
    underlying computation was never wrong; this catches the rare case where
    phrasing it went wrong, so a retry (and a guaranteed-correct fallback
    below) can recover instead of showing the user a wrong number."""
    interpretation = computed.get("interpretation")
    if not isinstance(interpretation, str):
        return True
    numbers = re.findall(r"\d+", interpretation)
    if not numbers:
        return True
    return any(n in answer for n in numbers)


def _fallback_answer(computed: dict) -> str:
    """Guaranteed-correct (if less naturally phrased) answer built directly
    from computed data, used only if narration fails the consistency check
    twice in a row."""
    parts = [str(computed.get("interpretation", ""))] if computed.get("interpretation") else []
    disclosures = computed.get("disclosures") or []
    if disclosures:
        parts.append("Note: " + "; ".join(disclosures) + ".")
    return "\n\n".join(parts) if parts else str(computed)


_MAX_LIST_ITEMS_FOR_NARRATION = 15


def _trim_for_narration(computed: dict) -> dict:
    """Caps any long list before it reaches the narrator. Exists because a
    live bug showed the model will otherwise infer a wrong sub-breakdown by
    counting a partial list itself (e.g. stated "10 Update Required, 5
    Stuck" from a 10-item sample whose true full-population split was 12/3)
    — trimming here, paired with the prompt rule below, keeps any list the
    model sees small enough that it reads as illustrative, not exhaustive."""
    trimmed = dict(computed)
    for key, value in computed.items():
        if isinstance(value, list) and len(value) > _MAX_LIST_ITEMS_FOR_NARRATION:
            trimmed[key] = value[:_MAX_LIST_ITEMS_FOR_NARRATION]
            trimmed[f"{key}_note"] = (
                f"showing {_MAX_LIST_ITEMS_FOR_NARRATION} of {len(value)} — this is a sample, "
                f"not the full list; don't count or categorize from it"
            )
    return trimmed


def _narrate(question: str, plan: QueryPlan, computed: dict, history: list[dict]) -> str:
    context_note = ""
    if plan.assumptions:
        context_note = f"\n(Assumptions applied: {'; '.join(plan.assumptions)})"

    messages = list(history[-6:]) + [{
        "role": "user",
        "content": (
            f"Founder's question: {question}{context_note}\n\n"
            f"Computed data (ground truth — use exactly these numbers):\n{_trim_for_narration(computed)}"
        ),
    }]
    return create_text(NARRATOR_SYSTEM_PROMPT, messages, max_tokens=800)


async def ask(question: str, state: ConversationState, monday_token: str) -> str:
    """Answers one question, mutating `state` with the new turn. Never raises —
    every failure mode is caught and turned into a clear, human-readable message."""
    today = date.today()
    try:
        plan = parse_query(question, previous_plan=state.last_plan, today=today)
    except LLMProviderError as exc:
        return f"⚠️ Couldn't reach the language model to understand that question: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Unexpected error while parsing your question: {exc}"

    if plan.needs_clarification():
        state.history.append({"role": "user", "content": question})
        state.history.append({"role": "assistant", "content": plan.clarification_question})
        state.last_plan = plan  # preserves whatever fields the model WAS confident about
        return plan.clarification_question

    try:
        async with MondayMCPClient(api_token=monday_token) as client:
            boards = await _load_boards(plan, client)
    except (MondayConnectionError, MondayAuthError, MondayTimeoutError,
            MondayBoardNotFoundError, MondayAPIError) as exc:
        return _friendly_mcp_error(exc)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Unexpected error reading from Monday.com: {exc}"

    try:
        boards = _apply_common_filters(plan, boards)
        computed = _compute(plan, boards, today)
    except NeedsClarification as exc:
        state.history.append({"role": "user", "content": question})
        state.history.append({"role": "assistant", "content": exc.question})
        state.last_plan = plan  # e.g. the sector was recognized, only the value didn't match live data
        return exc.question
    except I.InsightsSchemaError as exc:
        return f"⚠️ {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Unexpected error computing that answer: {exc}"

    try:
        answer = _narrate(question, plan, computed, state.history)
        if not _looks_consistent(answer, computed):
            answer = _narrate(question, plan, computed, state.history)  # one retry
            if not _looks_consistent(answer, computed):
                answer = _fallback_answer(computed)  # guaranteed-correct, less natural phrasing
    except LLMProviderError as exc:
        return f"⚠️ Got the numbers but couldn't phrase the answer (language model error): {exc}\n\nRaw result: {computed}"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Got the numbers but hit an unexpected error phrasing the answer: {exc}\n\nRaw result: {computed}"

    state.history.append({"role": "user", "content": question})
    state.history.append({"role": "assistant", "content": answer})
    state.last_plan = plan
    return answer
