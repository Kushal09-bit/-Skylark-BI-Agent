"""
Turns a founder's natural-language question into a structured QueryPlan:
which board(s), what metric, what time range/filters/grouping, and which of
the four required query shapes it is (single-board lookup, cross-board join,
trend-over-time, sector/segment breakdown).

Board *schema* (column names, what Deal Status "Won"/"Dead" mean) is
documented below as parsing context — this is metadata about structure, not
data. It never changes, so hardcoding it isn't the "cached/static fallback
data" this project forbids; that rule is about answers (row values,
aggregates), and every one of those still requires a live MCP fetch at query
time (see insights.py). Ambiguity about which real values fit an ambiguous
filter (e.g. a sector name the LLM guesses at but doesn't exist) is resolved
downstream in insights.py, against the freshly-fetched data — not here.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from llm_provider import create_tool_call

QUERY_SHAPES = ["single_board_lookup", "cross_board_join", "trend_over_time", "sector_breakdown"]
BOARDS = ["work_orders", "deals"]
METRICS = [
    "revenue_billed", "revenue_collected", "revenue_receivable",
    "pipeline_value", "deal_count", "win_rate", "avg_deal_size",
    "work_order_count", "completion_rate", "overdue_work_orders", "at_risk_work_orders",
    "pipeline_to_delivery_conversion", "sector_breakdown", "owner_breakdown",
    "deal_stage_breakdown", "billing_status_breakdown",
]
GROUP_BY_OPTIONS = ["sector", "month", "quarter", "owner", "deal_stage", "execution_status", "billing_status", None]
TIME_RANGE_RELATIVE_VALUES = [
    "this_quarter", "last_quarter", "this_month", "last_month",
    "this_year", "last_90_days", "last_12_months", "all_time", "custom",
]

SCHEMA_CONTEXT = """
WORK ORDERS board (project execution / delivery):
  Customer Name Code (e.g. WOCOMPANY_002), Serial # (deal reference, e.g. SDPLDEAL-075),
  Nature of Work, Execution Status (Completed / Not Started / Ongoing / Partial Completed /
  Pause or struck / Details pending from Client), Sector (Mining/Powerline/Renewables/
  Railways/Construction/Others), Type of Work (can be multiple, comma-joined),
  BD/KAM Personnel code (OWNER_NNN), several date fields (Data Delivery Date, Date of
  PO/LOI, Probable Start/End Date, Last invoice date), money fields in INR (Amount
  Excl/Incl of GST, Billed Value, Collected Amount, Amount Receivable, Amount to be
  billed), Invoice Status, WO Status (billed): Open/Closed, Billing Status.
  "At-risk/overdue" is not a literal column — it means Probable End Date has passed
  AND Execution Status isn't Completed/Partial Completed AND WO Status isn't Closed.

DEALS board (sales pipeline):
  Owner code (OWNER_NNN — same namespace as Work Orders' BD/KAM code), Client Code
  (COMPANYNNN — joins to Work Orders' Customer Name Code by numeric suffix, e.g.
  WOCOMPANY_002 <-> COMPANY002), Deal Status: Open / Won / Dead / On Hold (Won =
  closed-won, Dead = lost — this is the real win/loss field), Deal Stage: a mostly-
  lettered funnel from "A. Lead Generated" through "H. Work Order Received" to
  "K. Amount Accrued", with side states "L. Project Lost", "M. Projects On Hold",
  "N./O. Not relevant", Closure Probability (High/Medium/Low, often null), Masked
  Deal value (INR, null on over half of records), Sector/service, Tentative Close
  Date, Close Date (A) (actual), Created Date.

Cross-board correlation goes through the company-code join above (or the shared
OWNER_NNN code for rep-level questions), since there's no direct deal-to-work-order
foreign key.
"""

SYSTEM_PROMPT = f"""You turn a founder's business question about their company's Monday.com \
data into a structured query plan. You never invent numbers — you only decide *what to look up*.

{SCHEMA_CONTEXT}

Today's date is {{today}}.

Query shapes:
- single_board_lookup: a fact from one board (e.g. "how many open deals do we have")
- cross_board_join: needs both boards correlated via company code or owner code
  (e.g. "which closed deals have at-risk work orders", "pipeline to delivery conversion")
- trend_over_time: a metric over a time series, grouped by month/quarter
- sector_breakdown: a metric broken down by a category (sector, owner, stage, status)

Rules:
- If the question has nothing to do with Work Orders or Deals data (e.g. small
  talk, a question about an unrelated topic), set clarification_question to a
  short redirect explaining what you can help with instead of forcing it into
  one of the metrics below.
- Only ask a clarifying question (set clarification_question) when the request is
  genuinely unresolvable without guessing — e.g. a metric word that could mean two
  different things with materially different answers, or a time range that is core
  to the question and has no sensible default. When you do ask, ask exactly ONE
  focused question about the one thing you're unsure of — but STILL fill in every
  other field you ARE confident about (e.g. if the sector is clear but the metric
  isn't, set sector_filter and leave metric/shape unset). The next turn's answer to
  your question needs those fields to merge onto, or context gets silently dropped.
- Do NOT ask for things with a reasonable default. If no time range is given: use
  "all_time" for a snapshot/count/breakdown question, and "last_12_months" for a
  trend question. State the default you picked in `assumptions`.
- If the user's message is a follow-up to the previous plan (shown below, if any) —
  e.g. "now just Q3", "break that down by month", "what about Renewables", or a
  short direct answer to your own clarifying question (e.g. previous plan asked
  which metric and the user replies "Billed revenue") — start from that plan and
  only change what the user actually asked to change. Don't re-derive fields the
  user didn't mention changing, and don't drop a filter (like sector_filter) just
  because the new message doesn't repeat it.
- sector_filter / status_filter / owner_filter should be your best-effort guess at
  the real label (e.g. "renewables" -> "Renewables"). Exact casing doesn't matter;
  downstream matching is case-insensitive. If nothing like it plausibly exists on
  either board, leave it as given — the execution layer will check against live
  data and raise its own clarifying question if there's truly no match.
"""

# NOTE: boards/shape/metric/group_by/time_range_relative are deliberately NOT
# constrained with JSON-schema `enum` here. Groq validates tool-call arguments
# against the schema server-side and rejects the whole call on a mismatch
# (verified live: gpt-oss-120b reliably emitted "Work Orders"/"DEALS" instead
# of "work_orders"/"deals" and Groq 400'd before this code ever saw the
# response). Anthropic doesn't enforce enums as strictly, so a fixed enum
# worked there — but the fix needs to hold across providers, so validation
# happens in Python (_normalize_enum below) instead of relying on the API to
# reject bad casing. The valid values are still spelled out in each
# description so the model has the same guidance either way.
TOOL_SCHEMA = {
    "name": "emit_query_plan",
    "description": "Emit the structured query plan for the founder's question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "boards": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": f"Subset of exactly these strings (lowercase, underscored): {BOARDS}",
            },
            "shape": {"type": ["string", "null"], "description": f"Exactly one of: {QUERY_SHAPES}"},
            "metric": {"type": ["string", "null"], "description": f"Exactly one of: {METRICS}"},
            "time_range_relative": {
                "type": ["string", "null"],
                "description": f"Exactly one of: {TIME_RANGE_RELATIVE_VALUES}",
            },
            "time_range_start": {"type": ["string", "null"], "description": "ISO date, only if time_range_relative is custom"},
            "time_range_end": {"type": ["string", "null"], "description": "ISO date, only if time_range_relative is custom"},
            "sector_filter": {"type": ["string", "null"]},
            "status_filter": {"type": ["string", "null"]},
            "owner_filter": {"type": ["string", "null"]},
            "group_by": {
                "type": ["string", "null"],
                "description": f"Exactly one of: {[g for g in GROUP_BY_OPTIONS if g]}, or null",
            },
            "clarification_question": {"type": ["string", "null"]},
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["assumptions"],
    },
}


def _normalize_enum(value: str | None, valid_values: list[str]) -> str | None:
    """Case/spacing-tolerant match against a fixed vocabulary — e.g. "Work
    Orders" or "WORK ORDERS" both resolve to "work_orders". Returns None
    (treated as unset, not an error) if nothing matches, since downstream
    code already has safe defaults/fallbacks for an unset board/metric."""
    if value is None:
        return None
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in valid_values else None


@dataclass
class TimeRange:
    start: date | None
    end: date | None
    label: str


@dataclass
class QueryPlan:
    raw_question: str
    boards: list[str]
    shape: str
    metric: str
    time_range: TimeRange
    sector_filter: str | None = None
    status_filter: str | None = None
    owner_filter: str | None = None
    group_by: str | None = None
    clarification_question: str | None = None
    assumptions: list[str] = field(default_factory=list)

    def needs_clarification(self) -> bool:
        return self.clarification_question is not None


def _quarter_bounds(d: date, quarters_back: int = 0) -> tuple[date, date]:
    q = (d.month - 1) // 3
    q -= quarters_back
    year = d.year + (q // 4)
    q = q % 4
    start_month = q * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end_day = calendar.monthrange(year, end_month)[1]
    end = date(year, end_month, end_day)
    return start, end


def resolve_time_range(relative: str, start: str | None, end: str | None, today: date | None = None) -> TimeRange:
    """Converts a relative time phrase into concrete start/end dates using the
    real current date — never a hardcoded "today"."""
    today = today or date.today()
    if relative == "custom" and start and end:
        return TimeRange(date.fromisoformat(start), date.fromisoformat(end), f"{start} to {end}")
    if relative == "this_quarter":
        s, e = _quarter_bounds(today, 0)
        return TimeRange(s, e, f"this quarter ({s.isoformat()} to {e.isoformat()})")
    if relative == "last_quarter":
        s, e = _quarter_bounds(today, 1)
        return TimeRange(s, e, f"last quarter ({s.isoformat()} to {e.isoformat()})")
    if relative == "this_month":
        s = today.replace(day=1)
        e = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        return TimeRange(s, e, f"this month ({s.isoformat()} to {e.isoformat()})")
    if relative == "last_month":
        first_of_this = today.replace(day=1)
        last_month_end = first_of_this - timedelta(days=1)
        s = last_month_end.replace(day=1)
        return TimeRange(s, last_month_end, f"last month ({s.isoformat()} to {last_month_end.isoformat()})")
    if relative == "this_year":
        return TimeRange(date(today.year, 1, 1), date(today.year, 12, 31), f"{today.year}")
    if relative == "last_90_days":
        return TimeRange(today - timedelta(days=90), today, "last 90 days")
    if relative == "last_12_months":
        return TimeRange(today - timedelta(days=365), today, "last 12 months")
    return TimeRange(None, None, "all time")


def parse_query(
    question: str,
    previous_plan: QueryPlan | None = None,
    today: date | None = None,
) -> QueryPlan:
    today = today or date.today()

    previous_plan_note = "None — this is a new question." if previous_plan is None else json.dumps({
        "boards": previous_plan.boards, "shape": previous_plan.shape, "metric": previous_plan.metric,
        "time_range_label": previous_plan.time_range.label, "sector_filter": previous_plan.sector_filter,
        "status_filter": previous_plan.status_filter, "owner_filter": previous_plan.owner_filter,
        "group_by": previous_plan.group_by,
    })

    plan_input = create_tool_call(
        system=SYSTEM_PROMPT.format(today=today.isoformat()),
        user_content=f"Previous query plan: {previous_plan_note}\n\nFounder's question: {question}",
        tool_name=TOOL_SCHEMA["name"],
        tool_description=TOOL_SCHEMA["description"],
        input_schema=TOOL_SCHEMA["input_schema"],
    )

    # Fields are normalized the same way whether or not a clarification is
    # also being asked — a clarifying question about the metric shouldn't
    # discard a sector the model was already confident about, or the next
    # turn's answer has nothing correct to merge onto (a live bug: "Renewables"
    # then "Billed revenue" lost the sector entirely and answered for all
    # sectors, because the clarification path used to blank every other field).
    relative = _normalize_enum(plan_input.get("time_range_relative"), TIME_RANGE_RELATIVE_VALUES) or "all_time"
    time_range = resolve_time_range(
        relative,
        plan_input.get("time_range_start"),
        plan_input.get("time_range_end"),
        today,
    )

    raw_boards = plan_input.get("boards") or []
    boards = [b for raw in raw_boards if (b := _normalize_enum(raw, BOARDS)) is not None]

    return QueryPlan(
        raw_question=question,
        boards=boards,
        shape=_normalize_enum(plan_input.get("shape"), QUERY_SHAPES) or "single_board_lookup",
        metric=_normalize_enum(plan_input.get("metric"), METRICS) or "",
        time_range=time_range,
        sector_filter=plan_input.get("sector_filter"),
        status_filter=plan_input.get("status_filter"),
        owner_filter=plan_input.get("owner_filter"),
        group_by=_normalize_enum(plan_input.get("group_by"), [g for g in GROUP_BY_OPTIONS if g]),
        clarification_question=plan_input.get("clarification_question"),
        assumptions=plan_input.get("assumptions", []),
    )
