"""领域值对象（见 文档/研究/文件结构与接口契约.md §2）。

本模块只有纯数据与纯派生，不做任何 IO。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

# --------------------------------------------------------------------------
# 枚举（字符串常量，落库时校验）
# --------------------------------------------------------------------------

CLASS_SALE = "SALE"
CLASS_CANCEL = "CANCEL"
CLASS_EXCLUDED = "EXCLUDED"
ROW_CLASSES = (CLASS_SALE, CLASS_CANCEL, CLASS_EXCLUDED)

#: CR-02 排除主原因优先级：首个命中即为主原因，其余只作次要 flag。
EXCLUSION_PRIORITY: tuple[str, ...] = (
    "EMPTY_ROW",
    "FORMULA_UNSUPPORTED",
    "MISSING_REQUIRED",
    "INVALID_NUMBER_OR_DATE",
    "NON_INTEGER_QUANTITY",
    "NON_POSITIVE_PRICE",
    "PRICE_PRECISION_UNSUPPORTED",
    "ZERO_QUANTITY",
    "SIGN_CONFLICT",
)

#: 次要 flag（不增加排除计数）
FLAG_MISSING_CUSTOMER = "MISSING_CUSTOMER"
FLAG_MISSING_DESCRIPTION = "MISSING_DESCRIPTION"
FLAG_MISSING_COUNTRY = "MISSING_COUNTRY"
FLAG_SUSPECTED_DUPLICATE = "SUSPECTED_DUPLICATE"
FLAG_NUMERIC_OVERFLOW = "NUMERIC_OVERFLOW"
FLAG_TRUNCATED_INVOICE = "TRUNCATED_INVOICE_RANGE"

#: 规范八列字段名（顺序即快照 CSV 顺序）
SOURCE_FIELDS: tuple[str, ...] = (
    "invoice",
    "stockcode",
    "description",
    "quantity",
    "invoicedate_serial",
    "price",
    "customer_id",
    "country",
)

#: 匹配键所需五项必需字段（CR-02）
REQUIRED_FIELDS: tuple[str, ...] = (
    "invoice",
    "stockcode",
    "quantity",
    "invoicedate_serial",
    "price",
)


# --------------------------------------------------------------------------
# 源行身份
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRowRef:
    """全局行身份 = 源文件 SHA256 + 工作表名 + Excel 原始行号。

    不能用发票号替代（同一发票有多行）。
    """

    source_sha256: str
    sheet: str
    source_row: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", self.source_sha256) is None:
            raise ValueError("source_sha256 must contain exactly 64 hexadecimal characters")
        if not isinstance(self.sheet, str) or not self.sheet:
            raise ValueError("sheet must be nonempty text")
        if type(self.source_row) is not int or self.source_row < 2:
            raise ValueError("source_row must be an integer >= 2, not bool")

    def canonical(self) -> str:
        return f"{self.source_sha256.lower()}|{self.sheet}|{self.source_row}"

    def row_id(self) -> str:
        """稳定行 id：规范串的 SHA256 前 32 位十六进制。"""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# 行裁决
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawRow:
    """快照中一条源位置的原始值（未裁决）。空行哨兵的全部字段为 None/空白。"""

    ref: SourceRowRef
    values: Mapping[str, str | int | Decimal | None]
    has_formula: bool = False
    cell_types: Mapping[str, str] = field(default_factory=dict)
    excel_epoch: str = "1900"

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "cell_types", MappingProxyType(dict(self.cell_types)))

    def raw(self, name: str) -> str | int | Decimal | None:
        return self.values.get(name)

    def is_blank(self) -> bool:
        """CR-01：八字段均无值或仅空白 → 完全空白行。"""
        for name in SOURCE_FIELDS:
            value = self.values.get(name)
            if value is not None and str(value).strip() != "":
                return False
        return True


@dataclass(frozen=True, slots=True)
class RowVerdict:
    """一行有且仅有一种主要裁决（CR-04）。"""

    ref: SourceRowRef
    row_class: str
    primary_exclusion_code: str | None
    secondary_flags: tuple[str, ...]
    # 规范化后的值；未通过裁决的字段可能为 None
    invoice: str | None
    stock_code: str | None
    customer_key: str | None
    country: str | None
    description: str | None
    quantity: int | None
    price_units: int | None
    amount_units: int | None
    invoice_datetime: datetime | None
    duplicate_key: tuple[str, ...] = field(default=(), repr=False)
    product_category: str = "PRODUCT"
    matching_eligible: bool = False

    @property
    def is_excluded(self) -> bool:
        return self.row_class == CLASS_EXCLUDED

    def flag_set(self) -> frozenset[str]:
        return frozenset(self.secondary_flags)


# --------------------------------------------------------------------------
# 守恒计数（R1 / CR-09）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConservationCounts:
    """两层守恒计数。

    R1：``original_total == outside_count + selected_count``
        ``selected_count == sale_count + cancel_count + excluded_count``
    excluded 已是第三类，不能再与 sale/cancel 双加。
    """

    original_total: int
    outside_count: int
    selected_count: int
    sale_count: int
    cancel_count: int
    excluded_count: int
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_counts", MappingProxyType(dict(self.reason_counts)))

    @property
    def delta_original(self) -> int:
        return self.original_total - (self.outside_count + self.selected_count)

    @property
    def delta_selected(self) -> int:
        return self.selected_count - (
            self.sale_count + self.cancel_count + self.excluded_count
        )

    @property
    def balanced(self) -> bool:
        return self.delta_original == 0 and self.delta_selected == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "original_total": self.original_total,
            "outside_count": self.outside_count,
            "selected_count": self.selected_count,
            "sale_count": self.sale_count,
            "cancel_count": self.cancel_count,
            "excluded_count": self.excluded_count,
            "reason_counts": dict(sorted(self.reason_counts.items())),
            "delta_original": self.delta_original,
            "delta_selected": self.delta_selected,
        }


class ConservationError(ValueError):
    """两层守恒差不为 0，禁止发布结果（TC-22）。"""

    def __init__(self, counts: ConservationCounts) -> None:
        super().__init__(
            f"行数守恒失败: delta_original={counts.delta_original}, "
            f"delta_selected={counts.delta_selected}"
        )
        self.counts = counts
