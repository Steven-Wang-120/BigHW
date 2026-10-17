"""Shared reconciliation orchestration; optional authenticated HTTP write context."""

from pathlib import Path

from 主程序.domain.matching import reconcile_rows, MatchingCoverageError
from 主程序.domain.money import NumericOverflowError
from 主程序.repositories.database import DatabaseError, database_transaction
from 主程序.repositories.process_lock import ProcessLockError, database_process_lock
from 主程序.repositories import reconcile_repository as repository
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.import_service import counts


def run_summary(database: Path, run_id: str):
    with database_transaction(database, readonly=True) as c:
        repository.require_reconcile(c)
        return repository.completed_summary(c, run_id)


def validate_result(result, rows, batch_counts):
    """Cross-check persisted import coverage and independently recomputed integer triangles."""
    summary = result.summary
    coverage = batch_counts["cancel_coverage"]
    expected = {
        "raw_c_prefix_count": coverage["raw_c_prefix_count"],
        "product_valid_cancel": coverage["product_valid_cancel"],
        "non_product_review": coverage["non_product_valid_cancel"],
        "excluded_c_prefix": coverage["excluded_c_prefix"],
        "effective_cancel_count": batch_counts["cancel_count"],
    }
    if len(rows) != batch_counts["selected_count"] or any(summary[k] != v for k, v in expected.items()):
        raise InputError("COUNT_MISMATCH")
    product = summary["matched"] + summary["suspected"] + summary["no_original"]
    effective = product + summary["non_product_review"]
    if (product != expected["product_valid_cancel"] or effective != expected["effective_cancel_count"]
            or effective + expected["excluded_c_prefix"] != expected["raw_c_prefix_count"]
            or summary["matched"] != len(result.allocations)
            or summary["difference_count"] != len(result.differences)
            or len(result.allocations) + len(result.differences) != effective
            or any(summary[k] != 0 for k in ("raw_c_delta", "product_delta", "coverage_delta"))):
        raise InputError("COUNT_MISMATCH")
    cancels = [r for r in rows if r.row_class == "CANCEL"]
    for field, source_field, delta in (("quantity", "quantity", "quantity_delta"),
                                       ("amount_units", "amount_units", "amount_delta_units")):
        total = sum(abs(getattr(r, source_field)) for r in cancels)
        allocated = sum(getattr(a, source_field) for a in result.allocations)
        unmatched = sum(getattr(d, source_field) for d in result.differences)
        if (total != allocated + unmatched or summary[f"cancel_{field}"] != str(total)
                or summary[f"allocated_{field}"] != str(allocated)
                or summary[f"unmatched_{field}"] != str(unmatched) or summary[delta] != "0"):
            raise InputError("COUNT_MISMATCH")


def reconcile(database: Path, batch_id: str, *, fault=None, context=None):
    try:
        with database_process_lock(database, "shared"):
            with database_transaction(database) as c:
                repository.require_reconcile(c)
                if context is not None:
                    context.verify(c)
                run_id, attempt, reused = repository.start_run(c, batch_id)
                if reused:
                    if context is not None:
                        return {**repository.completed_summary(c, run_id), "reused": True}
                    return {**run_summary(database, run_id), "reused": True}
            try:
                batch_counts = counts(database, batch_id)
                with database_transaction(database, readonly=True) as c:
                    rows = repository.read_rows(c, batch_id)
                result = reconcile_rows(rows)
                validate_result(result, rows, batch_counts)
                summary = {**result.summary, "truncation": batch_counts["truncation"], "quality": batch_counts.get("quality", {"status": "UNAVAILABLE", "quality_version": None, "marked_row_count": None}), "row_truncation_flags_implemented": batch_counts.get("quality", {}).get("status") == "VERIFIED",
                           "stage": "HTTP_RECONCILED" if context is not None else "LOCAL_RECONCILED",
                           "formal_POST_RUNS_implemented": context is not None,
                           "source_counts": {k: batch_counts[k] for k in ("original_total", "outside_count", "selected_count",
                                                                        "sale_count", "cancel_count", "excluded_count", "delta_original", "delta_selected")}}
                with database_transaction(database) as c:
                    if context is not None:
                        context.verify(c)
                    repository.publish(c, run_id, batch_id, result, summary, fault, context=context)
                    payload = {**repository.completed_summary(c, run_id), "reused": False}
            except Exception as exc:
                code = ("NUMERIC_OVERFLOW" if isinstance(exc, NumericOverflowError) else "COUNT_MISMATCH" if isinstance(exc, MatchingCoverageError)
                        else exc.code if isinstance(exc, (InputError, DatabaseError)) else "RECONCILE_WRITE_FAILED")
                details = {"batch_id": batch_id, "run_id": run_id, "attempt": attempt,
                           "retry_action": "POST_RUNS" if context is not None else "LOCAL_RECONCILE",
                           "formal_POST_RUNS_implemented": context is not None}
                try:
                    with database_transaction(database) as c:
                        repository.fail_run(c, run_id, code, context=context)
                except Exception as failure:
                    raise InputError("AUDIT_WRITE_FAILED", 500, **details, failure_metadata_persisted=False) from failure
                status = (exc.status if isinstance(exc, InputError) else 503 if code == "DATABASE_BUSY" else 500) if context is not None else 500
                error = InputError(code, status, **details, failure_metadata_persisted=True)
                if context is not None:
                    error.audit_recorded = True
                raise error from exc
            return payload
    except ProcessLockError as exc:
        raise InputError("DATABASE_LIFECYCLE_LOCK_BUSY", 409) from exc


def batch_runs(database: Path, batch_id: str):
    with database_transaction(database, readonly=True) as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            return []
        return [{"run_id": row[0], "status": row[1], "attempt": row[2]} for row in
                c.execute("SELECT id,status,attempt FROM runs WHERE batch_id=? ORDER BY created_at,id", (batch_id,))]
