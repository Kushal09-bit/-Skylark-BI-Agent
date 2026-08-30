import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from insights import BoardData, trend_over_time


def _board(items):
    """A minimal BoardData for trend_over_time tests: field_ids map semantic
    names directly to the raw column keys used in the fixture items below."""
    return BoardData(
        name="Deals", id="test", items=items,
        field_ids={"created_date": "created_date", "deal_value": "deal_value"},
        artifact_excluded=0, total_raw=len(items),
    )


class TestTrendOverTimeTimeBounding:
    """Reproduces the exact live bug: 'How's Mining pipeline this quarter?'
    (correctly ₹0, no deals in Q3) followed by 'break that down by month'
    showed nonzero months from a completely different period — because the
    trend computation ignored the same quarter bound the first answer used."""

    # Mix of deals inside Q3 2026 (Jul 1 - Sep 30) and deals from unrelated
    # earlier periods, mirroring the live data shape (Sep 2025, Nov 2025,
    # Dec 2025, Jan 2026 deals existed; none were actually in Q3 2026).
    ITEMS = [
        {"column_values": {"created_date": "2025-09-15", "deal_value": "17616960"}},
        {"column_values": {"created_date": "2025-11-10", "deal_value": "4371208.2"}},
        {"column_values": {"created_date": "2025-12-01", "deal_value": "6606360"}},
        {"column_values": {"created_date": "2026-01-05", "deal_value": "489360"}},
    ]

    def test_unbounded_includes_everything(self):
        # Old behavior (the bug): no start/end means every period shows up,
        # even ones far outside whatever quarter a previous turn discussed.
        result = trend_over_time(_board(self.ITEMS), "created_date", "deal_value", "month")
        periods = [p["period"] for p in result["series"]]
        assert periods == ["2025-09", "2025-11", "2025-12", "2026-01"]
        assert result["disclosures"] == []

    def test_bounded_to_quarter_excludes_everything_outside_it(self):
        # This is the fix: passing the SAME quarter bound the headline answer
        # used must produce a consistent (here: empty) result, not silently
        # fall back to all-time data.
        start, end = date(2026, 7, 1), date(2026, 9, 30)
        result = trend_over_time(_board(self.ITEMS), "created_date", "deal_value", "month", start, end)
        assert result["series"] == []
        assert result["trend_direction"] == "flat"
        assert "outside the requested date range" in result["disclosures"][0]
        assert "4" in result["disclosures"][0]

    def test_bounded_to_a_range_that_partially_matches(self):
        start, end = date(2025, 11, 1), date(2025, 12, 31)
        result = trend_over_time(_board(self.ITEMS), "created_date", "deal_value", "month", start, end)
        periods = [p["period"] for p in result["series"]]
        assert periods == ["2025-11", "2025-12"]
        assert "2 records fell outside" in result["disclosures"][0]


class TestTrendDirectionCorrectness:
    """Reproduces the second half of the live bug: a rise from a zero (or
    near-zero) bucket to a large one was narrated as a 'downward trend' even
    though trend_direction itself was computed correctly. These tests pin
    down that the computation is right; agent.py's narration-consistency
    check (tested separately) is what catches the narrator contradicting it."""

    def test_zero_to_nonzero_is_up_not_down(self):
        items = [
            {"column_values": {"created_date": "2026-08-01", "deal_value": "0"}},
            {"column_values": {"created_date": "2026-09-01", "deal_value": "19000000"}},
        ]
        result = trend_over_time(_board(items), "created_date", "deal_value", "month")
        assert result["trend_direction"] == "up"
        assert "up" in result["interpretation"].lower()

    def test_clear_increase_is_up(self):
        items = [
            {"column_values": {"created_date": "2026-07-01", "deal_value": "489000"}},
            {"column_values": {"created_date": "2026-09-01", "deal_value": "19000000"}},
        ]
        result = trend_over_time(_board(items), "created_date", "deal_value", "month")
        assert result["trend_direction"] == "up"

    def test_clear_decrease_is_down(self):
        items = [
            {"column_values": {"created_date": "2026-07-01", "deal_value": "19000000"}},
            {"column_values": {"created_date": "2026-09-01", "deal_value": "489000"}},
        ]
        result = trend_over_time(_board(items), "created_date", "deal_value", "month")
        assert result["trend_direction"] == "down"

    def test_interpretation_uses_same_two_buckets_as_trend_direction(self):
        # Not just the first/last of the whole series — must match whichever
        # two buckets trend_direction itself compared, or the two fields could
        # tell different stories on a longer series.
        items = [
            {"column_values": {"created_date": "2026-01-01", "deal_value": "50000000"}},  # highest, but not compared
            {"column_values": {"created_date": "2026-08-01", "deal_value": "0"}},
            {"column_values": {"created_date": "2026-09-01", "deal_value": "19000000"}},
        ]
        result = trend_over_time(_board(items), "created_date", "deal_value", "month")
        assert result["trend_direction"] == "up"
        assert "2026-08" in result["interpretation"]
        assert "2026-09" in result["interpretation"]
        assert "2026-01" not in result["interpretation"]


class TestAgentTrendNarrationConsistency:
    def test_contradicting_up_trend_is_rejected(self):
        from agent import _trend_direction_consistent
        computed = {"trend_direction": "up"}
        assert not _trend_direction_consistent("Revenue is trending down overall.", computed)

    def test_matching_up_trend_is_accepted(self):
        from agent import _trend_direction_consistent
        computed = {"trend_direction": "up"}
        assert _trend_direction_consistent("Revenue is trending up nicely this quarter.", computed)

    def test_contradicting_down_trend_is_rejected(self):
        from agent import _trend_direction_consistent
        computed = {"trend_direction": "down"}
        assert not _trend_direction_consistent("Pipeline value is increasing month over month.", computed)

    def test_flat_trend_has_no_contradiction_words_to_check(self):
        from agent import _trend_direction_consistent
        computed = {"trend_direction": "flat"}
        assert _trend_direction_consistent("Revenue was down slightly then up again.", computed)

    def test_no_trend_direction_field_always_passes(self):
        from agent import _trend_direction_consistent
        assert _trend_direction_consistent("Anything at all, up or down.", {})
