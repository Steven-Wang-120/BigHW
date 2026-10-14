from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from 主程序.domain.matching import MatchingRow, reconcile_rows, REASON_PRIORITY, RULE_DIGEST
from 主程序.domain.models import SourceRowRef
from 主程序.domain.money import INT64_MAX, NumericOverflowError, units_to_display

TIME = datetime(2011, 1, 1)


def row(n, qty, *, invoice=None, time=None, price=20000, customer="123", country="UK", flags=(), category="PRODUCT", kind=None):
    kind = kind or ("SALE" if qty > 0 else "CANCEL")
    invoice = invoice or ("S" if qty > 0 else "C") + str(n)
    return MatchingRow(str(n), SourceRowRef("a" * 64, "Synthetic", n), kind, invoice,
                       customer, "SKU", price, qty, qty * price, time or TIME, country, tuple(flags), category,
                       category == "PRODUCT" and kind != "EXCLUDED", invoice[:1].upper() == "C")


def test_tc23_25_26_partial_cancellations_and_over_quantity():
    rows = [row(2, 10), row(3, -3), row(4, -7), row(5, -1)]
    result = reconcile_rows(rows)
    assert [a.quantity for a in result.allocations] == [3, 7]
    assert [d.reason for d in result.differences] == ["INSUFFICIENT_QUANTITY"]
    assert result.lots[0].remaining == 0
    assert result.allocations[0].amount_units == 60000
    assert result.summary["quantity_delta"] == result.summary["amount_delta_units"] == "0"
    assert reconcile_rows(reversed(rows)) == result


@pytest.mark.parametrize("qty", [3, 10])
def test_tc24_27_multiple_candidates_never_split_or_guess(qty):
    result = reconcile_rows([row(2, qty), row(3, qty), row(4, -5)])
    assert not result.allocations
    assert result.differences[0].reason == "MULTIPLE_CANDIDATES"
    assert len(result.differences[0].candidates) == 2


def test_exhausted_candidate_still_counts_as_multiple():
    rows = [row(2, 3), row(3, -3), row(4, 3, time=TIME + timedelta(seconds=1)), row(5, -1, time=TIME + timedelta(seconds=2))]
    result = reconcile_rows(rows)
    assert len(result.allocations) == 1
    assert result.differences[0].reason == "MULTIPLE_CANDIDATES"
    assert sorted(c["remaining_quantity"] for c in result.differences[0].candidates) == ["0", "3"]


@pytest.mark.parametrize("offset,matched", [(90 * 86400 - 1, True), (90 * 86400, True), (90 * 86400 + 1, False), (-1, False)])
def test_tc19_20_exact_window_and_future_sale(offset, matched):
    result = reconcile_rows([row(2, 5), row(3, -2, time=TIME + timedelta(seconds=offset))])
    assert bool(result.allocations) is matched
    if not matched:
        assert result.differences[0].reason == "NO_CANDIDATE_IN_RANGE"
        assert result.differences[0].message == "当前范围未找到候选，范围外可能存在"


def test_tc15_missing_customer_does_not_guess():
    result = reconcile_rows([row(2, 5, customer=None), row(3, -2, customer=None)])
    assert result.differences[0].reason == "MISSING_CUSTOMER"
    assert not result.allocations


def test_tc16_missing_description_country_and_tc30_lot_refs():
    rows = [row(2, 2, invoice="S1", country=None, flags=("MISSING_DESCRIPTION", "MISSING_COUNTRY")),
            row(3, 4, invoice="S1", country=None), row(4, -2, country=None), row(5, -4, country=None)]
    result = reconcile_rows(rows)
    assert len(result.lots) == 1 and result.lots[0].quantity == 6
    assert result.lots[0].row_ids == ("2", "3")
    assert len(result.lots[0].source_refs) == 2
    assert [a.quantity for a in result.allocations] == [2, 4]
    assert reconcile_rows(reversed(rows)) == result


@pytest.mark.parametrize("case", ["sale_duplicate", "cancel_duplicate", "country", "lot_countries", "price_diagnostic_duplicate"])
def test_tc17_18_conflicts_suppress_and_preserve_candidates(case):
    sales = [row(2, 5, invoice="S1")]
    cancel = row(4, -2)
    if case == "sale_duplicate":
        sales[0] = replace(sales[0], flags=("SUSPECTED_DUPLICATE",))
    elif case == "cancel_duplicate":
        cancel = replace(cancel, flags=("SUSPECTED_DUPLICATE",))
    elif case == "country":
        cancel = replace(cancel, country="FR")
    elif case == "lot_countries":
        sales.append(row(3, 5, invoice="S1", country="FR"))
    else:
        sales[0] = replace(sales[0], flags=("SUSPECTED_DUPLICATE",))
        cancel = replace(cancel, price_units=20100, amount_units=-40200)
    result = reconcile_rows([*sales, cancel])
    assert not result.allocations
    expected = "COUNTRY_CONFLICT" if case in {"country", "lot_countries"} else "SUSPECTED_DUPLICATE"
    assert result.differences[0].reason == expected
    assert result.differences[0].candidates


def test_reason_priority_keeps_secondary_diagnostics():
    rows = [row(2, 1, flags=("SUSPECTED_DUPLICATE",)), row(3, 1, country="FR"), row(4, -5)]
    result = reconcile_rows(rows)
    diff = result.differences[0]
    assert REASON_PRIORITY == ("MISSING_CUSTOMER", "SUSPECTED_DUPLICATE", "COUNTRY_CONFLICT", "MULTIPLE_CANDIDATES", "INSUFFICIENT_QUANTITY", "PRICE_MISMATCH", "NO_CANDIDATE_IN_RANGE")
    assert diff.reason == "SUSPECTED_DUPLICATE"
    assert {"COUNTRY_CONFLICT", "MULTIPLE_CANDIDATES"} <= set(diff.flags)
    assert len(RULE_DIGEST) == 64


def test_tc28_equal_display_price_not_equal_units():
    assert units_to_display(20001) == units_to_display(20002)
    result = reconcile_rows([row(2, 5, price=20001), row(3, -2, price=20002)])
    assert result.differences[0].reason == "PRICE_MISMATCH"
    assert not result.allocations


def test_non_product_review_and_excluded_c_are_separate():
    rows = [row(2, 5), row(3, -2), row(4, -1, category="POST"), row(5, -1, kind="EXCLUDED")]
    result = reconcile_rows(rows)
    assert result.summary["product_valid_cancel"] == 1
    assert result.summary["matched"] == 1
    assert result.summary["non_product_review"] == 1
    assert result.summary["effective_cancel_count"] == 2
    assert result.summary["excluded_c_prefix"] == 1
    assert result.differences[0].reason == "NON_PRODUCT_REVIEW"
    assert result.summary["raw_c_delta"] == result.summary["coverage_delta"] == 0


def test_zero_cancel_rate_null():
    result = reconcile_rows([row(2, 5)])
    assert result.summary["match_rate"] is None
    assert result.summary["cancel_quantity"] == result.summary["cancel_amount_units"] == "0"
    assert not result.differences


@pytest.mark.parametrize("case", ["lot_qty", "lot_product", "cancel_qty", "cancel_amount", "sale_total_qty", "sale_total_amount"])
def test_tc14g_all_aggregates_int64_safe(case):
    if case == "lot_qty":
        rows = [row(2, INT64_MAX, price=1, invoice="S1"), row(3, 1, price=1, invoice="S1")]
    elif case == "lot_product":
        rows = [row(2, INT64_MAX // 2, price=2, invoice="S1"), row(3, 1, price=2, invoice="S1")]
    elif case == "cancel_qty":
        rows = [row(2, -INT64_MAX, price=1), row(3, -1, price=1)]
    elif case == "sale_total_qty":
        rows = [row(2, INT64_MAX, price=1), row(3, 1, price=1)]
    elif case == "sale_total_amount":
        rows = [row(2, INT64_MAX // 2, price=2), row(3, 1, price=2)]
    else:
        rows = [row(2, -(INT64_MAX // 2), price=2), row(3, -1, price=2)]
    with pytest.raises(NumericOverflowError):
        reconcile_rows(rows)


@pytest.mark.parametrize("value", [None, "1", 1.0, -1, 2, [], {}])
def test_raw_c_prefix_requires_boolean_or_integer_zero_one(value):
    with pytest.raises(ValueError, match="INVALID_RAW_C_PREFIX"):
        replace(row(2, -1), raw_c_prefix=value)


@pytest.mark.parametrize("value", [True, 1])
def test_excluded_formula_invoice_none_uses_saved_raw_c_prefix(value):
    formula = replace(row(2, -1, kind="EXCLUDED"), invoice=None, raw_c_prefix=value)
    result = reconcile_rows([formula])
    assert result.summary["raw_c_prefix_count"] == result.summary["excluded_c_prefix"] == 1
    assert not result.allocations and not result.differences
