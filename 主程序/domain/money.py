"""金额与数值口径（CR-05 / CR-02）。

设计要点：
  * 一律以源 XML 的十进制字符串构造 ``Decimal``，绝不经过 ``float``。
  * 规范价以 £0.0001 为最小整数单位（``PRICE_SCALE = 10000``）。
  * 四位 ROUND_HALF_UP 候选值与源值的尾差必须 ≤ 1e-9 才接受，
    恰等接受、略超拒绝 PRICE_PRECISION_UNSUPPORTED（TC-14）。
  * 所有落库整数（quantity / price_units / line_amount_units / 取消绝对量）
    必须落在有符号 64 位范围内，越界不截断、不崩溃，
    统一按 CR-02 归入 INVALID_NUMBER_OR_DATE 并附 NUMERIC_OVERFLOW。
"""

from __future__ import annotations

from decimal import Context, Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP, localcontext

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PRICE_SCALE = 10000  # £0.0001 为一个整数单位
PRICE_QUANT = Decimal("0.0001")  # 四位小数量化步长
TAIL_EPSILON = Decimal("1e-9")  # 尾差阈值（英镑），TC-14 明确输入
DISPLAY_QUANT = Decimal("0.01")  # 展示两位

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

#: 非有限字面量：NaN / ±Infinity 一律主排除 INVALID_NUMBER_OR_DATE
_NON_FINITE_LITERALS = frozenset(
    {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }
)


class NumericParseError(ValueError):
    """源值不可解析或非有限。主排除码 INVALID_NUMBER_OR_DATE。"""

    def __init__(self, raw: object, reason: str) -> None:
        super().__init__(f"{reason}: {raw!r}")
        self.raw = raw
        self.reason = reason


class NumericOverflowError(ValueError):
    """数值可解析但超出所需 64 位范围。主码 INVALID_NUMBER_OR_DATE + NUMERIC_OVERFLOW。"""

    def __init__(self, field: str, value: object) -> None:
        super().__init__(f"{field} 超出 64 位范围: {value!r}")
        self.field = field
        self.value = value


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_decimal(raw: object) -> Decimal:
    """把源值解析为有限 ``Decimal``；NaN / ±Infinity / 空 / 非数一律抛错。"""
    if raw is None:
        raise NumericParseError(raw, "missing")
    if isinstance(raw, (float, bool)) or not isinstance(raw, (str, int, Decimal)):
        raise NumericParseError(raw, "unsupported_type")
    text = str(raw).strip()
    if not text:
        raise NumericParseError(raw, "blank")
    if text.lower() in _NON_FINITE_LITERALS:
        raise NumericParseError(raw, "non_finite")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - 防御
        raise NumericParseError(raw, "unparsable") from exc
    if not value.is_finite():
        raise NumericParseError(raw, "non_finite")
    return value


def is_int64(value: int) -> bool:
    return INT64_MIN <= value <= INT64_MAX


def require_int64(field: str, value: int) -> int:
    if not is_int64(value):
        raise NumericOverflowError(field, value)
    return value


# --------------------------------------------------------------------------
# 数量
# --------------------------------------------------------------------------


def parse_quantity(raw: object) -> int:
    """解析整数数量；非整数抛 NumericParseError('non_integer')。"""
    value = parse_decimal(raw)
    if value < INT64_MIN or value > INT64_MAX:
        raise NumericOverflowError("quantity", value)
    if value != value.to_integral_value():
        raise NumericParseError(raw, "non_integer")
    return int(value)


# --------------------------------------------------------------------------
# 单价
# --------------------------------------------------------------------------


def normalize_price(raw: object) -> tuple[int | None, str | None]:
    """把源单价规范为整数单位。

    返回 ``(price_units, exclusion_code)``；``exclusion_code`` 非空时
    ``price_units`` 为 ``None``。判定次序严格按 CR-02：
    先 NON_POSITIVE_PRICE，再 PRICE_PRECISION_UNSUPPORTED。
    """
    value = parse_decimal(raw)  # 可能抛 NumericParseError
    if value <= 0:
        return None, "NON_POSITIVE_PRICE"
    # Bound exponents before quantize or int conversion; a zero candidate is not a price.
    if value >= Decimal("922337203685477.58075"):
        raise NumericOverflowError("price_units", value)
    if value < Decimal("0.00005"):
        return None, "PRICE_PRECISION_UNSUPPORTED"
    with localcontext(Context(prec=32, rounding=ROUND_HALF_UP)):
        try:
            candidate = value.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
            units = int(candidate * PRICE_SCALE)
            require_int64("price_units", units)
            # Compare exact bounds rather than a precision-rounded subtraction.
            lower = candidate - TAIL_EPSILON
            upper = candidate + TAIL_EPSILON
            if value < lower or value > upper:
                return None, "PRICE_PRECISION_UNSUPPORTED"
        except DecimalException as exc:
            raise NumericOverflowError("price_units", value) from exc
    return units, None


def line_amount_units(price_units: int, quantity: int) -> int:
    """整数行金额 = 规范单价单位 × 整数数量。越界抛 NumericOverflowError。"""
    return require_int64("line_amount_units", price_units * quantity)


def cancel_absolute_quantity(quantity: int) -> int:
    """取消行绝对量。int64 最小值取绝对值会越界，须显式拒绝。"""
    return require_int64("cancel_absolute_quantity", abs(quantity))


def units_to_display(units: int) -> str:
    """把整数金额单位渲染为两位展示文本（ROUND_HALF_UP，仅影响显示）。"""
    cents = (abs(units) + 50) // 100
    sign = "-" if units < 0 else ""
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def units_to_plain_string(units: int) -> str:
    """JSON 中的整数金额字符串（避免 JS 超过 2^53）。"""
    return str(units)
