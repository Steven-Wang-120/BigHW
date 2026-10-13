from decimal import Decimal, localcontext

import pytest

from 主程序.domain.money import (
    INT64_MAX, INT64_MIN, NumericOverflowError, NumericParseError,
    cancel_absolute_quantity, line_amount_units, normalize_price, parse_decimal,
    parse_quantity, units_to_display, units_to_plain_string,
)


@pytest.mark.parametrize("raw", [True, False, 2.5, 1.0], ids=["bool-true", "bool-false", "float-price", "float-integer"])
def test_CR02_no_float_or_bool(raw):
    with pytest.raises(NumericParseError):
        parse_decimal(raw)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "sNaN", None, "", "invalid"])
def test_TC05_c_nonfinite_and_invalid(raw):
    with pytest.raises(NumericParseError):
        parse_decimal(raw)


@pytest.mark.parametrize("raw", ["1e5000", "-1e5000", "1e1000000000", "9223372036854775808", "-9223372036854775809"])
def test_TC14_c_d_quantity_overflow_before_int(raw, monkeypatch):
    import 主程序.domain.money as money

    def forbidden_int(value):
        raise AssertionError("overflow must be detected before int conversion")

    monkeypatch.setattr(money, "int", forbidden_int, raising=False)
    with pytest.raises(NumericOverflowError):
        parse_quantity(raw)


def test_TC14_c_d_signed_boundaries():
    assert parse_quantity(str(INT64_MAX)) == INT64_MAX
    assert parse_quantity(str(INT64_MIN)) == INT64_MIN
    with pytest.raises(NumericOverflowError):
        cancel_absolute_quantity(INT64_MIN)


@pytest.mark.parametrize("raw", ["1e5000", "1e1000000000", "922337203685477.5808"])
def test_TC14_e_extreme_price_overflow(raw):
    with pytest.raises(NumericOverflowError):
        normalize_price(raw)


@pytest.mark.parametrize("raw", ["1e-5000", "1e-1000000000", "0.000000001"])
def test_CR05_tiny_positive_cannot_normalize_to_zero(raw):
    assert normalize_price(raw) == (None, "PRICE_PRECISION_UNSUPPORTED")


@pytest.mark.parametrize("precision", [3, 6, 28, 80])
def test_TC11_TC14_b_external_decimal_context(precision):
    with localcontext() as context:
        context.prec = precision
        assert normalize_price("2.5499999999999998") == (25500, None)
        assert normalize_price("2.550000001") == (25500, None)
        assert normalize_price("2.5500000010001") == (None, "PRICE_PRECISION_UNSUPPORTED")
        assert normalize_price("2.550000001" + "0" * 100 + "1") == (None, "PRICE_PRECISION_UNSUPPORTED")
        assert normalize_price("922337203685477.5807") == (INT64_MAX, None)
        assert units_to_display(INT64_MAX) == "922337203685477.58"
        assert units_to_display(25450) == "2.55"
        assert units_to_display(-25450) == "-2.55"
        assert context.prec == precision


def test_TC14_f_line_amount_overflow():
    with pytest.raises(NumericOverflowError):
        line_amount_units(20000, 461168601842739)
    assert units_to_plain_string(INT64_MAX) == str(INT64_MAX)
    assert normalize_price(Decimal("2.0000")) == (20000, None)
