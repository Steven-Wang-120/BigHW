"""Generate before auditing/sending; only server generation is claimed by audit."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from starlette.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from 主程序.repositories.database import database_transaction
from 主程序.repositories import export_repository as repository, audit_repository
from 主程序.repositories.workflow_repository import scoped_filter
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.workflow_service import authorize

MAX_ROWS = 100_000
MAX_BYTES = 32 * 1024 * 1024
CHUNK_ROWS = 100
OUTPUT_ROOT = Path(__file__).resolve().parents[2] / '数据/runtime/B07-B08-20261016/exports'
COLUMNS = ('record_type', 'scope', 'run_id', 'snapshot_id', 'source_sha256', 'sheet', 'start_row', 'end_row',
           'normalization_version', 'matching_rule_version', 'rule_digest', 'amount_unit', 'scope_note',
           'difference_id', 'reason', 'match_status', 'work_status', 'assignee_id', 'version', 'source_row',
           'invoice', 'stock', 'description', 'quantity', 'amount_units', 'source_quantity', 'source_amount_units',
           'message', 'resolution_note', 'scoped_count', 'quality_version', 'quality_provided', 'source_flags',
           'quality_marked_row_count', 'scoped_marked_row_count')
NUMBERS = frozenset({'start_row', 'end_row', 'version', 'source_row', 'quantity', 'amount_units',
                     'source_quantity', 'source_amount_units', 'scoped_count', 'quality_marked_row_count', 'scoped_marked_row_count'})


def safe_text(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def verify(database, context):
    with database_transaction(database, readonly=True) as c:
        context.verify(c)


def export_run(database, context, run_id, *, reason=None, status=None, assignee_id=None):
    authorize(context.actor, 'read')
    where, params = scoped_filter(run_id, context.actor, reason=reason, status=status, assignee_id=assignee_id, prefix='d.')
    with database_transaction(database, readonly=True) as c:
        context.verify(c)
        metadata = repository.metadata(c, run_id, context.actor)
        total = repository.count(c, where, params)
    if total > MAX_ROWS:
        raise InputError('EXPORT_TOO_LARGE', 413, max_rows=MAX_ROWS)
    scope = 'ASSIGNED_ONLY' if context.actor.role == 'HANDLER' else 'AUTHORIZED_FILTER'
    truncation = metadata.pop('truncation')
    quality = metadata.pop('quality')
    scope_note = '规则推断，非真实原单；仅当前范围；匹配覆盖率不是准确率；金额单位£0.0001；仅当前授权筛选；质量：' + (f"已验证 {quality['quality_version']}" if quality['provided'] else '未知：未提供逐行质量证据，不代表0标记')
    scope_note += '；源行标记仅当前授权工单；scoped_marked_row_count仅当前筛选导出行'
    if truncation['warning']:
        scope_note += '；存在范围截断风险'
    common = {**metadata, 'run_id': run_id, 'scope': scope, 'amount_unit': 'GBP_0.0001', 'scope_note': scope_note,
              'quality_version': quality['quality_version'], 'quality_provided': str(quality['provided']).lower()}
    if context.actor.role != 'HANDLER':
        common['quality_marked_row_count'] = quality['marked_row_count']
        common['scope_note'] += '；quality_marked_row_count为全批选区标记数'
    columns = tuple(key for key in COLUMNS if key != 'quality_marked_row_count' or context.actor.role != 'HANDLER')
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.export-', suffix='.csv', dir=OUTPUT_ROOT)
    path, count, marked_count, size, digest = Path(name), 0, 0, 0, hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as stream:
            def write(values=None):
                nonlocal size
                buffer = io.StringIO(newline='')
                writer = csv.writer(buffer, lineterminator='\r\n')
                if values is None:
                    writer.writerow(columns)
                else:
                    writer.writerow([str(values[key]) if key in NUMBERS and values.get(key) is not None
                                     else safe_text(values.get(key)) for key in columns])
                chunk = buffer.getvalue().encode('utf-8')
                if size + len(chunk) > MAX_BYTES:
                    raise InputError('EXPORT_TOO_LARGE', 413, max_bytes=MAX_BYTES)
                stream.write(chunk)
                size += len(chunk)
                digest.update(chunk)
            write()
            write({**common, 'record_type': 'metadata'})
            last_id = ''
            while True:
                # A new transaction per page sees logout/deactivation commits.
                with database_transaction(database, readonly=True) as c:
                    context.verify(c)
                    rows = repository.page(c, where, params, last_id, CHUNK_ROWS)
                if not rows:
                    break
                for row in rows:
                    count += 1
                    if count > MAX_ROWS:
                        raise InputError('EXPORT_TOO_LARGE', 413, max_rows=MAX_ROWS)
                    last_id = row.pop('id')
                    flags = json.loads(row['source_flags'])
                    marked_count += int('TRUNCATED_INVOICE_RANGE' in flags)
                    row['source_flags'] = ';'.join(flags)
                    write({**common, **row, 'difference_id': last_id, 'record_type': 'difference'})
            write({**common, 'record_type': 'scoped-summary', 'scoped_count': count,
                   'scoped_marked_row_count': marked_count if quality['provided'] else None})
        with database_transaction(database) as c:
            context.verify(c)
            audit_repository.append(c, context.actor.id, context.request_id, 'reconciliation_run', run_id,
                                    'EXPORT_GENERATED', 'SUCCESS', after={'scope': scope, 'rows': count,
                                    'sha256': digest.hexdigest(), 'server_generated': True,
                                    'filters': {'reason': reason, 'status': status,
                                                'assignee_id': context.actor.id if context.actor.role == 'HANDLER' else assignee_id}})
        return path, {'rows': count, 'sha256': digest.hexdigest(), 'scope': scope}
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class ExportResponse(StreamingResponse):
    def __init__(self, path, database, context, run_id, metadata):
        self.path = path
        async def chunks():
            with path.open('rb') as stream:
                while True:
                    await run_in_threadpool(verify, database, context)
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    yield chunk
        super().__init__(chunks(), media_type='text/csv; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename="run-{run_id if run_id.isascii() and run_id.isalnum() else "export"}.csv"',
            'X-Export-Rows': str(metadata['rows']), 'X-Export-SHA256': metadata['sha256'], 'X-Export-Scope': metadata['scope']})
        self.database, self.context = database, context

    async def __call__(self, scope, receive, send):
        try:
            # Still able to return JSON if revoked before response headers.
            await run_in_threadpool(verify, self.database, self.context)
            await super().__call__(scope, receive, send)
        finally:
            # ASGI disconnect/send errors may leave the generator suspended with
            # its file open. Close it before unlinking (required on Windows).
            try:
                await self.body_iterator.aclose()
            finally:
                self.path.unlink(missing_ok=True)
