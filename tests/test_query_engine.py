import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from query_engine import resolve_time_range


class TestResolveTimeRange:
    def test_this_quarter_uses_real_current_date(self):
        # 2026-08-30 is Q3 (Jul-Sep) of calendar 2026.
        tr = resolve_time_range("this_quarter", None, None, today=date(2026, 8, 30))
        assert tr.start == date(2026, 7, 1)
        assert tr.end == date(2026, 9, 30)

    def test_last_quarter_crosses_into_previous_year(self):
        # Q1 2026 (Jan-Mar) -> last quarter from Q1 is Q4 2025.
        tr = resolve_time_range("last_quarter", None, None, today=date(2026, 2, 15))
        assert tr.start == date(2025, 10, 1)
        assert tr.end == date(2025, 12, 31)

    def test_this_month(self):
        tr = resolve_time_range("this_month", None, None, today=date(2026, 2, 10))
        assert tr.start == date(2026, 2, 1)
        assert tr.end == date(2026, 2, 28)

    def test_last_month_crosses_year_boundary(self):
        tr = resolve_time_range("last_month", None, None, today=date(2026, 1, 15))
        assert tr.start == date(2025, 12, 1)
        assert tr.end == date(2025, 12, 31)

    def test_custom_range(self):
        tr = resolve_time_range("custom", "2026-01-01", "2026-03-31", today=date(2026, 8, 30))
        assert tr.start == date(2026, 1, 1)
        assert tr.end == date(2026, 3, 31)

    def test_all_time_has_no_bounds(self):
        tr = resolve_time_range("all_time", None, None, today=date(2026, 8, 30))
        assert tr.start is None
        assert tr.end is None
