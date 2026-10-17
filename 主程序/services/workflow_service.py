"""Trusted actors, scope, pure transitions, and atomic work-order/audit updates."""

from 主程序.domain.permissions import Actor, allowed, in_scope
from 主程序.domain.states import RuleError, transition
from 主程序.repositories.database import database_transaction
from 主程序.repositories import workflow_repository as repository, audit_repository
from 主程序.repositories.xlsx_reader import InputError


def authorize(actor, action):
    if not isinstance(actor, Actor) or not allowed(actor, action):
        raise InputError("FORBIDDEN", 403)


def reject(database, actor, request_id, target_type, target_id, action, error, *, fault=None, before=None):
    if error.code == "AUDIT_WRITE_FAILED":
        return
    try:
        with database_transaction(database) as c:
            repository.require_workflow(c)
            if fault:
                fault(c)
            audit_repository.append(c, actor.id if actor else None, request_id, target_type, target_id,
                                    action, "REJECTED", code=error.code, before=before, after=before)
        error.audit_recorded = True
    except Exception as exc:
        raise InputError("AUDIT_WRITE_FAILED", 500, audit_persisted=False, rejected_code=error.code) from exc


def verify_session(c, actor, session_id):
    if session_id is not None:
        from 主程序.repositories.import_repository import now
        if c.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?", (session_id, actor.id, now())).fetchone() is None:
            raise InputError("SESSION_EXPIRED", 401)


def change(database, actor, difference_id, event, expected_version, request_id, *, assignee_id=None, resolution_note=None, session_id=None, fault=None, rejection_fault=None):
    before = None
    try:
        authorize(actor, event)
        with database_transaction(database) as c:
            repository.require_workflow(c)
            verify_session(c, actor, session_id)
            # Recheck persisted identity inside the writer transaction to serialize
            # deactivation against a previously authenticated request.
            user = c.execute("SELECT role,active FROM users WHERE id=?", (actor.id,)).fetchone()
            if user is None or not user[1] or user[0] != actor.role:
                raise InputError("SESSION_EXPIRED", 401)
            row = repository.get(c, difference_id)
            if event != "assign" and not in_scope(actor, row["assignee_id"]):
                raise InputError("NOT_FOUND", 404)
            before = repository.safe_state(row)
            previous = repository.state(row)
            try:
                updated = transition(previous, event, expected_version, assignee_id=assignee_id, resolution_note=resolution_note)
            except RuleError as exc:
                status = 422 if exc.code in {"EMPTY_RESOLUTION", "INVALID_ASSIGNEE"} else 409
                raise InputError(exc.code, status, **exc.details) from exc
            if event == "assign":
                handler = c.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role='HANDLER'", (assignee_id,)).fetchone()
                if handler is None:
                    raise InputError("INVALID_ASSIGNEE", 422)
                repository.assign(c, difference_id, actor.id, assignee_id)
            repository.update(c, difference_id, previous, updated)
            after = repository.get(c, difference_id)
            if fault:
                fault(c)
            audit_repository.append(c, actor.id, request_id, "difference", difference_id, event.upper(), "SUCCESS",
                                    before=repository.safe_state(row), after=repository.safe_state(after))
            payload = repository.present(after)
        return payload
    except InputError as exc:
        reject(database, actor, request_id, "difference", difference_id, event.upper(), exc, fault=rejection_fault, before=before)
        raise


def detail(database, actor, difference_id):
    authorize(actor, "read")
    with database_transaction(database, readonly=True) as c:
        repository.require_workflow(c)
        row = repository.get(c, difference_id)
        if not in_scope(actor, row["assignee_id"]):
            raise InputError("NOT_FOUND", 404)
        run = c.execute("SELECT rule_digest,rule_json,normalization_version FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        import json
        history = c.execute("SELECT actor_id,action,summary_json,created_at,request_id FROM audit_events WHERE target_type='difference' AND target_id=? AND outcome='SUCCESS' ORDER BY created_at,id", (difference_id,)).fetchall()
        return {**repository.present(row), "source": repository.evidence(c, row), "rule_digest": run[0],
                "rule": json.loads(run[1]), "normalization_version": run[2],
                "history": [{"actor_id": h[0], "action": h[1], "states": json.loads(h[2]), "created_at": h[3], "request_id": h[4]} for h in history]}


def differences(database, actor, run_id, **filters):
    authorize(actor, "read")
    with database_transaction(database, readonly=True) as c:
        repository.require_workflow(c)
        return repository.list_rows(c, run_id, actor=actor, **filters)
