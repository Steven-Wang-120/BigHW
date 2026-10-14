"""Persistence operations for B03; caller owns transaction boundaries."""

from datetime import datetime, timezone
import hashlib
from uuid import uuid4

from 主程序.repositories.xlsx_reader import InputError
from 主程序.domain.serialization import json_text


def now():
    return datetime.now(timezone.utc).isoformat()


def require_business(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] not in {2, 3}:
        raise InputError("SCHEMA_UPGRADE_REQUIRED", 409)


def audit(connection, batch_id, action, outcome, code=None, summary=None):
    try:
        connection.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (uuid4().hex, None, batch_id, "import_batch", batch_id, action, outcome, code,
                            json_text(summary or {}), now()))
    except Exception as exc:
        raise InputError("AUDIT_WRITE_FAILED", 500) from exc


def register(connection, manifest, full, digest):
    source_id = manifest["source_sha256"]
    existing = connection.execute("SELECT scan_json FROM source_files WHERE sha256=?", (source_id,)).fetchone()
    if existing and existing[0] != json_text(full):
        raise InputError("SOURCE_REGISTRATION_CONFLICT", 409)
    if not existing:
        connection.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?,?)",
                           (source_id, source_id, "online_retail_II.xlsx", manifest["source_bytes"], json_text(full), manifest["scan_version"], now()))
    existing = connection.execute("SELECT manifest_digest FROM snapshots WHERE id=?", (manifest["snapshot_id"],)).fetchone()
    if existing and existing[0] != digest:
        raise InputError("MANIFEST_MISMATCH")
    if not existing:
        connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (manifest["snapshot_id"], source_id, manifest["sheet"], manifest["start_row"], manifest["end_row"],
                            manifest["csv_sha256"], manifest["selection_version"], manifest["selected_count"], manifest["outside_count"], digest, json_text(manifest)))


def start_batch(connection, manifest, version, batch_id):
    existing = connection.execute("SELECT id FROM import_batches WHERE snapshot_id=? AND normalization_version=? AND status='SUCCESS'",
                                  (manifest["snapshot_id"], version)).fetchone()
    if existing:
        raise InputError("DUPLICATE_IMPORT", 409, batch_id=existing[0])
    active = connection.execute("SELECT id FROM import_batches WHERE snapshot_id=? AND normalization_version=? AND status='IMPORTING'",
                                (manifest["snapshot_id"], version)).fetchone()
    if active:
        raise InputError("IMPORT_IN_PROGRESS", 409, batch_id=active[0])
    attempt = connection.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM import_batches WHERE snapshot_id=? AND normalization_version=?",
                                 (manifest["snapshot_id"], version)).fetchone()[0]
    connection.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)",
                       (batch_id, manifest["snapshot_id"], version, "IMPORTING", attempt, None, None, now(), None))
    return attempt


def insert_rows(connection, batch_id, raw_rows, cells, verdicts):
    records = []
    for raw, cell, v in zip(raw_rows, cells, verdicts):
        row_id = hashlib.sha256((batch_id + "|" + raw.ref.canonical()).encode()).hexdigest()
        records.append((row_id, batch_id, raw.ref.source_sha256, raw.ref.sheet, raw.ref.source_row,
                        v.invoice, v.stock_code, v.customer_key, v.country, v.description, v.quantity,
                        v.price_units, v.amount_units, v.invoice_datetime.isoformat() if v.invoice_datetime else None,
                        v.row_class, v.primary_exclusion_code, json_text(v.secondary_flags),
                        json_text({"values": dict(raw.values), "cells": cell, "cell_types": dict(raw.cell_types),
                                   "has_formula": raw.has_formula, "excel_epoch": raw.excel_epoch}),
                        v.product_category, int(v.matching_eligible), int(str(raw.raw("invoice") or "").strip()[:1].upper() == "C")))
    connection.executemany("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in range(21)) + ")", records)


def finish_batch(connection, batch_id, counts):
    connection.execute("UPDATE import_batches SET status='SUCCESS', counts_json=?, finished_at=? WHERE id=? AND status='IMPORTING'",
                       (json_text(counts), now(), batch_id))
    audit(connection, batch_id, "IMPORT", "SUCCESS", summary=counts)


def fail_batch(connection, batch_id, code):
    connection.execute("UPDATE import_batches SET status='FAILED',error_code=?,finished_at=? WHERE id=? AND status='IMPORTING'",
                       (code, now(), batch_id))
    audit(connection, batch_id, "IMPORT", "FAILED", code)


def batch_counts_data(connection, batch_id):
    batch = connection.execute("SELECT status,counts_json,snapshot_id,normalization_version FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise InputError("BATCH_NOT_FOUND", 404)
    if batch[0] != "SUCCESS":
        raise InputError("IMPORT_NOT_READY", 409)
    snapshot = connection.execute("SELECT manifest_json FROM snapshots WHERE id=?", (batch[2],)).fetchone()[0]
    rows = connection.execute("SELECT class,primary_reason,category,raw_c_prefix,matching_eligible,COUNT(*) FROM import_rows WHERE batch_id=? GROUP BY class,primary_reason,category,raw_c_prefix,matching_eligible", (batch_id,)).fetchall()
    return batch, snapshot, rows
