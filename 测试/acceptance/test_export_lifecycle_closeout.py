"""Bounded synthetic generation/send lifecycle faults; not pre-request revocation."""
import asyncio
import csv
import io

import pytest

from 测试.acceptance.test_pipeline_api import b07, imported, ORIGIN, assert_error
from 测试.acceptance.test_b08_export_api import _run_and_difference
from 主程序.repositories.database import database_transaction
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.pipeline_service import WriteContext
from 主程序.services import export_service as exporter


def _revoke(db, user_id, mode):
    with database_transaction(db) as c:
        if mode == 'logout':
            c.execute("UPDATE sessions SET revoked_at='2026-10-17T00:00:00+00:00' WHERE user_id=?", (user_id,))
        else:
            c.execute('UPDATE users SET active=0,version=version+1 WHERE id=?', (user_id,))


def _context(client, app, request_id):
    identity = app.state.auth_service.identity(client.cookies['session'])
    return WriteContext(identity.actor, identity.session_id, request_id)


@pytest.mark.parametrize('mode', ['logout', 'deactivate'])
def test_revocation_during_real_keyset_pages_stops_generation_no_success_and_cleanup(b07, monkeypatch, tmp_path, mode):
    client, db, users, run, _ = _run_and_difference(b07)
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    monkeypatch.setattr(exporter, 'CHUNK_ROWS', 1)
    original = exporter.repository.page
    pages = []
    def revoke_after_first(c, where, params, last_id, size):
        rows = original(c, where, params, last_id, size)
        pages.append([r['id'] for r in rows])
        return rows
    monkeypatch.setattr(exporter.repository, 'page', revoke_after_first)
    from contextlib import contextmanager
    verifies = []
    @contextmanager
    def transaction_then_revoke(database, *, readonly=False):
        with database_transaction(database, readonly=readonly) as c:
            yield c
        # The first keyset page's read transaction has genuinely finished.
        if readonly and pages and not verifies:
            verifies.append(True)
            _revoke(db, users['FINANCE']['id'], mode)
    monkeypatch.setattr(exporter, 'database_transaction', transaction_then_revoke)
    try:
        response = client.get(f'/api/v1/runs/{run}/export?format=csv')
        assert_error(response, 401, 'SESSION_EXPIRED')
        assert len(pages) == 1 and len(pages[0]) == 1 and verifies == [True]
        assert not list(exporter.OUTPUT_ROOT.glob('.export-*'))
        with database_transaction(db, readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='EXPORT_GENERATED' AND outcome='SUCCESS'").fetchone()[0] == 0
    finally:
        client.close()


@pytest.mark.parametrize('mode', ['logout', 'deactivate'])
def test_revocation_after_generation_before_headers_returns_json_and_cleans(b07, monkeypatch, tmp_path, mode):
    client, db, users, run, _ = _run_and_difference(b07)
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    original = exporter.export_run
    generated = []
    def generate_then_revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        generated.append(value[0])
        assert value[0].is_file()
        _revoke(db, users['FINANCE']['id'], mode)
        return value
    monkeypatch.setattr(exporter, 'export_run', generate_then_revoke)
    try:
        response = client.get(f'/api/v1/runs/{run}/export?format=csv')
        assert_error(response, 401, 'SESSION_EXPIRED')
        assert response.headers['content-type'].startswith('application/json')
        assert len(generated) == 1 and not generated[0].exists()
        with database_transaction(db, readonly=True) as c:
            # Generation honestly completed/audited; no claim that bytes were sent/saved.
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='EXPORT_GENERATED' AND outcome='SUCCESS'").fetchone()[0] == 1
    finally:
        client.close()


def _errors(exc):
    return [e for child in getattr(exc, 'exceptions', []) for e in _errors(child)] or [exc]


@pytest.mark.parametrize('mode', ['logout', 'deactivate', 'send_error', 'disconnect'])
def test_after_first_chunk_revocation_or_asgi_failure_stops_subsequent_bytes_and_cleans(b07, monkeypatch, tmp_path, mode):
    client, db, users, run, _ = _run_and_difference(b07)
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    try:
        # Two synthetic rows, modest total output, genuinely more than one 64KiB chunk.
        with database_transaction(db) as c:
            c.execute('UPDATE import_rows SET description=? WHERE id=(SELECT cancel_id FROM differences WHERE run_id=? LIMIT 1)', ('synthetic-long-' + 'x' * 70000, run))
        context = _context(client, b07[4], 'send-' + mode)
        path, metadata = exporter.export_run(db, context, run)
        assert path.stat().st_size > 65536
        response = exporter.ExportResponse(path, db, context, run, metadata)
        sent = []
        async def exercise():
            disconnected = asyncio.Event()
            async def send(message):
                sent.append(message)
                if message['type'] == 'http.response.body' and message.get('body'):
                    if mode in ('logout', 'deactivate'):
                        _revoke(db, context.actor.id, mode)
                    elif mode == 'send_error':
                        raise RuntimeError('synthetic ASGI send failure')
                    else:
                        disconnected.set()
                        await asyncio.Event().wait()  # cancelled by disconnect listener
            async def receive():
                await disconnected.wait()
                return {'type': 'http.disconnect'}
            await response({'type': 'http', 'asgi': {'version': '3.0'}, 'method': 'GET', 'path': '/export'}, receive, send)
        if mode == 'disconnect':
            asyncio.run(exercise())
        else:
            with pytest.raises(BaseException) as caught:
                asyncio.run(exercise())
            leaves = _errors(caught.value)
            if mode in ('logout', 'deactivate'):
                assert any(isinstance(e, InputError) and e.code == 'SESSION_EXPIRED' for e in leaves), leaves
            else:
                assert any(isinstance(e, RuntimeError) and 'synthetic ASGI' in str(e) for e in leaves), leaves
        bodies = [m for m in sent if m['type'] == 'http.response.body' and m.get('body')]
        assert len(bodies) == 1 and len(bodies[0]['body']) == 65536
        assert not any(m['type'] == 'http.response.body' and m.get('more_body') is False for m in sent)
        assert not path.exists() and not list(exporter.OUTPUT_ROOT.glob('.export-*'))
    finally:
        client.close()


@pytest.mark.parametrize('limit', ['row-preflight', 'row-during-pagination', 'bytes'])
def test_actual_limits_trigger_and_cleanup_without_success_audit(b07, monkeypatch, tmp_path, limit):
    client, db, users, run, _ = _run_and_difference(b07)
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    try:
        if limit.startswith('row'):
            monkeypatch.setattr(exporter, 'MAX_ROWS', 1)
            if limit == 'row-during-pagination':
                # Simulates a stale preflight count; actual real rows exceed bound.
                monkeypatch.setattr(exporter.repository, 'count', lambda *args: 1)
                monkeypatch.setattr(exporter, 'CHUNK_ROWS', 1)
        else:
            monkeypatch.setattr(exporter, 'MAX_BYTES', 2048)
            with database_transaction(db) as c:
                c.execute('UPDATE import_rows SET description=? WHERE id=(SELECT cancel_id FROM differences WHERE run_id=? LIMIT 1)', ('x' * 3000, run))
        response = client.get(f'/api/v1/runs/{run}/export?format=csv')
        detail = assert_error(response, 413, 'EXPORT_TOO_LARGE')
        assert detail['max_bytes' if limit == 'bytes' else 'max_rows'] == (2048 if limit == 'bytes' else 1)
        assert not list(exporter.OUTPUT_ROOT.glob('.export-*'))
        with database_transaction(db, readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='EXPORT_GENERATED' AND outcome='SUCCESS'").fetchone()[0] == 0
    finally:
        client.close()


def test_formula_whitespace_is_escaped_but_negative_units_stay_numeric(b07, monkeypatch, tmp_path):
    client, db, users, run, item = _run_and_difference(b07)
    monkeypatch.setattr(exporter, 'OUTPUT_ROOT', tmp_path / 'exports')
    risky = ' \t\r\n=SUM(1,2)'
    try:
        with database_transaction(db) as c:
            c.execute('UPDATE import_rows SET description=? WHERE id=(SELECT cancel_id FROM differences WHERE id=?)', (risky, item['id']))
        response = client.get(f'/api/v1/runs/{run}/export?format=csv')
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8'))))
        row = next(r for r in rows if r['difference_id'] == item['id'])
        assert row['description'] == "'" + risky
        assert row['source_quantity'].startswith('-') and row['source_quantity'][1:].isdigit()
        assert row['source_amount_units'].startswith('-') and row['source_amount_units'][1:].isdigit()
        for prefix in (' ', '\t', '\r\n', ' '):
            for operator in ('=', '+', '-', '@'):
                assert exporter.safe_text(prefix + operator + 'bad') == "'" + prefix + operator + 'bad'
        assert not list(exporter.OUTPUT_ROOT.glob('.export-*'))
    finally:
        client.close()
