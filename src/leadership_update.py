"""
Leadership update generation.

INTERPRETATION (also logged in DECISION_LOG.md, since this requirement was
intentionally open-ended): a "leadership update" here means a periodic
(default: trailing 30 days) executive brief with four fixed sections, each
grounded in a live computation, not a free-form summary:

  1. Pipeline movement  — deals created and deals decided (Won/Dead) in the
     period, by count and value. We do NOT have stage-change history without
     parsing monday.com's activity log in detail (get_board_activity), which
     was out of scope for the time budget — "movement" here means net new
     activity in the window, not a stage-by-stage funnel diff.
  2. At-risk / overdue  — work orders past their Probable End Date and not
     closed (see insights.py's _is_at_risk), PLUS "stalled handoffs": deals
     Won more than 30 days ago with no matching work order yet.
  3. Notable wins        — the highest-value deals Won in the period.
  4. Operational highlights — completion rate and work orders whose Billing
     Status needs attention (Update Required / Stuck).

Like every other answer in this app, this pulls live from both boards on
every call — there is no cached "last update".
"""

from __future__ import annotations

from datetime import date, timedelta

import insights as I
from data_normalize import company_key, label_key, normalize_label, parse_date, to_number
from llm_provider import LLMProviderError, create_text
from monday_mcp_client import MondayMCPClient

STALLED_HANDOFF_DAYS = 30


def _pipeline_movement(deals: I.BoardData, start: date, end: date) -> dict:
    created, _ = I.filter_by_date_field(deals, "created_date", start, end)
    decided_in_period = []
    for item in deals.items:
        status = label_key(deals.get(item, "deal_status"))
        if status not in (label_key("Won"), label_key("Dead")):
            continue
        close = parse_date(deals.get(item, "close_date_actual")) or parse_date(deals.get(item, "tentative_close_date"))
        if close and start <= close <= end:
            decided_in_period.append(item)

    won = [i for i in decided_in_period if label_key(deals.get(i, "deal_status")) == label_key("Won")]
    dead = [i for i in decided_in_period if label_key(deals.get(i, "deal_status")) == label_key("Dead")]
    won_value = sum(v for i in won if (v := to_number(deals.get(i, "deal_value"))) is not None)

    return {
        "new_deals_created": len(created),
        "deals_won": len(won),
        "deals_lost": len(dead),
        "won_value_total": won_value,
    }


def _stalled_handoffs(deals: I.BoardData, wo: I.BoardData, today: date) -> list[dict]:
    wo_companies = {company_key(wo.get(i, "customer_code")) for i in wo.items}
    wo_companies.discard(None)
    stalled = []
    for item in deals.items:
        if label_key(deals.get(item, "deal_status")) != label_key("Won"):
            continue
        close = parse_date(deals.get(item, "close_date_actual")) or parse_date(deals.get(item, "tentative_close_date"))
        if close is None or (today - close).days < STALLED_HANDOFF_DAYS:
            continue
        key = company_key(deals.get(item, "client_code"))
        if key is not None and key not in wo_companies:
            stalled.append({
                "client_code": deals.get(item, "client_code"),
                "won_days_ago": (today - close).days,
            })
    return stalled


def _notable_wins(deals: I.BoardData, start: date, end: date, top_n: int = 5) -> list[dict]:
    won_in_period = []
    for item in deals.items:
        if label_key(deals.get(item, "deal_status")) != label_key("Won"):
            continue
        close = parse_date(deals.get(item, "close_date_actual")) or parse_date(deals.get(item, "tentative_close_date"))
        if close and start <= close <= end:
            value = to_number(deals.get(item, "deal_value"))
            won_in_period.append({"client_code": deals.get(item, "client_code"), "value": value, "close_date": close.isoformat()})
    won_in_period.sort(key=lambda d: d["value"] or 0, reverse=True)
    return won_in_period[:top_n]


async def compute_leadership_data(monday_token: str, period_days: int = 30, today: date | None = None) -> dict:
    today = today or date.today()
    start = today - timedelta(days=period_days)

    async with MondayMCPClient(api_token=monday_token) as client:
        wo = await I.load_board(client, "Work Orders", I.WORK_ORDER_FIELD_TITLES)
        deals = await I.load_board(client, "Deals", I.DEALS_FIELD_TITLES)

    movement = _pipeline_movement(deals, start, today)
    ops = I.operational_metrics(wo, today)
    stalled = sorted(_stalled_handoffs(deals, wo, today), key=lambda s: s["won_days_ago"], reverse=True)
    wins = _notable_wins(deals, start, today)

    billing_attention = [
        {"serial_no": wo.get(i, "serial_no"), "status": normalize_label(wo.get(i, "billing_status"))}
        for i in wo.items
        if label_key(wo.get(i, "billing_status")) in (label_key("Update Required"), label_key("Stuck"))
    ]
    # Computed from the FULL list, not the truncated sample below — a live bug
    # showed the narrator will otherwise infer (wrong) category counts from
    # whatever 10-item sample it's shown and state them as if authoritative.
    billing_attention_by_status: dict[str, int] = {}
    for entry in billing_attention:
        billing_attention_by_status[entry["status"]] = billing_attention_by_status.get(entry["status"], 0) + 1

    return {
        "period": f"{start.isoformat()} to {today.isoformat()}",
        "pipeline_movement": movement,
        "at_risk_work_orders": {
            "count": ops["at_risk_count"],
            "items": ops["at_risk_items"][:10],
            "definition": ops["at_risk_definition"],
        },
        "stalled_handoffs": {
            "total_count": len(stalled),
            "definition": (
                f"A Won deal with no matching (by company code) work order at least "
                f"{STALLED_HANDOFF_DAYS} days after close — a possible dropped handoff "
                f"from sales to delivery, not a literal monday.com status."
            ),
            "most_overdue_examples": stalled[:10],
        },
        "notable_wins": wins,
        "operational_highlights": {
            "completion_rate": ops["completion_rate"],
            "status_counts": ops["status_counts"],
            "billing_needs_attention_by_status": billing_attention_by_status,
            "billing_needs_attention_examples": billing_attention[:10],
            "billing_needs_attention_total": len(billing_attention),
        },
        "disclosures": ops["disclosures"] + (
            [f"{wo.artifact_excluded} malformed Work Orders rows excluded"] if wo.artifact_excluded else []
        ) + (
            [f"{deals.artifact_excluded} malformed Deals rows excluded"] if deals.artifact_excluded else []
        ),
    }


NARRATOR_SYSTEM_PROMPT = """You write a concise leadership update for a founder/executive team, from \
data you're given (already computed from live Monday.com data — never invent numbers). Structure it with \
these four headed sections, in this order: Pipeline Movement, At-Risk / Overdue, Notable Wins, Operational \
Highlights. Under each, 2-4 short sentences or a tight bullet list — no fluff, no restating the section \
title as a sentence. Money is INR; format naturally (e.g. "₹1.2 Cr"). If a section's data is empty or \
zero, say so plainly ("No deals were won in this period") rather than omitting the section. Mention \
disclosures (excluded/malformed records) briefly at the end under a short "Data notes" line, not scattered \
throughout. Any field ending in "_examples" is an illustrative sample, NOT the full population — never \
count, categorize, or summarize its contents as if it were complete; use only the accompanying _total or \
_by_status field for any count or breakdown you state."""


def narrate_leadership_update(data: dict) -> str:
    messages = [{"role": "user", "content": f"Computed data for the period {data['period']}:\n{data}"}]
    return create_text(NARRATOR_SYSTEM_PROMPT, messages, max_tokens=1200)


async def generate_leadership_update(monday_token: str, period_days: int = 30) -> str:
    """Never raises — MCP/LLM failures come back as a clear message, per the
    project's error-handling requirement."""
    from agent import _friendly_mcp_error  # local import: avoids a circular import at module load time

    try:
        data = await compute_leadership_data(monday_token, period_days)
    except Exception as exc:  # noqa: BLE001
        return _friendly_mcp_error(exc)
    try:
        return narrate_leadership_update(data)
    except LLMProviderError as exc:
        return f"⚠️ Computed the leadership data but couldn't phrase it (language model error): {exc}\n\nRaw data: {data}"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Computed the leadership data but hit an unexpected error phrasing it: {exc}\n\nRaw data: {data}"
