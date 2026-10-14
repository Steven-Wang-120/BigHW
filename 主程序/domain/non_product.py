"""Teacher clarification categories, orthogonal to row validity (CR-11)."""

NORMALIZATION_VERSION = "normalization-v0.2-teacher"
NON_PRODUCT_CODES = frozenset({"POST", "DOT", "M", "C2", "D", "S", "BANK CHARGES", "AMAZONFEE", "CRUK"})


def product_category(invoice: object, stock_code: object) -> str:
    invoice_text = str(invoice or "").strip()
    code = str(stock_code or "").strip()
    # A-prefixed INVOICES are bad-debt adjustment orders. A product code alone is not evidence.
    if invoice_text[:1].upper() == "A":
        return "A_BAD_DEBT_INVOICE"
    if code.upper() in NON_PRODUCT_CODES:
        return code.upper()
    if code.lower().startswith("gift_"):
        return "GIFT_VOUCHER"
    return "PRODUCT"


def cancellation_coverage(raw_cancel: int, product_valid: int, non_product_valid: int, excluded: int) -> dict:
    delta = raw_cancel - product_valid - non_product_valid - excluded
    if delta:
        raise ValueError("CANCEL_COVERAGE_MISMATCH")
    return {"raw_c_prefix_count": raw_cancel, "product_valid_cancel": product_valid,
            "non_product_valid_cancel": non_product_valid, "excluded_c_prefix": excluded,
            "delta": delta, "matching_denominator": product_valid,
            "matching_stage": "NOT_IMPLEMENTED", "matched": None, "suspected": None,
            "no_original": None, "match_rate": None}
