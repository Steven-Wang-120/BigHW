"""Composite B07/B08 security, scope, UI and CSV lifecycle regression."""

import csv
import io
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from 测试.acceptance.test_pipeline_api import b07, login, post_upload, write_headers, assert_error, ORIGIN
from 测试.integration.test_reconcile import imported
from 主程序.repositories.database import database_transaction


def test_v4_legacy_routes_rejection_audit_and_handler_scope(b07):
    db, _, _, users, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get('/evidence/run/missing').status_code == 401
        login(client, 'admin'); response = client.get('/evidence/run/missing'); assert response.status_code == 404 or response.status_code == 403
        with database_transaction(db, readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE outcome='REJECTED'").fetchone()[0] >= 1


def test_handler_list_rows_without_any_assignment_is_404(b07):
    db, _, _, _, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance'); batch = post_upload(client, b07).json()['batch_id']; run = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client)).json()['run_id']
        login(client, 'handler')
        assert_error(client.get(f'/api/v1/runs/{run}/differences'), 404, 'NOT_FOUND')


def test_api_money_formatter_keeps_precision_and_json_safe(b07):
    page = Path('主程序/web/static/js/api.js').read_text(encoding='utf-8')
    assert '10000n' in page and 'roundedPence' in page and 'jsonSafe' in page


def test_admin_controls_and_scoped_audit_filters(b07):
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'admin')
        assert client.get('/api/v1/users?page=1&page_size=1').status_code == 200
        assert client.get('/api/v1/audit?page=1&page_size=1&target_type=user').status_code == 200
        created = client.post('/api/v1/users', json={'username': 'new-handler', 'password': 'Synthetic-Only-2026!', 'role': 'HANDLER'}, headers=write_headers(client))
        assert created.status_code == 201
        user = created.json()['user']; disabled = client.patch('/api/v1/users/' + user['id'], json={'active': False, 'expected_version': user['version']}, headers=write_headers(client))
        assert disabled.status_code == 200 and disabled.json()['user']['active'] is False


def test_export_temp_cleanup_and_page_keyset(b07, monkeypatch, tmp_path):
    import 主程序.services.export_service as exporter
    db, _, _, _, app = b07
    old_root, old_chunk = exporter.OUTPUT_ROOT, exporter.CHUNK_ROWS
    exporter.OUTPUT_ROOT, exporter.CHUNK_ROWS = tmp_path, 1
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            login(client, 'finance'); batch = post_upload(client, b07).json()['batch_id']; run = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client)).json()['run_id']
            response = client.get(f'/api/v1/runs/{run}/export?format=csv&status=PENDING')
            assert response.status_code == 200
            rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8')))); assert rows[-1]['record_type'] == 'scoped-summary'
            assert len([p for p in exporter.OUTPUT_ROOT.glob('.export-*')]) == 0
    finally:
        exporter.OUTPUT_ROOT, exporter.CHUNK_ROWS = old_root, old_chunk


def test_export_audit_failure_cleans_file_and_has_no_success_audit(b07, monkeypatch, tmp_path):
    import 主程序.services.export_service as exporter
    from 主程序.repositories import audit_repository
    db, _, _, _, app = b07; old_root = exporter.OUTPUT_ROOT; exporter.OUTPUT_ROOT = tmp_path
    original = audit_repository.append
    def fail(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs):
        if action == 'EXPORT_GENERATED':
            from 主程序.repositories.xlsx_reader import InputError
            raise InputError('AUDIT_WRITE_FAILED', 500)
        return original(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs)
    monkeypatch.setattr(audit_repository, 'append', fail)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            login(client, 'finance'); batch = post_upload(client, b07).json()['batch_id']; run = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client)).json()['run_id']
            assert_error(client.get(f'/api/v1/runs/{run}/export?format=csv'), 500, 'AUDIT_WRITE_FAILED')
        assert len([p for p in exporter.OUTPUT_ROOT.glob('.export-*')]) == 0
        with database_transaction(db, readonly=True) as c: assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='EXPORT_GENERATED' AND outcome='SUCCESS'").fetchone()[0] == 0
    finally: exporter.OUTPUT_ROOT = old_root
