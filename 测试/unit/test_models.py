import pytest

from 主程序.domain.models import ConservationCounts, ConservationError, RawRow, SourceRowRef


@pytest.mark.parametrize("sha", ["g" * 64, "a" * 63, "a" * 65, None, 123])
def test_source_ref_requires_hex_sha(sha):
    with pytest.raises(ValueError):
        SourceRowRef(sha, "Synthetic", 2)


@pytest.mark.parametrize("row", [True, False, 1, 2.5, "2", None])
def test_source_ref_requires_integer_row_not_bool(row):
    with pytest.raises(ValueError):
        SourceRowRef("a" * 64, "Synthetic", row)


@pytest.mark.parametrize("sheet", ["", None, 123])
def test_source_ref_requires_sheet_text(sheet):
    with pytest.raises(ValueError):
        SourceRowRef("a" * 64, sheet, 2)


def test_source_ref_case_canonical_identity():
    assert SourceRowRef("A" * 64, "Synthetic", 2).row_id() == SourceRowRef("a" * 64, "Synthetic", 2).row_id()


def test_raw_mapping_is_a_defensive_copy():
    values = {"invoice": "S1"}
    row = RawRow(SourceRowRef("a" * 64, "Synthetic", 2), values)
    values["invoice"] = "changed"
    assert row.raw("invoice") == "S1"
    with pytest.raises(TypeError):
        row.values["invoice"] = "changed"


def test_raw_cell_types_are_a_defensive_copy(make_row):
    types = {"customer_id": "n"}
    row = make_row(cell_types=types)
    types["customer_id"] = "s"
    assert row.cell_types["customer_id"] == "n"
    assert row.excel_epoch == "1900"
    with pytest.raises(TypeError):
        row.cell_types["customer_id"] = "s"


def test_TC21_domain_conservation_only():
    reasons = {"ZERO_QUANTITY": 2}
    counts = ConservationCounts(12, 2, 10, 6, 2, 2, reasons)
    assert counts.balanced
    assert counts.delta_original == counts.delta_selected == 0
    reasons["ZERO_QUANTITY"] = 99
    assert counts.reason_counts["ZERO_QUANTITY"] == 2
    with pytest.raises(TypeError):
        counts.reason_counts["ZERO_QUANTITY"] = 99
    assert counts.as_dict()["selected_count"] == 10


def test_TC22_domain_conservation_only():
    counts = ConservationCounts(12, 2, 10, 6, 1, 2)
    assert not counts.balanced
    error = ConservationError(counts)
    assert error.counts.delta_selected == 1
    assert error.counts.delta_original == 0
