"""D-03 conservative matching: pure deterministic rules, no IO."""

from collections import defaultdict, Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import hashlib

from 主程序.domain.models import SourceRowRef
from 主程序.domain.money import require_int64
from 主程序.domain.serialization import json_text

REASON_PRIORITY = ("MISSING_CUSTOMER", "SUSPECTED_DUPLICATE", "COUNTRY_CONFLICT", "MULTIPLE_CANDIDATES",
                   "INSUFFICIENT_QUANTITY", "PRICE_MISMATCH", "NO_CANDIDATE_IN_RANGE")
RULE = {"version": "conservative-v1-90d", "window_seconds": 90 * 86400, "inclusive": True,
        "lot_key": ["invoice", "customer", "stock", "price_units", "invoice_time"],
        "cancel_order": ["invoice_time", "sheet", "source_row", "source_sha"],
        "candidate_policy": "window first; never filter remaining/country/duplicate to manufacture uniqueness",
        "reason_priority": list(REASON_PRIORITY), "non_product_policy": "NON_PRODUCT_REVIEW"}
RULE_DIGEST = hashlib.sha256(json_text(RULE).encode()).hexdigest()
NO_CANDIDATE_MESSAGE = "当前范围未找到候选，范围外可能存在"


class MatchingCoverageError(ValueError):
    pass


@dataclass(frozen=True)
class MatchingRow:
    id: str
    ref: SourceRowRef
    row_class: str
    invoice: str | None
    customer: str | None
    stock: str | None
    price_units: int | None
    quantity: int | None
    amount_units: int | None
    invoice_time: datetime | None
    country: str | None
    flags: tuple[str, ...]
    category: str
    matching_eligible: bool
    raw_c_prefix: bool | int

    def __post_init__(self):
        if type(self.raw_c_prefix) not in (bool, int) or self.raw_c_prefix not in (0, 1):
            raise ValueError("INVALID_RAW_C_PREFIX")


@dataclass
class SaleLot:
    id: str
    invoice: str
    customer: str | None
    stock: str
    price_units: int
    invoice_time: datetime
    quantity: int
    remaining: int
    amount_units: int
    row_ids: tuple[str, ...]
    source_refs: tuple[SourceRowRef, ...]
    countries: tuple[str, ...]
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Allocation:
    cancel_id: str
    lot_id: str
    quantity: int
    amount_units: int


@dataclass(frozen=True)
class Difference:
    cancel_id: str
    reason: str
    flags: tuple[str, ...]
    candidates: tuple[dict, ...]
    quantity: int
    amount_units: int
    message: str


@dataclass(frozen=True)
class MatchingResult:
    lots: tuple[SaleLot, ...]
    allocations: tuple[Allocation, ...]
    differences: tuple[Difference, ...]
    summary: dict


def checked(value, field):
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_MATCHING_INPUT")
    return require_int64(field, value)


def row_order(row):
    return row.invoice_time, row.ref.sheet, row.ref.source_row, row.ref.source_sha256


def validate_row(row):
    if row.invoice_time is None or row.invoice_time.tzinfo is not None or not row.invoice or not row.stock:
        raise ValueError("INVALID_MATCHING_INPUT")
    checked(row.price_units, "price_units")
    if not row.price_units or type(row.quantity) is not int:
        raise ValueError("INVALID_MATCHING_INPUT")
    require_int64("quantity", row.quantity)
    if (row.row_class == "SALE" and row.quantity <= 0) or (row.row_class == "CANCEL" and row.quantity >= 0):
        raise ValueError("INVALID_MATCHING_INPUT")
    checked(abs(row.quantity), "absolute_quantity")
    amount = require_int64("row_amount_units", row.quantity * row.price_units)
    if type(row.amount_units) is not int or row.amount_units != amount:
        raise ValueError("INVALID_MATCHING_INPUT")


def reconcile_rows(rows):
    """All effective cancellations get exactly one allocation or review difference."""
    rows = tuple(rows)
    if len({r.id for r in rows}) != len(rows) or len({r.ref for r in rows}) != len(rows):
        raise ValueError("INVALID_MATCHING_INPUT")
    sales = [r for r in rows if r.row_class == "SALE" and r.matching_eligible and r.category == "PRODUCT"]
    cancels = [r for r in rows if r.row_class == "CANCEL"]
    for row in sales + cancels:
        validate_row(row)
        if row.row_class == "CANCEL" and row.category == "PRODUCT" and not row.matching_eligible:
            raise ValueError("INVALID_MATCHING_INPUT")
    grouped = defaultdict(list)
    for row in sales:
        grouped[(row.invoice, row.customer, row.stock, row.price_units, row.invoice_time)].append(row)
    lots = []
    by_key = defaultdict(list)
    for key, members in grouped.items():
        members.sort(key=row_order)
        invoice, customer, stock, price, time = key
        quantity = checked(sum(r.quantity for r in members), "lot_quantity")
        amount = checked(price * quantity, "lot_amount_units")
        lot_id = hashlib.sha256(json_text([invoice, customer, stock, price, time.isoformat()]).encode()).hexdigest()
        countries = tuple(sorted({r.country for r in members if r.country}))
        flags = {flag for row in members for flag in row.flags}
        if len(countries) > 1:
            flags.add("COUNTRY_CONFLICT")
        lot = SaleLot(lot_id, invoice, customer, stock, price, time, quantity, quantity, amount,
                      tuple(r.id for r in members), tuple(r.ref for r in members), countries, tuple(sorted(flags)))
        lots.append(lot)
        if customer is not None:
            by_key[(customer, stock)].append(lot)
    lots.sort(key=lambda lot: lot.id)
    total_sale_quantity = checked(sum(l.quantity for l in lots), "total_sale_quantity")
    total_sale_amount = checked(sum(l.amount_units for l in lots), "total_sale_amount_units")
    for values in by_key.values():
        values.sort(key=lambda lot: lot.id)
    allocations, differences = [], []
    matched = suspected = no_original = non_product = 0
    cancel_quantity = cancel_amount = allocated_quantity = allocated_amount = unmatched_quantity = unmatched_amount = 0
    for cancel in sorted(cancels, key=row_order):
        quantity = checked(abs(cancel.quantity), "cancel_quantity")
        amount = checked(abs(cancel.amount_units), "cancel_amount_units")
        cancel_quantity = checked(cancel_quantity + quantity, "total_cancel_quantity")
        cancel_amount = checked(cancel_amount + amount, "total_cancel_amount_units")
        flags = set(cancel.flags)
        evidence = []
        if cancel.category != "PRODUCT":
            reason = "NON_PRODUCT_REVIEW"
            non_product += 1
        else:
            window = [lot for lot in by_key.get((cancel.customer, cancel.stock), ())
                      if timedelta(0) <= cancel.invoice_time - lot.invoice_time <= timedelta(seconds=RULE["window_seconds"])]
            exact = [lot for lot in window if lot.price_units == cancel.price_units]
            duplicate = "SUSPECTED_DUPLICATE" in flags or any("SUSPECTED_DUPLICATE" in lot.flags for lot in window)
            conflict = any(len(lot.countries) > 1 or (cancel.country and any(c != cancel.country for c in lot.countries)) for lot in window)
            conditions = {"MISSING_CUSTOMER": cancel.customer is None, "SUSPECTED_DUPLICATE": duplicate,
                          "COUNTRY_CONFLICT": conflict, "MULTIPLE_CANDIDATES": len(exact) > 1,
                          "INSUFFICIENT_QUANTITY": len(exact) == 1 and exact[0].remaining < quantity,
                          "PRICE_MISMATCH": bool(window) and not exact,
                          "NO_CANDIDATE_IN_RANGE": not window}
            for lot in window:
                flags.update(lot.flags)
                evidence.append({"lot_id": lot.id, "row_ids": list(lot.row_ids),
                                 "source_refs": [r.canonical() for r in lot.source_refs],
                                 "price_units": str(lot.price_units), "remaining_quantity": str(lot.remaining),
                                 "invoice_time": lot.invoice_time.isoformat(), "countries": list(lot.countries),
                                 "flags": list(lot.flags), "exact_price": lot.price_units == cancel.price_units})
            flags.update(name for name, hit in conditions.items() if hit)
            reason = next((name for name in REASON_PRIORITY if conditions[name]), None)
            if reason is None:
                lot = exact[0]
                lot.remaining -= quantity
                allocations.append(Allocation(cancel.id, lot.id, quantity, amount))
                matched += 1
                allocated_quantity = checked(allocated_quantity + quantity, "allocated_quantity")
                allocated_amount = checked(allocated_amount + amount, "allocated_amount_units")
                continue
            if reason == "NO_CANDIDATE_IN_RANGE":
                no_original += 1
            else:
                suspected += 1
        differences.append(Difference(cancel.id, reason, tuple(sorted(flags)), tuple(evidence), quantity, amount,
                                      NO_CANDIDATE_MESSAGE if reason == "NO_CANDIDATE_IN_RANGE" else "需要人工核查：" + reason))
        unmatched_quantity = checked(unmatched_quantity + quantity, "unmatched_quantity")
        unmatched_amount = checked(unmatched_amount + amount, "unmatched_amount_units")
    product_cancel = sum(r.category == "PRODUCT" for r in cancels)
    excluded_c = sum(r.row_class == "EXCLUDED" and r.raw_c_prefix for r in rows)
    raw_c = sum(r.raw_c_prefix for r in rows)
    if raw_c != product_cancel + non_product + excluded_c or matched + suspected + no_original != product_cancel:
        raise MatchingCoverageError("CANCEL_COVERAGE_MISMATCH")
    with localcontext() as context:
        context.prec = 28
        rate = str(Decimal(matched) / Decimal(product_cancel)) if product_cancel else None
    summary = {"product_valid_cancel": product_cancel, "matched": matched, "suspected": suspected,
               "no_original": no_original, "match_rate": rate, "non_product_review": non_product,
               "effective_cancel_count": len(cancels), "suspected_manual": suspected + non_product,
               "coverage_delta": len(cancels) - matched - suspected - no_original - non_product,
               "raw_c_prefix_count": raw_c, "excluded_c_prefix": excluded_c,
               "raw_c_delta": raw_c - product_cancel - non_product - excluded_c,
               "product_delta": product_cancel - matched - suspected - no_original,
               "difference_count": len(differences), "lot_count": len(lots), "allocation_count": len(allocations),
               "difference_reasons": dict(sorted(Counter(d.reason for d in differences).items())),
               "sale_quantity": str(total_sale_quantity), "sale_amount_units": str(total_sale_amount),
               "remaining_sale_quantity": str(checked(sum(l.remaining for l in lots), "remaining_sale_quantity")),
               "cancel_quantity": str(cancel_quantity), "allocated_quantity": str(allocated_quantity),
               "unmatched_quantity": str(unmatched_quantity), "quantity_delta": str(cancel_quantity - allocated_quantity - unmatched_quantity),
               "cancel_amount_units": str(cancel_amount), "allocated_amount_units": str(allocated_amount),
               "unmatched_amount_units": str(unmatched_amount), "amount_delta_units": str(cancel_amount - allocated_amount - unmatched_amount),
               "no_original_message": NO_CANDIDATE_MESSAGE, "matching_completed": True}
    return MatchingResult(tuple(lots), tuple(allocations), tuple(differences), summary)
