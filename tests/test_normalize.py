import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data_normalize import (
    company_key,
    extract_numeric_series,
    filter_out_artifact_items,
    is_missing,
    is_template_artifact_item,
    is_valid_sector,
    label_key,
    normalize_label,
    normalize_text,
    owner_key,
    parse_date,
    split_multi_label,
    to_number,
)


class TestIsMissing:
    def test_none_is_missing(self):
        assert is_missing(None)

    def test_null_like_tokens(self):
        for token in ["", "  ", "N/A", "n/a", "-", "--", "TBD", "null", "None"]:
            assert is_missing(token), f"{token!r} should be missing"

    def test_real_values_not_missing(self):
        assert not is_missing("0")
        assert not is_missing("Mining")
        assert not is_missing(0)


class TestToNumber:
    def test_plain_string_number(self):
        assert to_number("264398.08") == 264398.08

    def test_zero_string_is_not_none(self):
        # "0" is a real value (e.g. fully collected), distinct from missing.
        assert to_number("0") == 0.0

    def test_negative_billing_correction(self):
        assert to_number("-82907.29608") == -82907.29608

    def test_thousands_separator(self):
        assert to_number("1,234.5") == 1234.5

    def test_missing_returns_none(self):
        assert to_number(None) is None
        assert to_number("") is None
        assert to_number("N/A") is None

    def test_garbage_returns_none_not_raise(self):
        assert to_number("not a number") is None

    def test_numeric_passthrough(self):
        assert to_number(42) == 42.0
        assert to_number(3.14) == 3.14


class TestNormalizeText:
    def test_trims_and_collapses_whitespace(self):
        assert normalize_text("  Mining   Sector  ") == "Mining Sector"

    def test_missing_is_none(self):
        assert normalize_text(None) is None
        assert normalize_text("   ") is None


class TestLabelNormalization:
    def test_fixes_known_typo(self):
        # Real production data has both "Billed" and "BIlled" as distinct
        # labels on Work Orders' Billing Status column.
        assert normalize_label("BIlled") == "Billed"
        assert normalize_label("Billed") == "Billed"
        assert normalize_label("billed") == "Billed"

    def test_unknown_label_passthrough_trimmed(self):
        assert normalize_label("  Partially Billed  ") == "Partially Billed"

    def test_label_key_groups_typo_variants_together(self):
        assert label_key("Billed") == label_key("BIlled") == label_key("  billed ")

    def test_label_key_missing_is_none(self):
        assert label_key(None) is None


class TestParseDate:
    def test_iso_format(self):
        assert parse_date("2025-09-27") == date(2025, 9, 27)

    def test_day_first_slash_format(self):
        # Ambiguous D/M vs M/D resolved day-first (India-based operational data).
        assert parse_date("27/09/2025") == date(2025, 9, 27)

    def test_written_out_format(self):
        assert parse_date("27 September 2025") == date(2025, 9, 27)

    def test_abbreviated_month(self):
        assert parse_date("Sep 27, 2025") == date(2025, 9, 27)

    def test_missing_returns_none(self):
        assert parse_date(None) is None
        assert parse_date("") is None

    def test_garbage_returns_none_not_raise(self):
        assert parse_date("not a date at all") is None

    def test_date_object_passthrough(self):
        d = date(2025, 1, 1)
        assert parse_date(d) == d


class TestCompanyKey:
    def test_work_orders_and_deals_prefixes_join(self):
        # "WOCOMPANY_002" (Work Orders) and "COMPANY002" (Deals) must resolve
        # to the same join key despite the different prefix.
        assert company_key("WOCOMPANY_002") == company_key("COMPANY002") == 2

    def test_no_digits_returns_none(self):
        assert company_key("COMPANY") is None

    def test_missing_returns_none(self):
        assert company_key(None) is None


class TestOwnerKey:
    def test_same_namespace_normalizes_whitespace_and_case(self):
        assert owner_key("OWNER_003") == owner_key("  owner_003 ") == "OWNER_003"

    def test_missing_returns_none(self):
        assert owner_key(None) is None


class TestIsValidSector:
    def test_real_sector_is_valid(self):
        assert is_valid_sector("Mining")

    def test_column_title_leaked_as_value_is_invalid(self):
        # Found twice in the real Deals data: "Sector/service" is the
        # column's own title, not a sector.
        assert not is_valid_sector("Sector/service")
        assert not is_valid_sector("sector/service")

    def test_missing_is_invalid(self):
        assert not is_valid_sector(None)

    def test_unusual_but_legitimate_category_kept(self):
        # Ambiguous but treated as a real distinct category, not filtered —
        # see DECISION_LOG.md.
        assert is_valid_sector("Tender")
        assert is_valid_sector("DSP")


class TestTemplateArtifactDetection:
    TITLES = {
        "color_mm6qztv5": "Deal Status",
        "color_mm6q8qz4": "Closure Probability",
        "color_mm6qcs37": "Deal Stage",
        "color_mm6q3qtp": "Product deal",
        "color_mm6qdj66": "Sector/service",
        "text_mm6qcm6n": "Client Code",
    }

    def test_real_garbage_row_from_deals_board_detected(self):
        # The exact shape of the 2 corrupted rows found in the live Deals data.
        row = {
            "text_mm6q8rz0": None,
            "text_mm6qcm6n": None,
            "color_mm6qztv5": "Deal Status",
            "color_mm6q8qz4": "Closure Probability",
            "numeric_mm6qc1p3": None,
            "color_mm6qcs37": "Deal Stage",
            "color_mm6q3qtp": "Product deal",
            "color_mm6qdj66": "Sector/service",
        }
        assert is_template_artifact_item(row, self.TITLES)

    def test_real_data_row_not_flagged(self):
        row = {
            "color_mm6qztv5": "Open",
            "color_mm6qcs37": "A. Lead Generated",
            "text_mm6qcm6n": "COMPANY089",
        }
        assert not is_template_artifact_item(row, self.TITLES)

    def test_single_coincidental_match_not_enough(self):
        row = {"color_mm6qztv5": "Deal Status"}  # only one match
        assert not is_template_artifact_item(row, self.TITLES)

    def test_filter_out_artifact_items_counts_exclusions(self):
        good = {"id": "1", "column_values": {"color_mm6qztv5": "Open", "text_mm6qcm6n": "COMPANY089"}}
        bad = {
            "id": "2",
            "column_values": {
                "color_mm6qztv5": "Deal Status",
                "color_mm6qcs37": "Deal Stage",
            },
        }
        clean, excluded = filter_out_artifact_items([good, bad], self.TITLES)
        assert clean == [good]
        assert excluded == 1


class TestSplitMultiLabel:
    def test_comma_separated(self):
        assert split_multi_label("Hydrology, Topography Survey: RGB") == ["Hydrology", "Topography Survey: RGB"]

    def test_plus_separated(self):
        assert split_multi_label("Dock + DMO + Spectra") == ["Dock", "DMO", "Spectra"]

    def test_single_value_no_separator(self):
        assert split_multi_label("Mining") == ["Mining"]

    def test_missing_returns_empty_list(self):
        assert split_multi_label(None) == []
        assert split_multi_label("") == []


class TestNumericSeries:
    def test_excludes_missing_and_reports_count(self):
        series = extract_numeric_series(["100", None, "200", "N/A", "300"])
        assert series.values == [100.0, 200.0, 300.0]
        assert series.excluded_count == 2
        assert series.total_count == 5
        assert series.mean() == 200.0

    def test_disclosure_present_when_excluded(self):
        series = extract_numeric_series([None, "50"], exclusion_reason="no close date")
        assert series.disclosure() == "excluded 1 of 2 records (no close date)"

    def test_disclosure_none_when_nothing_excluded(self):
        series = extract_numeric_series(["1", "2"])
        assert series.disclosure() is None

    def test_empty_input_mean_is_none_not_zero_division(self):
        series = extract_numeric_series([None, "N/A"])
        assert series.mean() is None
        assert series.sum() == 0
