"""B02 complete streaming scan, deterministic raw snapshot and atomic publication."""

from collections import Counter
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import zipfile

from 主程序.domain.models import SOURCE_FIELDS
from 主程序.domain.serialization import json_text
from 主程序.domain.normalization import classify_row, normalize_invoice, serial_to_datetime
from 主程序.domain.non_product import NORMALIZATION_VERSION
from 主程序.repositories.xlsx_reader import WorkbookReader, InputError, SHEETS, HEADERS, blank_row

SOURCE_SHA = "bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980"
ZIP_SHA = "572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb"
SCAN_VERSION = "zip-xml-v1"
SELECTION_VERSION = "source-position-v1"
CSV_COLUMNS = ("source_sha256", "sheet", "source_row", *SOURCE_FIELDS, "cell_types", "cells", "has_formula", "excel_epoch")


def sha256(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise InputError("INPUT_UNAVAILABLE", 400) from exc
    return digest.hexdigest()


def validate_source(source: Path, archive: Path | None, expected_sha: str = SOURCE_SHA):
    if sha256(source) != expected_sha.lower():
        raise InputError("SOURCE_HASH_MISMATCH")
    if archive is not None:
        if sha256(archive) != ZIP_SHA:
            raise InputError("SOURCE_HASH_MISMATCH")
        with zipfile.ZipFile(archive) as z:
            if z.namelist() != ["online_retail_II.xlsx"]:
                raise InputError("SOURCE_CONTAINER_MISMATCH")
            with z.open("online_retail_II.xlsx") as stream:
                d = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    d.update(block)
            if d.hexdigest() != expected_sha:
                raise InputError("SOURCE_HASH_MISMATCH")


class Aggregate:
    def __init__(self):
        self.missing = Counter()
        self.types = {f: Counter() for f in SOURCE_FIELDS}
        self.categories = Counter()
        self.classes = Counter()
        self.reasons = Counter()
        self.cross = Counter()
        self.numeric = {f: Counter() for f in ("quantity", "price", "customer_id")}
        self.precision = Counter()
        self.date_min = self.date_max = None
        self.price_min = self.price_max = None
        self.max_tail = Decimal(0)
        self.formulas = 0
        self.empty = 0
        self.raw_cancel = 0
        self.a_invoice = self.a_stock = self.a_stock_product = 0

    def add(self, row, sha, sheet, epoch):
        raw = row.raw(sha, sheet, epoch)
        verdict = classify_row(raw)
        self.empty += raw.is_blank() and not row.has_formula
        self.formulas += row.has_formula
        self.categories[verdict.product_category] += 1
        self.classes[verdict.row_class] += 1
        if verdict.primary_exclusion_code:
            self.reasons[verdict.primary_exclusion_code] += 1
        self.cross[f"{verdict.product_category}|{verdict.row_class}"] += 1
        inv = normalize_invoice(raw.raw("invoice")) or ""
        self.raw_cancel += inv[:1].upper() == "C"
        self.a_invoice += inv[:1].upper() == "A"
        stock = str(raw.raw("stockcode") or "").strip()
        self.a_stock += stock[:1].upper() == "A"
        self.a_stock_product += stock[:1].upper() == "A" and verdict.product_category == "PRODUCT"
        for f in SOURCE_FIELDS:
            self.types[f][row.types[f]] += 1
            self.missing[f] += raw.raw(f) is None or str(raw.raw(f)).strip() == ""
        for f in self.numeric:
            try:
                value = Decimal(str(raw.raw(f)))
                if not value.is_finite():
                    self.numeric[f]["non_finite"] += 1
                    continue
                self.numeric[f]["finite"] += 1
                self.numeric[f]["non_integer"] += value != value.to_integral_value()
                self.numeric[f]["negative"] += value < 0
                self.numeric[f]["zero"] += value == 0
                if f == "price":
                    self.precision[str(max(0, -value.normalize().as_tuple().exponent))] += 1
                    self.price_min = value if self.price_min is None else min(value, self.price_min)
                    self.price_max = value if self.price_max is None else max(value, self.price_max)
                    if value > 0:
                        tail = abs(value - value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
                        self.max_tail = max(tail, self.max_tail)
                        self.numeric[f]["positive_tail_gt_1e9"] += tail > Decimal("1e-9")
            except (InvalidOperation, ValueError):
                self.numeric[f]["missing_or_invalid"] += 1
        if verdict.invoice_datetime is not None:
            date = verdict.invoice_datetime.isoformat()
            self.date_min = date if self.date_min is None else min(self.date_min, date)
            self.date_max = date if self.date_max is None else max(self.date_max, date)

    def merge(self, other):
        for name in ("missing", "categories", "classes", "reasons", "cross", "precision"):
            getattr(self, name).update(getattr(other, name))
        for name in ("types", "numeric"):
            for key, value in getattr(other, name).items():
                getattr(self, name)[key].update(value)
        for name in ("formulas", "empty", "raw_cancel", "a_invoice", "a_stock", "a_stock_product"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.max_tail = max(self.max_tail, other.max_tail)
        for name in ("date_min", "price_min"):
            values = [v for v in (getattr(self, name), getattr(other, name)) if v is not None]
            setattr(self, name, min(values) if values else None)
        for name in ("date_max", "price_max"):
            values = [v for v in (getattr(self, name), getattr(other, name)) if v is not None]
            setattr(self, name, max(values) if values else None)

    def result(self):
        return {"missing": dict(self.missing), "cell_types": {f: dict(c) for f, c in self.types.items()},
                "categories": dict(self.categories), "classes": dict(self.classes), "reasons": dict(self.reasons),
                "category_validity": dict(self.cross), "numeric": {f: dict(c) for f, c in self.numeric.items()},
                "price_decimal_places": dict(self.precision), "max_positive_price_tail": str(self.max_tail),
                "price_min": str(self.price_min), "price_max": str(self.price_max),
                "date_min": self.date_min, "date_max": self.date_max, "formula_rows": self.formulas,
                "empty_positions": self.empty, "raw_c_prefix": self.raw_cancel,
                "a_invoice_prefix": self.a_invoice, "a_stock_prefix": self.a_stock,
                "a_stock_prefix_product": self.a_stock_product}


def prepare_snapshot(source: Path, output: Path, *, archive: Path | None = None,
                     expected_sha: str = SOURCE_SHA, selection=(SHEETS[1], 2, 10001)):
    source, output = Path(source), Path(output)
    if output.exists():
        raise InputError("OUTPUT_ALREADY_EXISTS", 409)
    validate_source(source, archive, expected_sha)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".scan-", dir=output.parent))
    clock = time.perf_counter()
    sheet_selected, start, end = selection
    if start < 2 or end < start:
        raise InputError("SOURCE_POSITION_INVALID")
    csv_path = temp / "retail_2010_rows_2_10001.csv"
    try:
        scans = {}
        selected_rows = []
        with WorkbookReader(source) as workbook:
            if tuple(workbook.sheets) != SHEETS:
                raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
            epoch = workbook.epoch
            for sheet in workbook.sheets:
                aggregate = Aggregate()
                pending_rows = {}
                pending_start = 2
                last_value = 1
                for row in workbook.rows(sheet):
                    if row.number == 1:
                        continue
                    if not row.has_value:
                        pending_rows[row.number] = row
                        continue
                    for n in range(pending_start, row.number):
                        empty = pending_rows.get(n, blank_row(n))
                        aggregate.add(empty, expected_sha, sheet, epoch)
                        if sheet == sheet_selected and start <= n <= end:
                            selected_rows.append(empty)
                    pending_rows.clear()
                    aggregate.add(row, expected_sha, sheet, epoch)
                    if sheet == sheet_selected and start <= row.number <= end:
                        selected_rows.append(row)
                    pending_start = row.number + 1
                    last_value = row.number
                if last_value == 1:
                    raise InputError("EMPTY_SHEET")
                scans[sheet] = {"last_value_row": last_value, "original_count": last_value - 1,
                                "headers": list(HEADERS), "aggregate": aggregate.result()}
        if len(selected_rows) != end - start + 1:
            raise InputError("SELECTION_OUTSIDE_SOURCE")
        # A second streaming pass inspects only the boundary invoices/lot keys, not precomputed matches.
        boundary = {normalize_invoice(r.values["invoice"]) for r in (selected_rows[0], selected_rows[-1])} - {None}
        boundary_lots = {(normalize_invoice(r.values["invoice"]), str(r.values["invoicedate_serial"]))
                         for r in (selected_rows[0], selected_rows[-1])}
        outside_invoice = outside_lot = 0
        with WorkbookReader(source) as workbook:
            for row in workbook.rows(sheet_selected):
                if row.number < start or row.number > end:
                    inv = normalize_invoice(row.values["invoice"])
                    outside_invoice += inv in boundary
                    outside_lot += (inv, str(row.values["invoicedate_serial"])) in boundary_lots
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for row in selected_rows:
                writer.writerow({"source_sha256": expected_sha, "sheet": sheet_selected, "source_row": row.number,
                                 **row.values, "cell_types": json_text(row.types), "cells": json_text(row.cells),
                                 "has_formula": "1" if row.has_formula else "0", "excel_epoch": epoch})
        total = sum(s["original_count"] for s in scans.values())
        full = {"scan_version": SCAN_VERSION, "normalization_version": NORMALIZATION_VERSION,
                "source_sha256": expected_sha, "source_bytes": source.stat().st_size, "excel_epoch": epoch,
                "method": "complete ZIP/XML values and positions; dimension unused", "sheets": scans,
                "original_total": total, "headers": list(HEADERS)}
        (temp / "full_scan.json").write_text(json_text(full) + "\n", encoding="utf-8")
        manifest = {"scan_version": SCAN_VERSION, "selection_version": SELECTION_VERSION,
                    "source_sha256": expected_sha, "source_bytes": source.stat().st_size,
                    "excel_epoch": epoch, "sheet": sheet_selected, "start_row": start, "end_row": end,
                    "original_total": total, "selected_count": len(selected_rows), "outside_count": total - len(selected_rows),
                    "csv_filename": csv_path.name, "csv_sha256": sha256(csv_path), "csv_columns": list(CSV_COLUMNS),
                    "full_scan_sha256": sha256(temp / "full_scan.json"),
                    "truncation": {"boundary_invoice_outside_rows": outside_invoice, "boundary_invoice_time_outside_rows": outside_lot,
                                   "warning": bool(outside_invoice), "scope": "current source range only"}}
        manifest["snapshot_id"] = hashlib.sha256(json_text(manifest).encode()).hexdigest()
        (temp / "snapshot_10k.json").write_text(json_text(manifest) + "\n", encoding="utf-8")
        validate_source(source, archive, expected_sha)
        # Whole directory publication: never partially replace an existing manifest or snapshot.
        os.rename(temp, output)
        return {"output": str(output), "snapshot_id": manifest["snapshot_id"],
                "manifest_digest": sha256(output / "snapshot_10k.json"), "csv_sha256": manifest["csv_sha256"],
                "original_total": total, "selected_count": len(selected_rows), "sheets": scans,
                "truncation": manifest["truncation"], "elapsed_seconds": time.perf_counter() - clock}
    except BaseException:
        # Keep failed staging for diagnosis; raw and published outputs remain untouched.
        raise
