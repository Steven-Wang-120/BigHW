"""Shared import orchestration; optional authenticated HTTP write context."""

from collections import Counter
import json
from pathlib import Path
from uuid import uuid4

from 主程序.domain.models import ConservationCounts
from 主程序.domain.normalization import classify_row, mark_suspected_duplicates
from 主程序.domain.non_product import NORMALIZATION_VERSION, cancellation_coverage
from 主程序.repositories.database import DatabaseError, database_transaction
from 主程序.repositories.process_lock import ProcessLockError, database_process_lock
from 主程序.repositories import import_repository as repository
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.manifest_service import verify_snapshot
from 主程序.services.snapshot_service import SOURCE_SHA, json_text


def derive_counts(manifest, grouped, normalization_version):
    classes, reasons, categories, cross = Counter(), Counter(), Counter(), Counter()
    raw_cancel = product_cancel = non_product_cancel = excluded_cancel = eligible = 0
    for kind, reason, category, c_prefix, matching, count in grouped:
        classes[kind] += count
        if reason:
            reasons[reason] += count
        categories[category] += count
        cross[f"{category}|{kind}"] += count
        raw_cancel += count if c_prefix else 0
        eligible += count if matching else 0
        if c_prefix:
            if kind == "EXCLUDED":
                excluded_cancel += count
            elif category == "PRODUCT":
                product_cancel += count
            else:
                non_product_cancel += count
    counts = ConservationCounts(manifest["original_total"], manifest["outside_count"], manifest["selected_count"],
                                classes["SALE"], classes["CANCEL"], classes["EXCLUDED"], reasons)
    if not counts.balanced or sum(reasons.values()) != classes["EXCLUDED"] or sum(categories.values()) != counts.selected_count:
        raise InputError("COUNT_MISMATCH")
    return {**counts.as_dict(), "categories": dict(sorted(categories.items())), "category_validity": dict(sorted(cross.items())),
            "non_product_count": counts.selected_count - categories["PRODUCT"], "product_count": categories["PRODUCT"],
            "invalid_count": classes["EXCLUDED"], "matching_eligible_count": eligible,
            "cancel_coverage": cancellation_coverage(raw_cancel, product_cancel, non_product_cancel, excluded_cancel),
            "normalization_version": normalization_version, "truncation": manifest["truncation"],
            "quality": {"status": "VERIFIED", "quality_version": manifest.get("quality", {}).get("quality_version"), "marked_row_count": manifest.get("quality", {}).get("row_flag_counts", {}).get("TRUNCATED_INVOICE_RANGE")} if manifest.get("quality") else {"status": "UNAVAILABLE", "quality_version": None, "marked_row_count": None},
            "stage": "IMPORT_ONLY", "matching_completed": False}


def counts(database: Path, batch_id: str):
    with database_transaction(database, readonly=True) as c:
        repository.require_business(c)
        batch, snapshot, grouped = repository.batch_counts_data(c, batch_id)
    manifest = json.loads(snapshot)
    quality = {"status": "VERIFIED", "quality_version": manifest.get("quality", {}).get("quality_version"), "marked_row_count": manifest.get("quality", {}).get("row_flag_counts", {}).get("TRUNCATED_INVOICE_RANGE")} if manifest.get("quality") else {"status": "UNAVAILABLE", "quality_version": None, "marked_row_count": None}
    result = derive_counts(manifest, grouped, batch[3])
    result["quality"] = quality
    stored = json.loads(batch[1])
    if result != stored:
        if "quality" not in stored and set(stored) == set(result) - {"quality"} and {k: result[k] for k in stored} == stored:
            result = stored
        else:
            raise InputError("COUNT_MISMATCH")
    return {"batch_id": batch_id, "snapshot_id": batch[2], **result, "quality": {**quality, "provided": quality['status'] == 'VERIFIED'}}


def import_snapshot(database: Path, directory: Path, source: Path, manifest_digest: str, *, expected_sha=SOURCE_SHA, fault=None):
    verified = verify_snapshot(directory, source, manifest_digest, expected_sha)
    return import_verified(database, verified, manifest_digest, fault=fault)


def import_verified(database, verified, manifest_digest, *, context=None, fault=None):
    manifest, full, raw_rows, cells, quality_rows = verified
    batch_id = uuid4().hex
    try:
        with database_process_lock(database, "shared"):
            with database_transaction(database) as c:
                repository.require_business(c)
                if context is not None:
                    context.verify(c)
                repository.register(c, manifest, full, manifest_digest)
                try:
                    attempt = repository.start_batch(c, manifest, NORMALIZATION_VERSION, batch_id)
                except InputError as exc:
                    if exc.code == "DUPLICATE_IMPORT":
                        repository.audit(c, exc.details["batch_id"], "IMPORT", "REJECTED", exc.code, context=context)
                        if context is not None:
                            exc.audit_recorded = True
                        duplicate = exc
                    else:
                        raise
                else:
                    duplicate = None
            if duplicate:
                raise duplicate
            try:
                verdicts = mark_suspected_duplicates([classify_row(row, truncation_flags={"TRUNCATED_INVOICE_RANGE"} if row.ref.source_row in quality_rows else frozenset()) for row in raw_rows])
                grouped = Counter((v.row_class, v.primary_exclusion_code, v.product_category,
                                   int(str(raw.raw("invoice") or "").strip()[:1].upper() == "C"), int(v.matching_eligible))
                                  for raw, v in zip(raw_rows, verdicts))
                result = derive_counts(manifest, [(*key, count) for key, count in grouped.items()], NORMALIZATION_VERSION)
                with database_transaction(database) as c:
                    if context is not None:
                        context.verify(c)
                    repository.insert_rows(c, batch_id, raw_rows, cells, verdicts)
                    if fault:
                        fault(c)
                    repository.finish_batch(c, batch_id, result, context=context)
                return {"batch_id": batch_id, "snapshot_id": manifest["snapshot_id"], "status": "SUCCESS", "attempt": attempt, **result,
                        'quality': {**result['quality'], 'provided': result['quality']['status'] == 'VERIFIED'}}
            except Exception as exc:
                code = exc.code if isinstance(exc, (InputError, DatabaseError)) else "IMPORT_WRITE_FAILED"
                try:
                    with database_transaction(database) as c:
                        repository.fail_batch(c, batch_id, code, context=context)
                except Exception as failure:
                    raise InputError("AUDIT_WRITE_FAILED", 500, batch_id=batch_id, failure_metadata_persisted=False) from failure
                status = (exc.status if isinstance(exc, InputError) else 503 if code == "DATABASE_BUSY" else 500) if context is not None else 500
                error = InputError(code, status, batch_id=batch_id, attempt=attempt, failure_metadata_persisted=True)
                if context is not None:
                    error.audit_recorded = True
                raise error from exc
    except ProcessLockError as exc:
        raise InputError("DATABASE_LIFECYCLE_LOCK_BUSY", 409) from exc
