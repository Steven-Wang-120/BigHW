from dataclasses import replace
from datetime import datetime
from decimal import localcontext
import json
from pathlib import Path

import pytest

from 主程序.domain.models import CLASS_EXCLUDED
from 主程序.domain.normalization import classify_row, mark_suspected_duplicates

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "数据/fixtures/controlled_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["rows"], ids=lambda case: case["id"])
def test_controlled_domain_verdict(case, make_row):
    row = make_row(case["values"], cell_types=case.get("cell_types"), has_formula=case.get("has_formula", False))
    verdict = classify_row(row)
    assert verdict.row_class == case["class"]
    assert verdict.primary_exclusion_code == case.get("primary")
    assert set(case.get("flags", [])).issubset(verdict.flag_set())
    for name, attribute in [("units", "price_units"), ("amount", "amount_units"), ("stock", "stock_code"), ("customer", "customer_key")]:
        if name in case:
            assert getattr(verdict, attribute) == case[name]
    if case["id"] == "TC-04-multiple-reasons":
        assert verdict.quantity == 0
        assert verdict.stock_code == "SKU-A"
        assert verdict.invoice_datetime == datetime(2023, 3, 15)


@pytest.mark.parametrize("field", ["quantity", "price"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_TC05_c_each_nonfinite_field(field, value, make_row):
    verdict = classify_row(make_row({field: value}))
    assert verdict.row_class == CLASS_EXCLUDED
    assert verdict.primary_exclusion_code == "INVALID_NUMBER_OR_DATE"
    assert verdict.amount_units is None


@pytest.mark.parametrize("case", FIXTURE["dates"], ids=lambda case: case["id"])
def test_CR08_source_epoch_and_microseconds(case, make_row):
    with localcontext() as context:
        context.prec = 3
        verdict = classify_row(make_row({"invoicedate_serial": case["serial"]}, excel_epoch=case["epoch"]))
    assert verdict.row_class == "SALE"
    assert verdict.invoice_datetime == datetime.fromisoformat(case["expected"])
    assert verdict.invoice_datetime.tzinfo is None


@pytest.mark.parametrize("serial,epoch", [("60", "1900"), ("60.5", "1900"), ("-1", "1900"), ("1e5000", "1900"), ("1e1000000000", "1900"), ("1", "unknown"), ("NaN", "1904")])
def test_CR08_invalid_dates_do_not_crash(serial, epoch, make_row, monkeypatch):
    if "e" in serial:
        import 主程序.domain.normalization as normalization

        def forbidden_int(value):
            raise AssertionError("extreme dates must be rejected before int conversion")

        monkeypatch.setattr(normalization, "int", forbidden_int, raising=False)
    verdict = classify_row(make_row({"invoicedate_serial": serial}, excel_epoch=epoch))
    assert verdict.primary_exclusion_code == "INVALID_NUMBER_OR_DATE"


@pytest.mark.parametrize("raw,expected", [("123456789012345678901234567890.0", "123456789012345678901234567890"), ("1e1000000000", "1e1000000000")])
def test_CR03_numeric_customer_metadata_never_uses_float_or_allocates_huge_int(raw, expected, make_row):
    verdict = classify_row(make_row({"customer_id": raw}, cell_types={"customer_id": "n"}))
    assert verdict.row_class == "SALE"
    assert verdict.customer_key == expected


def test_CR02_formula_metadata_does_not_trust_cached_quantity(make_row):
    verdict = classify_row(make_row({"quantity": "2"}, cell_types={"quantity": "f"}))
    assert verdict.primary_exclusion_code == "FORMULA_UNSUPPORTED"
    assert verdict.quantity is None
    assert verdict.price_units == 20000


def test_CR02_empty_row_priority(make_row):
    verdict = classify_row(make_row({name: None for name in FIXTURE["defaults"]}))
    assert verdict.primary_exclusion_code == "EMPTY_ROW"


def test_TC18_marking_only_distinct_source_rows(make_row):
    first = classify_row(make_row({"quantity": "5"}, row_number=2))
    second = classify_row(make_row({"quantity": "5"}, row_number=3))
    result = mark_suspected_duplicates([first, second])
    assert len(result) == 2
    assert sum(v.quantity for v in result) == 10
    assert all("SUSPECTED_DUPLICATE" in v.flag_set() for v in result)
    assert all("SUSPECTED_DUPLICATE" not in v.flag_set() for v in [first, second])
    assert mark_suspected_duplicates(result) == result


def test_TC18_marking_only_same_ref_not_distinct(make_row):
    first = classify_row(make_row())
    same_ref = replace(first, ref=replace(first.ref, source_sha256="A" * 64))
    result = mark_suspected_duplicates([first, same_ref, first])
    assert len(result) == 3
    assert all("SUSPECTED_DUPLICATE" not in v.flag_set() for v in result)


def test_TC18_marking_only_description_and_subsecond_are_content(make_row):
    first = classify_row(make_row())
    second = classify_row(make_row({"description": "Different synthetic description"}, row_number=3))
    third = classify_row(make_row({"invoicedate_serial": "45000.000001"}, row_number=4))
    assert all("SUSPECTED_DUPLICATE" not in v.flag_set() for v in mark_suspected_duplicates([first, second, third]))
