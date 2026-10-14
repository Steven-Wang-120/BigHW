"""行规范化与裁决（CR-01 – CR-08）。

本模块是纯函数层：输入 :class:`RawRow`（快照原值），输出 :class:`RowVerdict`。
不做任何 IO，不依赖数据库。

CR-02 的排除主原因是**优先级**而非多次计数：一条行只产生一个主原因，
其余命中项降级为次要 flag。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Context, Decimal, DecimalException, ROUND_DOWN, ROUND_HALF_UP, localcontext

from 主程序.domain.models import (
    CLASS_CANCEL,
    CLASS_EXCLUDED,
    CLASS_SALE,
    EXCLUSION_PRIORITY,
    FLAG_MISSING_COUNTRY,
    FLAG_MISSING_CUSTOMER,
    FLAG_MISSING_DESCRIPTION,
    FLAG_NUMERIC_OVERFLOW,
    FLAG_SUSPECTED_DUPLICATE,
    REQUIRED_FIELDS,
    RawRow,
    RowVerdict,
)
from 主程序.domain.non_product import product_category
from 主程序.domain.money import (
    NumericOverflowError,
    NumericParseError,
    cancel_absolute_quantity,
    line_amount_units,
    normalize_price,
    parse_decimal,
    parse_quantity,
)

# --------------------------------------------------------------------------
# 日期（CR-08）
# --------------------------------------------------------------------------

#: Excel 1900 日期系统基准（现代日期用 1899-12-30 为第 0 天）
EXCEL_EPOCH_1900 = datetime(1899, 12, 30)


def serial_to_datetime(serial: Decimal, excel_epoch: str = "1900") -> datetime:
    """Decode source-local time to microseconds; reject Excel's fictional day 60."""
    if excel_epoch not in {"1900", "1904"}:
        raise ValueError("unknown Excel date epoch")
    if not serial.is_finite() or serial < 0 or serial >= Decimal("3000000"):
        raise ValueError("Excel date serial outside supported datetime range")
    if excel_epoch == "1900" and Decimal("60") <= serial < Decimal("61"):
        raise ValueError("Excel serial 60 represents the invalid date 1900-02-29")
    whole_days = int(serial.to_integral_value(rounding=ROUND_DOWN))
    with localcontext(Context(prec=32, rounding=ROUND_HALF_UP)):
        fraction = serial - Decimal(whole_days)
        microseconds = int((fraction * 86400000000).to_integral_value(rounding=ROUND_HALF_UP))
    epoch = datetime(1904, 1, 1) if excel_epoch == "1904" else EXCEL_EPOCH_1900
    leap_adjustment = 1 if excel_epoch == "1900" and whole_days < 60 else 0
    return epoch + timedelta(days=whole_days + leap_adjustment, microseconds=microseconds)


# --------------------------------------------------------------------------
# 标识符文本化（CR-03）
# --------------------------------------------------------------------------


def _text(raw: object) -> str | None:
    """仅去首尾空白；不做大小写或补零改动。"""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def normalize_invoice(raw: object) -> str | None:
    """单号按标识符文本保留；C 前缀大小写不敏感只在判定时使用。"""
    return _text(raw)


def normalize_stock_code(raw: object) -> str | None:
    """商品码按标识符文本保留，合法字母码不强制转数、不改内部大小写。"""
    return _text(raw)


def normalize_customer_id(raw: object, cell_type: str = "s") -> str | None:
    """Only numeric cell metadata permits numeric identifier normalization."""
    text = _text(raw)
    if text is None or cell_type != "n":
        return text
    try:
        value = parse_decimal(raw)
    except NumericParseError:
        return text
    if value != value.to_integral_value() or value.adjusted() > 1000:
        return text
    return format(value.to_integral_value(), "f")


def normalize_country(raw: object) -> str | None:
    return _text(raw)


def normalize_description(raw: object) -> str | None:
    """描述仅用于展示，**从不**作为匹配键（CR-06）。"""
    return _text(raw)


def is_cancel_invoice(invoice: str | None) -> bool:
    return bool(invoice) and invoice[:1].upper() == "C"


# --------------------------------------------------------------------------
# 单项解析结果
# --------------------------------------------------------------------------


class _Parsed:
    """三态解析结果：ok / unparsable / overflow / 领域码。"""

    __slots__ = ("status", "value", "code")

    def __init__(self, status: str, value: object = None, code: str | None = None) -> None:
        self.status = status
        self.value = value
        self.code = code


def _parse_quantity_field(raw: object) -> _Parsed:
    try:
        return _Parsed("ok", parse_quantity(raw))
    except NumericParseError as exc:
        if exc.reason == "non_integer":
            return _Parsed("non_integer")
        return _Parsed("unparsable")
    except NumericOverflowError:
        return _Parsed("overflow")


def _parse_date_field(raw: object, excel_epoch: str = "1900") -> _Parsed:
    try:
        value = parse_decimal(raw)
    except NumericParseError:
        return _Parsed("unparsable")
    except NumericOverflowError:  # pragma: no cover - parse_decimal 不抛此错
        return _Parsed("overflow")
    try:
        return _Parsed("ok", serial_to_datetime(value, excel_epoch))
    except (OverflowError, ValueError, DecimalException):
        return _Parsed("unparsable")


def _parse_price_field(raw: object) -> _Parsed:
    try:
        units, code = normalize_price(raw)
    except NumericParseError:
        return _Parsed("unparsable")
    except NumericOverflowError:
        return _Parsed("overflow")
    if code == "NON_POSITIVE_PRICE":
        return _Parsed("nonpositive")
    if code == "PRICE_PRECISION_UNSUPPORTED":
        return _Parsed("precision")
    return _Parsed("ok", units)


# --------------------------------------------------------------------------
# 行裁决
# --------------------------------------------------------------------------


def classify_row(row: RawRow) -> RowVerdict:
    """Collect independent reasons first, then select the single CR-02 primary."""
    reasons: set[str] = set()
    flags: list[str] = []
    formula_fields = {name for name, kind in row.cell_types.items() if kind == "f"}
    formula_fields.update(name for name in REQUIRED_FIELDS if isinstance(row.raw(name), str) and row.raw(name).lstrip().startswith("="))
    if row.is_blank() and not (row.has_formula or formula_fields):
        reasons.add("EMPTY_ROW")
    if row.has_formula or formula_fields:
        reasons.add("FORMULA_UNSUPPORTED")
    if any(_text(row.raw(name)) is None for name in REQUIRED_FIELDS):
        reasons.add("MISSING_REQUIRED")

    def available(name: str) -> bool:
        return name not in formula_fields and _text(row.raw(name)) is not None

    qty = _parse_quantity_field(row.raw("quantity")) if available("quantity") else _Parsed("missing")
    date = _parse_date_field(row.raw("invoicedate_serial"), row.excel_epoch) if available("invoicedate_serial") else _Parsed("missing")
    price = _parse_price_field(row.raw("price")) if available("price") else _Parsed("missing")
    for parsed in (qty, date, price):
        if parsed.status in {"unparsable", "overflow"}:
            reasons.add("INVALID_NUMBER_OR_DATE")
        if parsed.status == "overflow":
            flags.append(FLAG_NUMERIC_OVERFLOW)
    if qty.status == "non_integer":
        reasons.add("NON_INTEGER_QUANTITY")
    if price.status == "nonpositive":
        reasons.add("NON_POSITIVE_PRICE")
    if price.status == "precision":
        reasons.add("PRICE_PRECISION_UNSUPPORTED")

    invoice = normalize_invoice(row.raw("invoice")) if available("invoice") else None
    stock_code = normalize_stock_code(row.raw("stockcode")) if available("stockcode") else None
    country = normalize_country(row.raw("country")) if available("country") else None
    description = normalize_description(row.raw("description")) if available("description") else None
    customer_key = normalize_customer_id(row.raw("customer_id"), row.cell_types.get("customer_id", "s")) if available("customer_id") else None

    quantity = qty.value if qty.status == "ok" else None
    price_units = price.value if price.status == "ok" else None
    invoice_dt = date.value if date.status == "ok" else None
    amount_units = None
    if quantity is not None:
        if quantity == 0:
            reasons.add("ZERO_QUANTITY")
        elif invoice is not None and is_cancel_invoice(invoice) != (quantity < 0):
            reasons.add("SIGN_CONFLICT")
        try:
            if quantity < 0:
                cancel_absolute_quantity(quantity)
            if price_units is not None:
                amount_units = line_amount_units(price_units, quantity)
        except NumericOverflowError:
            reasons.add("INVALID_NUMBER_OR_DATE")
            flags.append(FLAG_NUMERIC_OVERFLOW)

    if customer_key is None:
        flags.append(FLAG_MISSING_CUSTOMER)
    if description is None:
        flags.append(FLAG_MISSING_DESCRIPTION)
    if country is None:
        flags.append(FLAG_MISSING_COUNTRY)
    category = product_category(row.raw("invoice"), row.raw("stockcode"))
    if category != "PRODUCT":
        flags.append("NON_PRODUCT")
    ordered = [code for code in EXCLUSION_PRIORITY if code in reasons]
    primary = ordered[0] if ordered else None
    flags = ordered[1:] + flags
    row_class = CLASS_EXCLUDED if primary else CLASS_CANCEL if quantity < 0 else CLASS_SALE
    duplicate_key = ()
    if primary is None:
        duplicate_key = (invoice or "", stock_code or "", description or "", str(quantity),
                         invoice_dt.isoformat(), str(price_units), customer_key or "", country or "")
    return RowVerdict(
        ref=row.ref, row_class=row_class, primary_exclusion_code=primary,
        secondary_flags=tuple(dict.fromkeys(flags)), invoice=invoice, stock_code=stock_code,
        customer_key=customer_key, country=country, description=description,
        quantity=quantity, price_units=price_units, amount_units=amount_units,
        invoice_datetime=invoice_dt, duplicate_key=duplicate_key,
        product_category=category, matching_eligible=primary is None and category == "PRODUCT",
    )


# --------------------------------------------------------------------------
# 疑似重复（CR-07）
# --------------------------------------------------------------------------


def mark_suspected_duplicates(verdicts: list[RowVerdict]) -> list[RowVerdict]:
    """不同源行完整八字段规范值相同 → 标 SUSPECTED_DUPLICATE。

    行身份与原行数保留，**不静默去重**；flag 只叠加，不改变主裁决。
    """
    buckets: dict[tuple[str, ...], list[int]] = {}
    for index, verdict in enumerate(verdicts):
        key = verdict.duplicate_key
        if key:
            buckets.setdefault(key, []).append(index)

    flagged: dict[int, None] = {}
    for indexes in buckets.values():
        if len({verdicts[index].ref.canonical() for index in indexes}) > 1:
            for index in indexes:
                flagged[index] = None

    if not flagged:
        return list(verdicts)

    result: list[RowVerdict] = []
    for index, verdict in enumerate(verdicts):
        if index in flagged and FLAG_SUSPECTED_DUPLICATE not in verdict.secondary_flags:
            flags = tuple(verdict.secondary_flags) + (FLAG_SUSPECTED_DUPLICATE,)
            result.append(replace(verdict, secondary_flags=flags))
        else:
            result.append(verdict)
    return result
