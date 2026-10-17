"""Authenticated B07 adapter; CLI remains local and never claims HTTP execution."""

from dataclasses import dataclass

from 主程序.domain.matching import RULE
from 主程序.repositories.database import database_transaction, DatabaseError
from 主程序.repositories.process_lock import database_process_lock
from 主程序.repositories import pipeline_repository as repository
from 主程序.repositories.workflow_repository import require_workflow
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services import import_service, reconcile_service
from 主程序.services.workflow_service import authorize, verify_session


@dataclass(frozen=True)
class WriteContext:
    actor: object
    session_id: str
    request_id: str

    def verify(self, c):
        require_workflow(c)
        verify_session(c, self.actor, self.session_id)
        repository.verify_identity(c, self.actor)


def rule_version(value):
    if value != RULE["version"]:
        raise InputError("RULE_UNCONFIRMED", 422)


def imported(database, context, catalog, snapshot_id, manifest_digest, csv_digest):
    authorize(context.actor, "import")
    verified = catalog.verified(snapshot_id, manifest_digest)
    return import_checked(database, context, verified, manifest_digest, csv_digest)


def import_checked(database, context, verified, manifest_digest, csv_digest):
    if csv_digest != verified[0]["csv_sha256"]:
        raise InputError("SNAPSHOT_HASH_MISMATCH", 422)
    return import_service.import_verified(database, verified, manifest_digest, context=context)


def start_run(database, context, batch_id, matching_rule_version):
    authorize(context.actor, "run")
    # Resolve the object before the domain rule, after HTTP structural validation.
    with database_transaction(database, readonly=True) as c:
        context.verify(c)
        repository.batch(c, batch_id)
    rule_version(matching_rule_version)
    payload = reconcile_service.reconcile(database, batch_id, context=context)
    status = 201 if not payload["reused"] and payload["attempt"] == 1 else 200
    return payload, status


def pipeline(database, context, catalog, snapshot_id, manifest_digest, csv_digest, matching_rule_version):
    # Do not create an import for an unapproved rule.
    authorize(context.actor, "import")
    verified = catalog.verified(snapshot_id, manifest_digest)
    rule_version(matching_rule_version)
    with database_process_lock(database, "shared"):
        batch = import_checked(database, context, verified, manifest_digest, csv_digest)
        try:
            run, _ = start_run(database, context, batch["batch_id"], matching_rule_version)
        except InputError as exc:
            # Even pre-start failures must preserve the successful import. A run ID
            # is honestly null if no attempt could be created (e.g. session revoked).
            exc.details = {"batch_id": batch["batch_id"], "run_id": None, "attempt": None,
                           "retry_action": "POST_RUNS", **exc.details}
            raise
        except DatabaseError as exc:
            raise InputError(exc.code, 503 if exc.code == "DATABASE_BUSY" else 500,
                             batch_id=batch["batch_id"], run_id=None, attempt=None, retry_action="POST_RUNS") from exc
        return {"batch_id": batch["batch_id"], "run_id": run["run_id"], "status": run["status"],
                "attempt": run["attempt"], "summary": run, "import": batch}


def import_status(database, actor, batch_id, *, counts_only=False):
    authorize(actor, "import_read")
    with database_transaction(database, readonly=True) as c:
        require_workflow(c)
        payload = repository.batch(c, batch_id)
    if counts_only:
        return import_service.counts(database, batch_id)
    if payload["status"] == "SUCCESS":
        payload.update(import_service.counts(database, batch_id))
    return payload


def run_status(database, actor, run_id):
    authorize(actor, "read")
    with database_transaction(database, readonly=True) as c:
        require_workflow(c)
        return repository.run(c, run_id, actor)
