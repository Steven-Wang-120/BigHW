"""Status-only reads for unfinished work; no partial row/result publication."""

import json

from 主程序.repositories.xlsx_reader import InputError
from 主程序.repositories import reconcile_repository


def batch(c, batch_id):
    row = c.execute("SELECT snapshot_id,normalization_version,status,attempt,error_code FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise InputError("BATCH_NOT_FOUND", 404)
    return dict(zip(("snapshot_id", "normalization_version", "status", "attempt", "error_code"), row)) | {"batch_id": batch_id}


def run(c, run_id, actor):
    row = c.execute("SELECT batch_id,status,attempt,normalization_version,rule_json,rule_digest,error_code FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise InputError("RUN_NOT_FOUND", 404)
    if actor.role == "HANDLER":
        if c.execute("SELECT 1 FROM differences WHERE run_id=? AND assignee_id=?", (run_id, actor.id)).fetchone() is None:
            raise InputError("NOT_FOUND", 404)
        # No global counters or other handlers' details in this necessary summary.
        from 主程序.domain.quality import quality_summary
        manifest = json.loads(c.execute('SELECT s.manifest_json FROM runs r JOIN snapshots s ON s.id=r.snapshot_id WHERE r.id=?', (run_id,)).fetchone()[0])
        return {"run_id": run_id, "status": row[1], "attempt": row[2], "normalization_version": row[3],
                "rule": json.loads(row[4]), "rule_digest": row[5], "scope": "ASSIGNED_ONLY", 'quality': quality_summary(manifest, scoped=True)}
    if row[1] == "COMPLETED":
        return reconcile_repository.completed_summary(c, run_id)
    return {"run_id": run_id, "batch_id": row[0], "status": row[1], "attempt": row[2], "error_code": row[6]}


def list_runs(c, actor, *, batch_id=None, page=1, page_size=50):
    clauses, params = ['1=1'], []
    if actor.role == 'HANDLER':
        clauses.append("r.status='COMPLETED' AND EXISTS(SELECT 1 FROM differences d WHERE d.run_id=r.id AND d.assignee_id=?)")
        params.append(actor.id)
    if batch_id is not None:
        clauses.append('r.batch_id=?')
        params.append(batch_id)
    where = ' AND '.join(clauses)
    total = c.execute('SELECT COUNT(*) FROM runs r WHERE ' + where, params).fetchone()[0]
    rows = c.execute('SELECT r.id,r.batch_id,r.status,r.attempt,r.created_at FROM runs r WHERE ' + where +
                     ' ORDER BY r.created_at DESC,r.id LIMIT ? OFFSET ?', (*params, page_size, (page - 1) * page_size)).fetchall()
    items = []
    for row in rows:
        item = dict(zip(('run_id', 'batch_id', 'status', 'attempt', 'created_at'), row))
        if actor.role == 'HANDLER':
            item.pop('batch_id')
            item['scope'] = 'ASSIGNED_ONLY'
        items.append(item)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


def verify_identity(c, actor):
    if c.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role=?", (actor.id, actor.role)).fetchone() is None:
        raise InputError("SESSION_EXPIRED", 401)
