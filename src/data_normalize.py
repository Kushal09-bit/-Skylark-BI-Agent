"""
Normalization for the messy fields actually observed on the Work Orders and
Deals boards (see DECISION_LOG.md "Real messy-data findings"): numbers sent as
strings, null-like text tokens, a text-typed date column sitting next to
proper date columns, a real casing typo in a status label ("BIlled"), and two
differently-prefixed company code namespaces that need joining.

Every function here is null-safe and never raises on bad input — a value that
can't be parsed comes back as None (or, for numeric series, is counted as
excluded) rather than crashing the caller or silently corrupting an aggregate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from dateutil import parser as dateutil_parser

_MISSING_TOKENS = {"", "n/a", "na", "none", "null", "-", "--", "tbd", "unknown"}

# Real typo found in production Work Orders data (Billing Status column):
# "BIlled" alongside correctly-cased "Billed". Keyed by casefold so any
# whitespace/case variant of a known label maps to one canonical display form.
_LABEL_FIXES = {
    "billed": "Billed",
}


def is_missing(value: object) -> bool:
    """True for None, whitespace-only strings, and common null-like tokens."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in _MISSING_TOKENS
    return False


def to_number(value: object) -> float | None:
    """
    monday.com's MCP tools return numeric column values as JSON strings
    (including "0" and negative amounts for billing corrections). Also
    tolerates thousands separators. Returns None for missing/unparseable
    input rather than raising — callers must treat None as "exclude from
    this aggregate", not as zero.
    """
    if is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def normalize_text(value: object) -> str | None:
    """Trim + collapse internal whitespace. None for missing input."""
    if is_missing(value):
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    return text or None


def normalize_label(value: object) -> str | None:
    """
    Display-safe canonical form of a status/dropdown label: whitespace
    normalized, and known casing typos (e.g. "BIlled") corrected. Unknown
    labels are returned trimmed but otherwise untouched — we don't guess at
    canonical casing for labels we haven't seen in the wild.
    """
    text = normalize_text(value)
    if text is None:
        return None
    return _LABEL_FIXES.get(text.casefold(), text)


def label_key(value: object) -> str | None:
    """Casefolded grouping key so 'Billed' and 'BIlled' land in the same bucket."""
    label = normalize_label(value)
    return label.casefold() if label is not None else None


# Ambiguous numeric dates (DD/MM vs MM/DD) are parsed day-first: this dataset
# is an India-based company's operational data (INR amounts throughout).
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def parse_date(value: object) -> date | None:
    """
    Parses a date regardless of the source column's declared type — monday.com
    has at least one date-shaped field ("Collection Date") stored as a plain
    text column, so column type alone can't tell us what's a date. Tries known
    explicit formats first (deterministic), then falls back to dateutil's
    fuzzy parser. Returns None on anything unparseable; never raises.
    """
    if is_missing(value):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return dateutil_parser.parse(text, dayfirst=True, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


_COMPANY_CODE_RE = re.compile(r"(\d+)")


def company_key(value: object) -> int | None:
    """
    Joins Work Orders' "Customer Name Code" (WOCOMPANY_002) against Deals'
    "Client Code" (COMPANY002) on their shared numeric suffix. See
    DECISION_LOG.md — there is no explicit foreign key between the boards;
    this is the best available join and is treated as an assumption, not a
    guarantee. Returns None if no digits are present to extract.
    """
    text = normalize_text(value)
    if text is None:
        return None
    match = _COMPANY_CODE_RE.search(text)
    return int(match.group(1)) if match else None


_INVALID_SECTOR_LABELS = {"sector/service"}


def is_valid_sector(value: object) -> bool:
    """
    False for missing values and for values that are data-entry artifacts
    rather than real sectors. Confirmed in the full Deals dataset: "Sector/
    service" appears twice — the column's own title typed in as a value. Other
    unusual-looking labels ("Tender", "DSP") are kept as legitimate distinct
    categories rather than filtered out; see DECISION_LOG.md.
    """
    key = label_key(value)
    return key is not None and key not in _INVALID_SECTOR_LABELS


def owner_key(value: object) -> str | None:
    """
    Canonical join key for "BD/KAM Personnel code" (Work Orders) and "Owner
    code" (Deals) — both already use the identical OWNER_NNN namespace, so
    this only needs to normalize whitespace/case, not translate a prefix.
    """
    text = normalize_text(value)
    return text.upper() if text is not None else None


def is_template_artifact_item(column_values: dict, column_titles_by_id: dict) -> bool:
    """
    True for malformed rows rather than real data. Confirmed in the live Deals
    board: 2 items where every populated status column's value is literally
    that column's own title ("Deal Status" == "Deal Status", "Sector/service"
    == "Sector/service", ...) with every other field null — almost certainly
    a corrupted header/template row surviving the CSV import. `column_titles_by_id`
    is the id->title map from a live get_board_schema call, not a hardcoded
    list, so this still works if the board's columns change.
    Requires at least 2 matching status columns to avoid a false positive from
    one coincidental match.
    """
    matches = 0
    for col_id, value in column_values.items():
        title = column_titles_by_id.get(col_id)
        if title is None or is_missing(value):
            continue
        if label_key(value) == label_key(title):
            matches += 1
    return matches >= 2


def filter_out_artifact_items(items: list[dict], column_titles_by_id: dict) -> tuple[list[dict], int]:
    """Splits board items into (clean_items, excluded_count) using is_template_artifact_item."""
    clean = [i for i in items if not is_template_artifact_item(i.get("column_values", {}), column_titles_by_id)]
    return clean, len(items) - len(clean)


def split_multi_label(value: object) -> list[str]:
    """
    Some status/dropdown columns hold multiple labels joined inconsistently —
    "Type of Work" uses commas ("Hydrology, Topography Survey: RGB"), "Product
    deal" uses "+" ("Dock + DMO + Spectra"). Splits on whichever separator is
    present; returns a single-item list for an ordinary value, [] for missing.
    """
    text = normalize_text(value)
    if text is None:
        return []
    separator = "," if "," in text else ("+" if "+" in text else None)
    if separator is None:
        return [text]
    return [part.strip() for part in text.split(separator) if part.strip()]


@dataclass
class NumericSeries:
    """A cleaned numeric series plus what got excluded and why, so callers
    can produce the "excluded N records with no X" disclosure required on
    every answer touching incomplete data."""

    values: list[float] = field(default_factory=list)
    excluded_count: int = 0
    total_count: int = 0
    exclusion_reason: str = ""

    @property
    def included_count(self) -> int:
        return len(self.values)

    def mean(self) -> float | None:
        return sum(self.values) / len(self.values) if self.values else None

    def sum(self) -> float:
        return sum(self.values)

    def disclosure(self) -> str | None:
        """A one-line note for the excluded records, or None if nothing was excluded."""
        if self.excluded_count == 0:
            return None
        noun = "record" if self.total_count == 1 else "records"
        return f"excluded {self.excluded_count} of {self.total_count} {noun} ({self.exclusion_reason})"


def extract_numeric_series(
    raw_values: list[object], exclusion_reason: str = "missing or non-numeric value"
) -> NumericSeries:
    """Cleans a raw list of column values into a NumericSeries, tracking exclusions."""
    series = NumericSeries(total_count=len(raw_values), exclusion_reason=exclusion_reason)
    for raw in raw_values:
        number = to_number(raw)
        if number is None:
            series.excluded_count += 1
        else:
            series.values.append(number)
    return series
