"""B04 persistence; services own the transaction boundaries."""

from datetime import datetime
import hashlib
import json
from uuid import uuid4

from 主程序.domain.matching import MatchingRow, RULE, RULE_DIGEST
from 主程序.domain.models import SourceRowRef
from 主程序.domain.serialization import json_text
from 主程序.repositories.import_repository import now
from 主程序.repositories.xlsx_reader import InputError


def require_reconcile(c):
    if c.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise InputError("SCHEMA_UPGRADE_REQUIRED", 409)


def start_run(c, batch_id):
    batch = c.execute("SELECT snapshot_id,normalization_version,status FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise InputError("BATCH_NOT_FOUND", 404)
    if batch[2] != "SUCCESS":
        raise InputError("IMPORT_NOT_READY", 409)
    snapshot, version, _ = batch
    existing = c.execute("SELECT id,status,attempt,batch_id FROM runs WHERE snapshot_id=? AND normalization_version=? AND rule_digest=?",
                         (snapshot, version, RULE_DIGEST)).fetchone()
    if existing:
        run_id, status, attempt, saved_batch = existing
        if saved_batch != batch_id:
            raise InputError("RUN_BATCH_CONFLICT", 409)
        if status == "COMPLETED":
            return run_id, attempt, True
        if status == "RUNNING":
            raise InputError("RUN_IN_PROGRESS", 409, batch_id=batch_id, run_id=run_id, attempt=attempt)
        attempt += 1
        c.execute("UPDATE runs SET status='RUNNING',attempt=?,error_code=NULL,finished_at=NULL WHERE id=?", (attempt, run_id))
        return run_id, attempt, False
    run_id = uuid4().hex
    c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (run_id, batch_id, snapshot, version, RULE_DIGEST, json_text(RULE), "RUNNING", 1, None, None, now(), None))
    return run_id, 1, False


def read_rows(c, batch_id):
    rows = c.execute("SELECT id,source_sha,sheet,source_row,class,invoice,customer,stock,price_units,quantity,amount_units,invoice_time,country,flags_json,category,matching_eligible,raw_c_prefix FROM import_rows WHERE batch_id=?", (batch_id,)).fetchall()
    return [MatchingRow(r[0], SourceRowRef(r[1], r[2], r[3]), r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                        datetime.fromisoformat(r[11]) if r[11] else None, r[12], tuple(json.loads(r[13])), r[14], bool(r[15]), r[16]) for r in rows]


def audit(c, run_id, outcome, code=None, summary=None):
    try:
        c.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (uuid4().hex, None, run_id, "reconciliation_run", run_id, "RECONCILE", outcome, code, json_text(summary or {}), now()))
    except Exception as exc:
        raise InputError("AUDIT_WRITE_FAILED", 500) from exc


def publish(c, run_id, batch_id, result, summary, fault=None):
    ids = {lot.id: hashlib.sha256((run_id + "|" + lot.id).encode()).hexdigest() for lot in result.lots}
    c.executemany("INSERT INTO sale_lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  [(ids[l.id], run_id, batch_id, l.id, l.invoice, l.customer, l.stock, l.price_units,
                    l.invoice_time.isoformat(), l.quantity, l.remaining, l.amount_units, json_text(l.countries), json_text(l.flags)) for l in result.lots])
    c.executemany("INSERT INTO sale_lot_rows VALUES (?,?,?,?)",
                  [(ids[l.id], run_id, batch_id, row) for l in result.lots for row in l.row_ids])
    c.executemany("INSERT INTO allocations VALUES (?,?,?,?,?,?,?)",
                  [(uuid4().hex, run_id, batch_id, a.cancel_id, ids[a.lot_id], a.quantity, a.amount_units) for a in result.allocations])
    records = []
    for d in result.differences:
        candidates = [{**v, "lot_id": ids[v["lot_id"]]} for v in d.candidates]
        records.append((uuid4().hex, run_id, batch_id, d.cancel_id, d.reason, json_text(d.flags), json_text(candidates),
                        d.message, d.quantity, d.amount_units, "UNMATCHED", "PENDING", 0))
    c.executemany("INSERT INTO differences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", records)
    if fault:
        fault(c)
    changed = c.execute("UPDATE runs SET status='COMPLETED',summary_json=?,finished_at=? WHERE id=? AND batch_id=? AND status='RUNNING'",
                        (json_text(summary), now(), run_id, batch_id))
    if changed.rowcount != 1:
        raise InputError("RUN_STATE_CONFLICT", 409, run_id=run_id)
    audit(c, run_id, "SUCCESS", summary=summary)


def fail_run(c, run_id, code):
    changed = c.execute("UPDATE runs SET status='FAILED',summary_json=NULL,error_code=?,finished_at=? WHERE id=? AND status='RUNNING'",
                        (code, now(), run_id))
    if changed.rowcount != 1:
        raise InputError("RUN_STATE_CONFLICT", 409, run_id=run_id)
    audit(c, run_id, "FAILED", code)


def completed_summary(c, run_id):
    run = c.execute("SELECT batch_id,snapshot_id,normalization_version,rule_digest,rule_json,status,attempt,summary_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise InputError("RUN_NOT_FOUND", 404)
    if run[5] != "COMPLETED":
        raise InputError("RUN_NOT_READY", 409, run_id=run_id, run_status=run[5])
    return {"run_id": run_id, "batch_id": run[0], "snapshot_id": run[1], "normalization_version": run[2],
            "rule_digest": run[3], "rule": json.loads(run[4]), "status": run[5], "attempt": run[6], **json.loads(run[7])}
