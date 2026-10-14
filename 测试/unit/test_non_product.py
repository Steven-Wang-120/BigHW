import pytest

from 主程序.domain.non_product import NON_PRODUCT_CODES, cancellation_coverage
from 主程序.domain.normalization import classify_row


@pytest.mark.parametrize("code", sorted(NON_PRODUCT_CODES) + ["gift_001", "GiFt_test"])
def test_tc49_non_product_classified_before_validity(code, make_row):
    valid = classify_row(make_row({"stockcode": code}))
    assert valid.row_class == "SALE"
    assert valid.product_category != "PRODUCT"
    assert valid.matching_eligible is False
    invalid = classify_row(make_row({"stockcode": code, "price": "0"}))
    assert invalid.product_category == valid.product_category
    assert invalid.row_class == "EXCLUDED"
    assert invalid.primary_exclusion_code == "NON_POSITIVE_PRICE"


def test_tc50_a_invoice_not_a_product_code(make_row):
    adjustment = classify_row(make_row({"invoice": "A123", "stockcode": "85123A"}))
    assert adjustment.product_category == "A_BAD_DEBT_INVOICE"
    assert not adjustment.matching_eligible
    product = classify_row(make_row({"invoice": "S1", "stockcode": "ABC-VALID"}))
    assert product.product_category == "PRODUCT"
    assert product.matching_eligible


def test_tc51_cancel_scope_does_not_fake_match_rate():
    result = cancellation_coverage(12, 9, 2, 1)
    assert result["matching_denominator"] == 9
    assert result["match_rate"] is None
    assert result["matched"] is None
    with pytest.raises(ValueError, match="CANCEL_COVERAGE_MISMATCH"):
        cancellation_coverage(12, 8, 2, 1)
