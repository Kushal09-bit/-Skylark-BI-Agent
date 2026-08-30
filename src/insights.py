"""
Business-intelligence computations over live Work Orders / Deals data.

Every function here takes already-fetched BoardData (see load_board) — nothing
in this module calls Monday.com itself, and nothing here caches results across
calls. agent.py is responsible for calling load_board() fresh for every
question. Column *titles* below are schema vocabulary (structural, not data)
resolved to live column ids via get_board_schema on every load — see
DECISION_LOG.md for why that's not a "static fallback".

Two metric definitions aren't literal monday.com fields and are documented
here rather than left implicit:
  - "At-risk/overdue work order": Probable End Date has passed AND Execution
    Status isn't Completed/Partial Completed AND WO Status (billed) isn't Closed.
  - "Revenue is attributed by Probable End Date" for any time-filtered revenue
    query, since there is no explicit "billed on" or "collected on" date field
    on the board — only status/amount columns and a handful of unrelated dates.
  - "Win rate" = Won / (Won + Dead), excluding Open/On Hold deals (deals not
    yet decided don't belong in a win-rate denominator).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from data_normalize import (
    company_key,
    extract_numeric_series,
    filter_out_artifact_items,
    is_missing,
    is_valid_sector,
    label_key,
    normalize_label,
    owner_key,
    parse_date,
    split_multi_label,
    to_number,
)
from monday_mcp_client import MondayMCPClient

WORK_ORDER_FIELD_TITLES = {
    "customer_code": "Customer Name Code",
    "serial_no": "Serial #",
    "nature_of_work": "Nature of Work",
    "execution_status": "Execution Status",
    "sector": "Sector",
    "type_of_work": "Type of Work",
    "owner_code": "BD/KAM Personnel code",
    "probable_start_date": "Probable Start Date",
    "probable_end_date": "Probable End Date",
    "data_delivery_date": "Data Delivery Date",
    "amount_excl_gst": "Amount in Rupees (Excl of GST) (Masked)",
    "billed_incl_gst": "Billed Value in Rupees (Incl of GST.) (Masked)",
    "collected_incl_gst": "Collected Amount in Rupees (Incl of GST.) (Masked)",
    "amount_receivable": "Amount Receivable (Masked)",
    "invoice_status": "Invoice Status",
    "wo_status_billed": "WO Status (billed)",
    "billing_status": "Billing Status",
}

DEALS_FIELD_TITLES = {
    "owner_code": "Owner code",
    "client_code": "Client Code",
    "deal_status": "Deal Status",
    "close_date_actual": "Close Date (A)",
    "closure_probability": "Closure Probability",
    "deal_value": "Masked Deal value",
    "tentative_close_date": "Tentative Close Date",
    "deal_stage": "Deal Stage",
    "product_deal": "Product deal",
    "sector": "Sector/service",
    "created_date": "Created Date",
}


class InsightsSchemaError(Exception):
    """The live board schema no longer has a column this project depends on."""


@dataclass
class BoardData:
    name: str
    id: str
    items: list[dict]
    field_ids: dict[str, str]
    artifact_excluded: int
    total_raw: int

    def get(self, item: dict, field: str):
        col_id = self.field_ids.get(field)
        return item.get("column_values", {}).get(col_id) if col_id else None


def _resolve_field_ids(schema: dict, field_titles: dict[str, str]) -> dict[str, str]:
    title_to_id = {c["title"]: c["id"] for c in schema["columns"]}
    resolved, missing = {}, []
    for semantic, title in field_titles.items():
        if title in title_to_id:
            resolved[semantic] = title_to_id[title]
        else:
            missing.append(title)
    if missing:
        raise InsightsSchemaError(
            f"The live board schema is missing expected column(s): {', '.join(missing)}. "
            f"It may have been renamed or restructured since this app was built."
        )
    return resolved


async def load_board(client: MondayMCPClient, board_name: str, field_titles: dict[str, str]) -> BoardData:
    handle = await client.find_board_by_name(board_name)
    schema = await client.get_board_schema(handle.id)
    column_titles_by_id = {c["id"]: c["title"] for c in schema["columns"]}
    field_ids = _resolve_field_ids(schema, field_titles)
    raw_items = await client.get_all_board_items(handle.id, include_columns=True)
    clean_items, excluded = filter_out_artifact_items(raw_items, column_titles_by_id)
    return BoardData(
        name=handle.name, id=handle.id, items=clean_items,
        field_ids=field_ids, artifact_excluded=excluded, total_raw=len(raw_items),
    )


def _in_range(d: date | None, start: date | None, end: date | None) -> bool:
    if d is None:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def filter_by_date_field(board: BoardData, field: str, start: date | None, end: date | None) -> tuple[list[dict], int]:
    """All-time (start and end both None) short-circuits to "no filtering, no exclusions" —
    an item with a missing date isn't a data problem if the question never asked about dates."""
    if start is None and end is None:
        return board.items, 0
    in_range, excluded = [], 0
    for item in board.items:
        d = parse_date(board.get(item, field))
        if _in_range(d, start, end):
            in_range.append(item)
        else:
            excluded += 1
    return in_range, excluded


def revenue_summary(board: BoardData, start: date | None, end: date | None) -> dict:
    items, out_of_range = filter_by_date_field(board, "probable_end_date", start, end)
    billed = extract_numeric_series(
        [board.get(i, "billed_incl_gst") for i in items], "no billed amount recorded"
    )
    collected = extract_numeric_series(
        [board.get(i, "collected_incl_gst") for i in items], "no collected amount recorded"
    )
    receivable = extract_numeric_series(
        [board.get(i, "amount_receivable") for i in items], "no receivable amount recorded"
    )
    disclosures = [d for d in (billed.disclosure(), collected.disclosure(), receivable.disclosure()) if d]
    if out_of_range and (start or end):
        disclosures.append(f"{out_of_range} work orders fell outside the requested date range (by Probable End Date)")

    collection_rate = None
    if billed.sum() > 0:
        collection_rate = collected.sum() / billed.sum()

    interpretation = (
        f"Collected {collection_rate:.0%} of billed value so far" if collection_rate is not None
        else "No billed value in this range to compute a collection rate against"
    )

    return {
        "billed_total": billed.sum(),
        "collected_total": collected.sum(),
        "receivable_total": receivable.sum(),
        "collection_rate": collection_rate,
        "work_order_count": len(items),
        "interpretation": interpretation,
        "disclosures": disclosures,
        "date_basis": "Probable End Date (no explicit billed/collected-on date field exists on this board)",
    }


def pipeline_health(board: BoardData, start: date | None, end: date | None) -> dict:
    items, out_of_range = filter_by_date_field(board, "created_date", start, end)
    by_status: dict[str, list[dict]] = {}
    for item in items:
        status = normalize_label(board.get(item, "deal_status")) or "(no status)"
        by_status.setdefault(status, []).append(item)

    won = by_status.get("Won", [])
    dead = by_status.get("Dead", [])
    win_rate = len(won) / (len(won) + len(dead)) if (won or dead) else None

    open_items = by_status.get("Open", [])
    open_values = extract_numeric_series([board.get(i, "deal_value") for i in open_items], "no deal value recorded")
    won_values = extract_numeric_series([board.get(i, "deal_value") for i in won], "no deal value recorded")

    disclosures = [d for d in (open_values.disclosure(), won_values.disclosure()) if d]
    if board.artifact_excluded:
        disclosures.append(f"{board.artifact_excluded} malformed template rows excluded from all Deals figures")
    if out_of_range and (start or end):
        disclosures.append(f"{out_of_range} deals fell outside the requested date range (by Created Date)")

    interpretation = (
        f"Win rate {win_rate:.0%} across {len(won) + len(dead)} decided deals"
        if win_rate is not None else "No decided (Won/Dead) deals yet in this range to compute a win rate"
    )

    return {
        "counts_by_status": {k: len(v) for k, v in by_status.items()},
        "open_pipeline_value": open_values.sum(),
        "avg_won_deal_size": won_values.mean(),
        "win_rate": win_rate,
        "interpretation": interpretation,
        "disclosures": disclosures,
    }


def sector_breakdown(wo_board: BoardData, deals_board: BoardData) -> dict:
    wo_by_sector: dict[str, list[dict]] = {}
    wo_excluded = 0
    for item in wo_board.items:
        raw = wo_board.get(item, "sector")
        if not is_valid_sector(raw):
            wo_excluded += 1
            continue
        wo_by_sector.setdefault(normalize_label(raw), []).append(item)

    deal_by_sector: dict[str, list[dict]] = {}
    deal_excluded = 0
    for item in deals_board.items:
        raw = deals_board.get(item, "sector")
        if not is_valid_sector(raw):
            deal_excluded += 1
            continue
        deal_by_sector.setdefault(normalize_label(raw), []).append(item)

    result = {}
    all_sectors = set(wo_by_sector) | set(deal_by_sector)
    for sector in all_sectors:
        wo_items = wo_by_sector.get(sector, [])
        deal_items = deal_by_sector.get(sector, [])
        billed = extract_numeric_series([wo_board.get(i, "billed_incl_gst") for i in wo_items])
        pipeline = extract_numeric_series([deals_board.get(i, "deal_value") for i in deal_items])
        result[sector] = {
            "work_order_count": len(wo_items),
            "deal_count": len(deal_items),
            "billed_total": billed.sum(),
            "pipeline_value": pipeline.sum(),
        }

    disclosures = []
    if wo_excluded:
        disclosures.append(f"{wo_excluded} work orders excluded (missing/invalid sector value)")
    if deal_excluded:
        disclosures.append(f"{deal_excluded} deals excluded (missing/invalid sector value, e.g. 'Sector/service' artifact)")

    top_sector = max(result.items(), key=lambda kv: kv[1]["billed_total"], default=(None, None))[0]
    interpretation = f"{top_sector} leads in billed revenue" if top_sector else "No sector data available"

    return {"by_sector": result, "interpretation": interpretation, "disclosures": disclosures}


def _is_at_risk(board: BoardData, item: dict, today: date) -> bool:
    end_date = parse_date(board.get(item, "probable_end_date"))
    if end_date is None or end_date >= today:
        return False
    exec_status = label_key(board.get(item, "execution_status"))
    if exec_status in {label_key("Completed"), label_key("Partial Completed")}:
        return False
    wo_status = label_key(board.get(item, "wo_status_billed"))
    if wo_status == label_key("Closed"):
        return False
    return True


def operational_metrics(board: BoardData, today: date) -> dict:
    total = len(board.items)
    exec_statuses = [board.get(i, "execution_status") for i in board.items]
    completed = sum(1 for s in exec_statuses if label_key(s) == label_key("Completed"))
    known_status = sum(1 for s in exec_statuses if not is_missing(s))
    completion_rate = completed / known_status if known_status else None

    at_risk_items = [i for i in board.items if _is_at_risk(board, i, today)]

    status_counts: dict[str, int] = {}
    for s in exec_statuses:
        label = normalize_label(s) or "(no status)"
        status_counts[label] = status_counts.get(label, 0) + 1

    missing_status = total - known_status
    disclosures = []
    if missing_status:
        disclosures.append(f"{missing_status} of {total} work orders have no Execution Status recorded")

    interpretation = (
        f"{completion_rate:.0%} completion rate; {len(at_risk_items)} work orders are at-risk or overdue"
        if completion_rate is not None else f"{len(at_risk_items)} work orders are at-risk or overdue"
    )

    return {
        "total_work_orders": total,
        "completion_rate": completion_rate,
        "at_risk_count": len(at_risk_items),
        "at_risk_items": [{"serial_no": board.get(i, "serial_no"), "id": i["id"]} for i in at_risk_items],
        "status_counts": status_counts,
        "interpretation": interpretation,
        "disclosures": disclosures,
        "at_risk_definition": (
            "Probable End Date has passed AND Execution Status isn't Completed/Partial "
            "Completed AND WO Status (billed) isn't Closed — not a literal monday.com field."
        ),
    }


def cross_board_conversion(deals_board: BoardData, wo_board: BoardData, today: date) -> dict:
    """Which companies with a Won deal have a matching (by company code) work order,
    and whether that work order is active/at-risk or already closed."""
    won_deals = [d for d in deals_board.items if label_key(deals_board.get(d, "deal_status")) == label_key("Won")]
    won_companies = {company_key(deals_board.get(d, "client_code")) for d in won_deals}
    won_companies.discard(None)

    wo_by_company: dict[int, list[dict]] = {}
    for wo in wo_board.items:
        key = company_key(wo_board.get(wo, "customer_code"))
        if key is not None:
            wo_by_company.setdefault(key, []).append(wo)

    converted, not_yet_converted, at_risk_among_converted = 0, 0, 0
    for company in won_companies:
        matching_wos = wo_by_company.get(company, [])
        if matching_wos:
            converted += 1
            if any(_is_at_risk(wo_board, wo, today) for wo in matching_wos):
                at_risk_among_converted += 1
        else:
            not_yet_converted += 1

    conversion_rate = converted / len(won_companies) if won_companies else None
    interpretation = (
        f"{conversion_rate:.0%} of won-deal companies ({converted}/{len(won_companies)}) have a work order in "
        f"delivery; {at_risk_among_converted} of those are at-risk"
        if conversion_rate is not None else "No won deals to compute conversion against"
    )

    return {
        "won_company_count": len(won_companies),
        "converted_to_delivery": converted,
        "not_yet_converted": not_yet_converted,
        "at_risk_among_converted": at_risk_among_converted,
        "conversion_rate": conversion_rate,
        "interpretation": interpretation,
        "disclosures": [
            "Join is by normalized company code (no explicit deal-to-work-order foreign key exists — see DECISION_LOG.md)"
        ],
    }


def breakdown_by_field(board: BoardData, field: str, value_field: str | None = None) -> dict:
    """Generic grouping (counts, and optionally a value_field sum) by any label
    field — covers owner/deal-stage/billing-status breakdowns without a bespoke
    function per field."""
    groups: dict[str, list[dict]] = {}
    missing = 0
    for item in board.items:
        raw = board.get(item, field)
        if is_missing(raw):
            missing += 1
            continue
        groups.setdefault(normalize_label(raw), []).append(item)

    result = {}
    for label, items in groups.items():
        entry = {"count": len(items)}
        if value_field:
            entry["value_sum"] = extract_numeric_series([board.get(i, value_field) for i in items]).sum()
        result[label] = entry

    disclosures = []
    if missing:
        noun = "record" if missing == 1 else "records"
        disclosures.append(f"{missing} {noun} excluded (no {field.replace('_', ' ')} recorded)")

    top = max(result.items(), key=lambda kv: kv[1]["count"], default=(None, None))[0]
    interpretation = f"'{top}' is the largest group by count" if top else "No data available for this breakdown"

    return {"by_group": result, "interpretation": interpretation, "disclosures": disclosures}


def trend_over_time(
    board: BoardData, date_field: str, value_field: str | None, granularity: str = "month"
) -> dict:
    """Buckets items by month/quarter of `date_field`; sums value_field per bucket
    (or counts items if value_field is None). Returns buckets in chronological order
    plus a trend_direction comparing the last two buckets."""
    buckets: dict[str, list[dict]] = {}
    excluded = 0
    for item in board.items:
        d = parse_date(board.get(item, date_field))
        if d is None:
            excluded += 1
            continue
        if granularity == "quarter":
            key = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
        else:
            key = f"{d.year}-{d.month:02d}"
        buckets.setdefault(key, []).append(item)

    series = []
    for period in sorted(buckets):
        items = buckets[period]
        if value_field:
            numeric = extract_numeric_series([board.get(i, value_field) for i in items])
            series.append({"period": period, "value": numeric.sum(), "count": len(items)})
        else:
            series.append({"period": period, "value": len(items), "count": len(items)})

    trend_direction = "flat"
    if len(series) >= 2:
        prev, last = series[-2]["value"], series[-1]["value"]
        if prev == 0:
            trend_direction = "up" if last > 0 else "flat"
        else:
            change = (last - prev) / prev
            trend_direction = "up" if change > 0.05 else "down" if change < -0.05 else "flat"

    disclosures = []
    if excluded:
        noun = "record" if excluded == 1 else "records"
        disclosures.append(f"{excluded} {noun} excluded (no parseable {date_field.replace('_', ' ')})")

    return {"series": series, "trend_direction": trend_direction, "disclosures": disclosures}
