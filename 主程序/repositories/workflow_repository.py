"""Parameterized work-order persistence; no authorization policy here."""

import json
from uuid import uuid4

from 主程序.domain.states import WorkState
from 主程序.repositories.import_repository import now
from 主程序.repositories.xlsx_reader import InputError


def require_workflow(c):
    if c.execute("PRAGMA user_version").fetchone()[0] != 4:
        raise InputError("SCHEMA_UPGRADE_REQUIRED", 409)


def get(c, difference_id):
    cur = c.execute("SELECT d.* FROM differences d JOIN runs r ON r.id=d.run_id WHERE d.id=? AND r.status='COMPLETED'", (difference_id,))
    row = cur.fetchone()
    if row is None:
        raise InputError("NOT_FOUND", 404)
    return dict(zip((col[0] for col in cur.description), row))


def state(row):
    return WorkState(row["work_status"], row["version"], row["assignee_id"], row["resolution_note"])


def safe_state(row):
    return {key: row[key] for key in ("work_status", "version", "assignee_id", "resolution_note")}


def present(row):
    result = {key: value for key, value in row.items() if key not in {"flags_json", "candidates_json"}}
    result.update(flags=json.loads(row["flags_json"]), candidates=json.loads(row["candidates_json"]))
    result["type"] = result["reason"]
    for key in ("quantity", "amount_units"):
        result[key] = str(result[key])
    return result


def assign(c, difference_id, actor_id, assignee_id):
    c.execute("INSERT INTO assignments VALUES (?,?,?,?,?)", (uuid4().hex, difference_id, assignee_id, actor_id, now()))


def update(c, difference_id, previous, updated):
    changed = c.execute("UPDATE differences SET work_status=?,version=?,assignee_id=?,resolution_note=? WHERE id=? AND version=?",
                        (updated.status, updated.version, updated.assignee_id, updated.resolution_note, difference_id, previous.version))
    if changed.rowcount != 1:
        raise InputError("VERSION_CONFLICT", 409)


def scoped_filter(run_id, actor, *, reason=None, status=None, assignee_id=None, prefix=''):
    clauses, params = [prefix + 'run_id=?'], [run_id]
    for column, value in (("reason", reason), ("work_status", status), ("assignee_id", actor.id if actor.role == "HANDLER" else assignee_id)):
        if value is not None:
            clauses.append(prefix + column + '=?')
            params.append(value)
    return ' AND '.join(clauses), params


def list_rows(c, run_id, *, actor, reason=None, status=None, assignee_id=None, page=1, page_size=50):
    if c.execute("SELECT 1 FROM runs WHERE id=? AND status='COMPLETED'", (run_id,)).fetchone() is None:
        raise InputError("NOT_FOUND", 404)
    if actor.role == 'HANDLER' and c.execute('SELECT 1 FROM differences WHERE run_id=? AND assignee_id=?', (run_id, actor.id)).fetchone() is None:
        raise InputError('NOT_FOUND', 404)
    where, params = scoped_filter(run_id, actor, reason=reason, status=status, assignee_id=assignee_id)
    total = c.execute("SELECT COUNT(*) FROM differences WHERE " + where, params).fetchone()[0]
    cur = c.execute("SELECT * FROM differences WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size))
    names = [col[0] for col in cur.description]
    return {"items": [present(dict(zip(names, row))) for row in cur], "total": total, "page": page, "page_size": page_size}


def evidence(c, row):
    cur = c.execute("SELECT source_sha,sheet,source_row,invoice,stock,customer,country,description,quantity,price_units,amount_units,invoice_time,flags_json FROM import_rows WHERE id=?", (row["cancel_id"],))
    result = dict(zip((col[0] for col in cur.description), cur.fetchone()))
    result['flags'] = json.loads(result.pop('flags_json'))
    manifest = json.loads(c.execute('SELECT s.manifest_json FROM runs r JOIN snapshots s ON s.id=r.snapshot_id WHERE r.id=?', (row['run_id'],)).fetchone()[0])
    from 主程序.domain.quality import row_quality
    result['quality'] = row_quality(manifest, result['flags'])
    for key in ("quantity", "price_units", "amount_units"):
        result[key] = str(result[key])
    return result
