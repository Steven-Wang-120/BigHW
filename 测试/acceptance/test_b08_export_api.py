"""B08 scoped export, pagination and native static route tests."""

import csv
import io

from fastapi.testclient import TestClient
import pytest

from 测试.acceptance.test_pipeline_api import b07, login, post_upload, write_headers, assert_error, ORIGIN
from 测试.integration.test_reconcile import imported
from 主程序.repositories.database import database_transaction
from 主程序.services.workflow_service import change
from 主程序.domain.permissions import Actor


def test_native_pages_and_static_allowlist(b07):
    with TestClient(b07[4], base_url=ORIGIN) as client:
        for path in ('/', '/login', '/import', '/results', '/differences', '/detail', '/counts', '/admin'):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert '<script' in response.text or path == '/'
        for path in ('/pages/../web/api.py', '/assets/js/secret.js', '/assets/css/../app.py', '/static/unknown.txt'):
            assert client.get(path).status_code in (404, 400)
        assert client.get('/evidence/counts').status_code == 200
        assert client.get('/static/js/counts.js').status_code == 200


def test_runs_users_scope_and_strict_pagination(b07):
    db, _, _, users, app = b07
    with TestClient(app, base_url=ORIGIN) as finance:
        login(finance, 'finance')
        batch = post_upload(finance, b07).json()['batch_id']
        run = finance.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(finance)).json()['run_id']
        assert finance.get('/api/v1/runs?page=1&page_size=1').json()['total'] == 1
        assert finance.get('/api/v1/runs?batch_id=' + batch).json()['items'][0]['run_id'] == run
        assert_error(finance.get('/api/v1/users'), 403, 'FORBIDDEN')
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin, 'admin')
        users_response = admin.get('/api/v1/users?page=1&page_size=100')
        assert users_response.status_code == 200
        assert all(set(item) == {'id', 'username', 'role', 'active', 'version'} for item in users_response.json()['items'])
        for query in ('page=1&page=2', 'page_size=101', 'batch_id=a&batch_id=b'):
            assert_error(admin.get('/api/v1/users?' + query), 422, 'VALIDATION_ERROR')
        assert_error(admin.get('/api/v1/runs'), 403, 'FORBIDDEN')
    with TestClient(app, base_url=ORIGIN) as handler:
        login(handler, 'handler')
        assert handler.get('/api/v1/runs').json()['items'] == []
        assert_error(handler.get('/api/v1/users'), 403, 'FORBIDDEN')


def _run_and_difference(b07):
    db, _, _, users, app = b07
    client = TestClient(app, base_url=ORIGIN)
    login(client, 'finance')
    batch = post_upload(client, b07).json()['batch_id']
    run = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client)).json()['run_id']
    item = client.get('/api/v1/runs/' + run + '/differences').json()['items'][0]
    return client, db, users, run, item


def test_csv_export_stable_scoped_formula_safe_and_audited(b07):
    client, db, users, run, item = _run_and_difference(b07)
    try:
        with database_transaction(db) as c:
            c.execute("UPDATE import_rows SET description=? WHERE id=(SELECT cancel_id FROM differences WHERE id=?)", ('=SUM(1)', item['id']))
        response = client.get('/api/v1/runs/' + run + '/export?format=csv')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/csv')
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8'))))
        assert rows[0]['record_type'] == 'metadata'
        assert rows[-1]['record_type'] == 'scoped-summary'
        assert rows[-1]['scoped_count'] == '2'
        assert all(row['scope'] == 'AUTHORIZED_FILTER' for row in rows)
        assert any(row['description'] == "'=SUM(1)" for row in rows)
        assert any(row['amount_units'].lstrip('-').isdigit() for row in rows if row['record_type'] == 'difference')
        with database_transaction(db, readonly=True) as c:
            audit = c.execute("SELECT action,outcome,summary_json FROM audit_events WHERE action='EXPORT_GENERATED'").fetchall()
            assert len(audit) == 1 and audit[0][0:2] == ('EXPORT_GENERATED', 'SUCCESS')
            assert 'server_generated' in audit[0][2] and 'saved' not in audit[0][2]
    finally:
        client.close()


def test_csv_export_handler_only_assigned_and_admin_forbidden(b07):
    client, db, users, run, item = _run_and_difference(b07)
    try:
        supervisor = TestClient(b07[4], base_url=ORIGIN)
        login(supervisor, 'supervisor')
        assigned = supervisor.post('/api/v1/differences/' + item['id'] + '/assign', json={'assignee_id': users['HANDLER']['id'], 'expected_version': 0}, headers=write_headers(supervisor))
        assert assigned.status_code == 200, assigned.text
        handler = TestClient(b07[4], base_url=ORIGIN)
        login(handler, 'handler')
        assert handler.get('/api/v1/runs').json()['total'] == 1
        export = handler.get('/api/v1/runs/' + run + '/export?format=csv&assignee_id=other')
        assert export.status_code == 200 and export.headers['X-Export-Scope'] == 'ASSIGNED_ONLY'
        rows = list(csv.DictReader(io.StringIO(export.content.decode('utf-8'))))
        assert rows[-1]['scoped_count'] == '1'
        assert all(row['scope'] == 'ASSIGNED_ONLY' for row in rows)
        admin = TestClient(b07[4], base_url=ORIGIN)
        login(admin, 'admin')
        assert_error(admin.get('/api/v1/runs/' + run + '/export?format=csv'), 403, 'FORBIDDEN')
        handler.close(); supervisor.close(); admin.close()
    finally:
        client.close()


def test_csv_export_rejects_unknown_format_and_audit_failure_no_file(b07, monkeypatch):
    client, db, users, run, _ = _run_and_difference(b07)
    try:
        assert_error(client.get('/api/v1/runs/' + run + '/export?format=json'), 422, 'VALIDATION_ERROR')
        from 主程序.repositories import audit_repository
        original = audit_repository.append
        def fail_export(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs):
            if action == 'EXPORT_GENERATED':
                from 主程序.repositories.xlsx_reader import InputError
                raise InputError('AUDIT_WRITE_FAILED', 500)
            return original(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs)
        monkeypatch.setattr(audit_repository, 'append', fail_export)
        assert_error(client.get('/api/v1/runs/' + run + '/export?format=csv'), 500, 'AUDIT_WRITE_FAILED')
        with database_transaction(db, readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='EXPORT_GENERATED'").fetchone()[0] == 0
    finally:
        client.close()


def test_csv_export_revoked_session_does_not_send(b07):
    client, db, _, run, _ = _run_and_difference(b07)
    try:
        with database_transaction(db) as c:
            c.execute("UPDATE sessions SET revoked_at='2026-10-16T00:00:00+00:00' WHERE token_hash=(SELECT token_hash FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='finance') ORDER BY id LIMIT 1)")
        assert_error(client.get('/api/v1/runs/' + run + '/export?format=csv'), 401, 'SESSION_EXPIRED')
    finally:
        client.close()
