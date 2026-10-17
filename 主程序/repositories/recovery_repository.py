"""Transactional recovery of clean interrupted schema-4 jobs."""

from 主程序.domain.serialization import json_text
from 主程序.repositories.import_repository import now
from 主程序.repositories.xlsx_reader import InputError
from 主程序.repositories.audit_repository import append


_ACTIVE_IMPORT = "SELECT id,attempt FROM import_batches WHERE status='IMPORTING' ORDER BY created_at,id"
_ACTIVE_RUN = "SELECT id,attempt FROM runs WHERE status='RUNNING' ORDER BY created_at,id"


def _partial(c, table, column, value):
    return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,)).fetchone()[0]


def recover(c, request_id):
    if c.execute("PRAGMA user_version").fetchone()[0] != 4:
        raise InputError("RECOVERY_SCHEMA4_REQUIRED", 409)
    imports = c.execute(_ACTIVE_IMPORT).fetchall()
    runs = c.execute(_ACTIVE_RUN).fetchall()
    diagnostics = []
    for batch_id, attempt in imports:
        count = _partial(c, "import_rows", "batch_id", batch_id)
        if count:
            diagnostics.append({"target_type": "import_batch", "target_id": batch_id,
                                "facts": {"import_rows": count}})
    for run_id, attempt in runs:
        facts = {table: _partial(c, table, "run_id", run_id)
                 for table in ("sale_lots", "sale_lot_rows", "allocations", "differences")}
        if any(facts.values()):
            diagnostics.append({"target_type": "reconciliation_run", "target_id": run_id,
                                "facts": facts})
    if diagnostics:
        raise InputError("RECOVERY_PARTIAL_FACTS", 409, diagnostics=diagnostics)

    recovered_imports, recovered_runs = [], []
    for batch_id, attempt in imports:
        changed = c.execute(
            "UPDATE import_batches SET status='FAILED',error_code='INTERRUPTED_PROCESS',finished_at=? "
            "WHERE id=? AND status='IMPORTING' AND attempt=?", (now(), batch_id, attempt))
        if changed.rowcount != 1:
            raise InputError("RECOVERY_STATE_CONFLICT", 409, target_id=batch_id)
        summary = {"actor_kind": "LOCAL_CLI", "command": "recover-jobs",
                   "previous_status": "IMPORTING", "new_status": "FAILED",
                   "previous_attempt": attempt, "recovery_code": "INTERRUPTED_PROCESS"}
        append(c, None, request_id, "import_batch", batch_id, "RECOVER_IMPORT", "FAILED",
               code="INTERRUPTED_PROCESS", after=summary)
        recovered_imports.append({"batch_id": batch_id, "previous_status": "IMPORTING",
                                  "previous_attempt": attempt, "status": "FAILED",
                                  "error_code": "INTERRUPTED_PROCESS"})
    for run_id, attempt in runs:
        changed = c.execute(
            "UPDATE runs SET status='FAILED',summary_json=NULL,error_code='INTERRUPTED_PROCESS',finished_at=? "
            "WHERE id=? AND status='RUNNING' AND attempt=?", (now(), run_id, attempt))
        if changed.rowcount != 1:
            raise InputError("RECOVERY_STATE_CONFLICT", 409, target_id=run_id)
        summary = {"actor_kind": "LOCAL_CLI", "command": "recover-jobs",
                   "previous_status": "RUNNING", "new_status": "FAILED",
                   "previous_attempt": attempt, "recovery_code": "INTERRUPTED_PROCESS"}
        append(c, None, request_id, "reconciliation_run", run_id, "RECOVER_RECONCILE", "FAILED",
               code="INTERRUPTED_PROCESS", after=summary)
        recovered_runs.append({"run_id": run_id, "previous_status": "RUNNING",
                               "previous_attempt": attempt, "status": "FAILED",
                               "error_code": "INTERRUPTED_PROCESS"})
    return {"request_id": request_id, "status": "RECOVERED",
            "imports": recovered_imports, "runs": recovered_runs}
