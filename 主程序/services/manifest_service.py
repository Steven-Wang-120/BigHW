"""Snapshot validation: local explicit trust anchor plus verified source, not uploaded claims."""

import csv
import hashlib
import json
from pathlib import Path
import re

from 主程序.domain.models import RawRow, SOURCE_FIELDS, SourceRowRef, ROW_CLASSES, EXCLUSION_PRIORITY
from 主程序.domain.non_product import NON_PRODUCT_CODES
from 主程序.repositories.xlsx_reader import InputError, WorkbookReader, blank_row, SHEETS, HEADERS
from 主程序.services.snapshot_service import CSV_COLUMNS, SCAN_VERSION, SELECTION_VERSION, json_text, sha256, validate_source

CELL_TYPES = {"absent", "f", "n", "s", "inlineStr", "str", "b", "e", "d"}
CATEGORIES = NON_PRODUCT_CODES | {"PRODUCT", "GIFT_VOUCHER", "A_BAD_DEBT_INVOICE"}


def load_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InputError("MANIFEST_MISMATCH")
            result[key] = value
        return result

    def invalid_constant(_):
        raise InputError("MANIFEST_MISMATCH")

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except json.JSONDecodeError as exc:
        raise InputError("MANIFEST_MISMATCH") from exc


def read_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputError("MANIFEST_MISMATCH") from exc
    return load_json(text)


def require(condition):
    if not condition:
        raise InputError("MANIFEST_MISMATCH")


def object_fields(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def integer(value, minimum=0):
    require(type(value) is int and minimum <= value <= 9223372036854775807)


def text(value):
    require(type(value) is str and bool(value))


def digest(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def count_map(value, keys=None):
    require(type(value) is dict)
    for key, count in value.items():
        text(key)
        require(keys is None or key in keys)
        integer(count)


def validate_aggregate(value):
    object_fields(value, ("missing", "cell_types", "categories", "classes", "reasons", "category_validity",
                         "numeric", "price_decimal_places", "max_positive_price_tail", "price_min", "price_max",
                         "date_min", "date_max", "formula_rows", "empty_positions", "raw_c_prefix",
                         "a_invoice_prefix", "a_stock_prefix", "a_stock_prefix_product"))
    object_fields(value["missing"], SOURCE_FIELDS)
    count_map(value["missing"])
    object_fields(value["cell_types"], SOURCE_FIELDS)
    for counts in value["cell_types"].values():
        count_map(counts, CELL_TYPES)
    for name, keys in (("categories", CATEGORIES), ("classes", ROW_CLASSES), ("reasons", EXCLUSION_PRIORITY),
                       ("category_validity", {f"{c}|{v}" for c in CATEGORIES for v in ROW_CLASSES})):
        count_map(value[name], keys)
    object_fields(value["numeric"], ("quantity", "price", "customer_id"))
    for counts in value["numeric"].values():
        count_map(counts, {"non_finite", "finite", "non_integer", "negative", "zero", "positive_tail_gt_1e9", "missing_or_invalid"})
    count_map(value["price_decimal_places"])
    require(all(k.isdigit() for k in value["price_decimal_places"]))
    for name in ("max_positive_price_tail", "price_min", "price_max"):
        text(value[name])
    for name in ("date_min", "date_max"):
        require(value[name] is None or type(value[name]) is str)
    for name in ("formula_rows", "empty_positions", "raw_c_prefix", "a_invoice_prefix", "a_stock_prefix", "a_stock_prefix_product"):
        integer(value[name])


def validate_manifest(manifest):
    object_fields(manifest, ("snapshot_id", "scan_version", "selection_version", "source_sha256", "source_bytes",
                             "excel_epoch", "sheet", "start_row", "end_row", "original_total", "selected_count", "outside_count",
                             "csv_filename", "csv_sha256", "csv_columns", "full_scan_sha256", "truncation"))
    for name in ("snapshot_id", "source_sha256", "csv_sha256", "full_scan_sha256"):
        digest(manifest[name])
    for name in ("scan_version", "selection_version", "sheet", "csv_filename"):
        text(manifest[name])
    require(manifest["sheet"] in SHEETS and manifest["excel_epoch"] in ("1900", "1904"))
    for name in ("source_bytes", "original_total", "selected_count"):
        integer(manifest[name], 1)
    for name in ("start_row", "end_row"):
        integer(manifest[name], 2)
    integer(manifest["outside_count"])
    require(type(manifest["csv_columns"]) is list and all(type(k) is str for k in manifest["csv_columns"]))
    truncation = manifest["truncation"]
    object_fields(truncation, ("boundary_invoice_outside_rows", "boundary_invoice_time_outside_rows", "warning", "scope"))
    integer(truncation["boundary_invoice_outside_rows"])
    integer(truncation["boundary_invoice_time_outside_rows"])
    require(type(truncation["warning"]) is bool and truncation["warning"] == bool(truncation["boundary_invoice_outside_rows"]))
    require(truncation["boundary_invoice_time_outside_rows"] <= truncation["boundary_invoice_outside_rows"])
    require(truncation["scope"] == "current source range only")


def validate_full(full):
    object_fields(full, ("scan_version", "normalization_version", "source_sha256", "source_bytes", "excel_epoch", "method",
                         "sheets", "original_total", "headers"))
    digest(full["source_sha256"])
    for name in ("scan_version", "normalization_version", "method"):
        text(full[name])
    require(full["excel_epoch"] in ("1900", "1904") and full["headers"] == list(HEADERS))
    integer(full["source_bytes"], 1)
    integer(full["original_total"], 1)
    object_fields(full["sheets"], SHEETS)
    for sheet in full["sheets"].values():
        object_fields(sheet, ("last_value_row", "original_count", "headers", "aggregate"))
        integer(sheet["last_value_row"], 2)
        require(sheet["last_value_row"] <= 1048576 and sheet["headers"] == list(HEADERS))
        integer(sheet["original_count"], 1)
        validate_aggregate(sheet["aggregate"])


def validate_cells(types, cells, number):
    object_fields(types, SOURCE_FIELDS)
    require(all(type(v) is str and v in CELL_TYPES for v in types.values()))
    require(type(cells) is dict and set(cells) <= set(SOURCE_FIELDS))
    for field, cell in cells.items():
        object_fields(cell, ("ref", "type", "xml_value", "style", "numFmtId", "formula", "formula_attributes"))
        require(cell["ref"] == f"{chr(65 + SOURCE_FIELDS.index(field))}{number}")
        require(type(cell["type"]) is str and cell["type"] in CELL_TYPES - {"absent", "f"})
        for name in ("style", "numFmtId"):
            require(type(cell[name]) is str and cell[name].isdigit())
        for name in ("xml_value", "formula"):
            require(cell[name] is None or type(cell[name]) is str)
        require(type(cell["formula_attributes"]) is dict and all(type(v) is str for v in cell["formula_attributes"].values()))
        require(types[field] == ("f" if cell["formula"] is not None else cell["type"]))
    require(all((field in cells) == (kind != "absent") for field, kind in types.items()))


def verify_snapshot(directory: Path, source: Path, trusted_digest: str, expected_sha: str):
    directory = Path(directory)
    manifest_path = directory / "snapshot_10k.json"
    if sha256(manifest_path) != trusted_digest:
        raise InputError("MANIFEST_MISMATCH")
    manifest = read_json(manifest_path)
    validate_manifest(manifest)
    original = dict(manifest)
    identity = original.pop("snapshot_id")
    if identity != hashlib.sha256(json_text(original).encode()).hexdigest():
        raise InputError("MANIFEST_MISMATCH")
    full_path = directory / "full_scan.json"
    if sha256(full_path) != manifest["full_scan_sha256"]:
        raise InputError("MANIFEST_MISMATCH")
    full = read_json(full_path)
    validate_full(full)
    validate_source(source, None, expected_sha)
    for key in ("source_sha256", "source_bytes", "excel_epoch", "scan_version", "original_total"):
        if manifest[key] != full[key]:
            raise InputError("MANIFEST_MISMATCH")
    if manifest["source_sha256"] != expected_sha or manifest["source_bytes"] != Path(source).stat().st_size:
        raise InputError("SOURCE_HASH_MISMATCH")
    if manifest["scan_version"] != SCAN_VERSION or manifest["selection_version"] != SELECTION_VERSION:
        raise InputError("MANIFEST_MISMATCH")
    if manifest["csv_filename"] != "retail_2010_rows_2_10001.csv" or manifest["csv_columns"] != list(CSV_COLUMNS):
        raise InputError("HEADER_MISMATCH")
    total = sum(s["last_value_row"] - 1 for s in full["sheets"].values())
    if total != full["original_total"] or any(s["original_count"] != s["last_value_row"] - 1 for s in full["sheets"].values()):
        raise InputError("COUNT_MISMATCH")
    for s in full["sheets"].values():
        aggregate = s["aggregate"]
        n = s["original_count"]
        if (sum(aggregate["classes"].values()) != n or sum(aggregate["categories"].values()) != n
                or sum(aggregate["category_validity"].values()) != n
                or sum(aggregate["reasons"].values()) != aggregate["classes"].get("EXCLUDED", 0)):
            raise InputError("COUNT_MISMATCH")
    start, end = manifest["start_row"], manifest["end_row"]
    if not 2 <= start <= end <= full["sheets"][manifest["sheet"]]["last_value_row"]:
        raise InputError("SOURCE_POSITION_INVALID")
    if manifest["selected_count"] != end - start + 1 or total != manifest["selected_count"] + manifest["outside_count"]:
        raise InputError("COUNT_MISMATCH")
    csv_path = directory / manifest["csv_filename"]
    if sha256(csv_path) != manifest["csv_sha256"]:
        raise InputError("SNAPSHOT_HASH_MISMATCH")
    raw_rows = []
    cells_list = []
    try:
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != list(CSV_COLUMNS):
                raise InputError("HEADER_MISMATCH")
            for n, record in enumerate(reader, start):
                if set(record) != set(CSV_COLUMNS) or any(v is None for v in record.values()):
                    raise InputError("CORRUPT_FILE", 400)
                if record["source_sha256"] != expected_sha or record["sheet"] != manifest["sheet"] or record["source_row"] != str(n) or n > end:
                    raise InputError("SOURCE_IDENTITY_MISMATCH")
                if record["excel_epoch"] != manifest["excel_epoch"] or record["has_formula"] not in {"0", "1"}:
                    raise InputError("MANIFEST_MISMATCH")
                values = {k: record[k] if record[k] != "" else None for k in SOURCE_FIELDS}
                types = load_json(record["cell_types"])
                cells = load_json(record["cells"])
                validate_cells(types, cells, n)
                require((record["has_formula"] == "1") == any(c["formula"] is not None for c in cells.values()))
                raw_rows.append(RawRow(SourceRowRef(expected_sha, manifest["sheet"], n), values,
                                       record["has_formula"] == "1", types, record["excel_epoch"]))
                cells_list.append(cells)
    except (csv.Error, OSError, UnicodeError) as exc:
        raise InputError("CORRUPT_FILE", 400) from exc
    if len(raw_rows) != manifest["selected_count"]:
        raise InputError("COUNT_MISMATCH")
    # Read actual selected source positions to reject fabricated row values even if a manifest is rehashed.
    with WorkbookReader(source) as workbook:
        if workbook.epoch != manifest["excel_epoch"]:
            raise InputError("MANIFEST_MISMATCH")
        source_rows = {}
        for row in workbook.rows(manifest["sheet"]):
            if row.number > end:
                break
            if row.number >= start:
                source_rows[row.number] = row
        for raw, cells in zip(raw_rows, cells_list):
            row = source_rows.get(raw.ref.source_row, blank_row(raw.ref.source_row))
            expected_values = {k: v if v not in (None, "") else None for k, v in row.values.items()}
            if dict(raw.values) != expected_values or dict(raw.cell_types) != row.types or raw.has_formula != row.has_formula or cells != row.cells:
                raise InputError("SOURCE_VALUE_MISMATCH")
    return manifest, full, raw_rows, cells_list
