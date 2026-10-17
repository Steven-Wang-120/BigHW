"""Authorized export reads share the list filter, without global handler totals."""

import json

from 主程序.domain.quality import quality_summary
from 主程序.repositories.workflow_repository import scoped_filter
from 主程序.repositories.xlsx_reader import InputError


def metadata(c, run_id, actor):
    row = c.execute("""SELECT r.snapshot_id,r.normalization_version,r.rule_digest,r.rule_json,s.manifest_json
                       FROM runs r JOIN snapshots s ON s.id=r.snapshot_id
                       WHERE r.id=? AND r.status='COMPLETED'""", (run_id,)).fetchone()
    if row is None:
        raise InputError('NOT_FOUND', 404)
    if actor.role == 'HANDLER' and c.execute('SELECT 1 FROM differences WHERE run_id=? AND assignee_id=?', (run_id, actor.id)).fetchone() is None:
        raise InputError('NOT_FOUND', 404)
    manifest = json.loads(row[4])
    return {'snapshot_id': row[0], 'normalization_version': row[1], 'rule_digest': row[2],
            'matching_rule_version': json.loads(row[3])['version'],
            'quality': quality_summary(manifest, scoped=actor.role == 'HANDLER'),
            **{key: manifest[key] for key in ('source_sha256', 'sheet', 'start_row', 'end_row', 'truncation')}}


def count(c, where, params):
    return c.execute('SELECT COUNT(*) FROM differences d WHERE ' + where, params).fetchone()[0]


def page(c, where, params, last_id, size):
    cursor = c.execute('''SELECT d.id,d.reason,d.match_status,d.work_status,d.assignee_id,d.version,d.quantity,d.amount_units,
                               d.message,d.resolution_note,r.source_row,r.invoice,r.stock,r.description,r.quantity AS source_quantity,
                               r.amount_units AS source_amount_units,r.flags_json AS source_flags
                        FROM differences d JOIN import_rows r ON r.id=d.cancel_id
                        WHERE ''' + where + ' AND d.id>? ORDER BY d.id LIMIT ?', (*params, last_id, size))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]
