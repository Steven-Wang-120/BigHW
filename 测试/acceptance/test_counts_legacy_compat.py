"""Legacy counts UI compatibility and v4 authenticated page split, synthetic only."""
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest

from 测试.integration.test_reconcile import imported
from 测试.integration.test_workflow import workdb
from 测试.acceptance.test_session_api import login, ORIGIN
from 主程序.repositories.database import initialize_database, upgrade_database, database_transaction
from 主程序.web.app import create_app


class Controls(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids, self.scripts = set(), []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'id' in values:
            self.ids.add(values['id'])
        if tag == 'script':
            self.scripts.append(values.get('src'))


@pytest.mark.parametrize('version', [1, 2, 3])
def test_legacy_counts_controls_match_served_script_without_v4_session_dependency(tmp_path, version):
    db = tmp_path / 'legacy.sqlite3'
    initialize_database(db)
    if version > 1:
        upgrade_database(db, target_version=version)
    before = db.read_bytes()
    with TestClient(create_app(db), base_url='http://127.0.0.1:8000') as client:
        response = client.get('/evidence/counts?batch_id=synthetic-batch')
        assert response.status_code == 200
        controls = Controls(response.text)
        assert controls.scripts == ['/static/js/counts.js']
        assert {'batch', 'timestamp', 'load', 'status', 'totals', 'categories', 'coverage', 'quality', 'runs'} <= controls.ids
        script = client.get(controls.scripts[0])
        assert script.status_code == 200
        assert set(re.findall(r"\$\('([^']+)'\)", script.text)) <= controls.ids
        assert "['totals','categories','coverage','quality']" in script.text
        assert "get('batch_id')" in script.text
        assert "fetch('/evidence/counts/'" in script.text
        assert "$('load').addEventListener('click',load)" in script.text
        assert client.get('/static/css/counts.css').status_code == 200
        assert '尚未完成' in response.text and '不把缺失结果当作0' in response.text
        for content in (response.text, script.text):
            assert 'appApi.me' not in content
            assert '/assets/js/api.js' not in content
            assert '/api/v1/auth' not in content
            assert "location.assign('/login')" not in content
    assert db.read_bytes() == before


@pytest.mark.parametrize('version', [2, 3])
def test_legacy_counts_ui_still_reads_published_import_facts_without_session(imported, version):
    db, batch = imported
    if version == 3:
        upgrade_database(db, target_version=3)
    before = db.read_bytes()
    with TestClient(create_app(db)) as client:
        page = client.get('/evidence/counts?batch_id=' + batch)
        assert page.status_code == 200 and 'id="totals"' in page.text
        response = client.get('/evidence/counts/' + batch)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['batch_id'] == batch
        assert data['selected_count'] == data['sale_count'] + data['cancel_count'] + data['excluded_count'] == 5
        assert data['delta_original'] == data['delta_selected'] == 0
        assert data['runs'] == []
    assert db.read_bytes() == before


def test_v4_counts_page_uses_business_api_and_legacy_json_keeps_auth_and_rejection_audit(workdb):
    db, run, _, users = workdb
    with database_transaction(db, readonly=True) as c:
        batch = c.execute('SELECT batch_id FROM runs WHERE id=?', (run,)).fetchone()[0]
    with TestClient(create_app(db, app_origin=ORIGIN), base_url=ORIGIN) as client:
        page = client.get('/evidence/counts?batch_id=' + batch)
        assert page.status_code == 200
        assert page.text == client.get('/counts?batch_id=' + batch).text
        controls = Controls(page.text)
        assert controls.scripts == ['/assets/js/api.js', '/assets/js/business-counts.js']
        assert {'count-form', 'batch-id', 'message', 'counts'} <= controls.ids
        assert '/static/js/counts.js' not in page.text
        assert '尚未完成' not in page.text
        script = client.get('/assets/js/business-counts.js').text
        assert "appApi.me(['FINANCE', 'SUPERVISOR'])" in script
        assert '/imports/' in script and '/counts' in script
        def rejected(status, code):
            response = client.get('/evidence/counts/' + batch)
            assert response.status_code == status, response.text
            payload = response.json()
            assert payload['error']['code'] == code
            assert payload['request_id'] == response.headers['X-Request-ID']
            with database_transaction(db, readonly=True) as c:
                assert c.execute('SELECT outcome,error_code FROM audit_events WHERE request_id=?',
                                 (payload['request_id'],)).fetchall() == [('REJECTED', code)]
        rejected(401, 'SESSION_EXPIRED')
        for username in ('admin', 'handler'):
            login(client, username)
            rejected(403, 'FORBIDDEN')
        for username in ('finance', 'supervisor'):
            login(client, username)
            legacy = client.get('/evidence/counts/' + batch)
            formal = client.get('/api/v1/imports/' + batch + '/counts')
            assert legacy.status_code == formal.status_code == 200
            assert legacy.json()['selected_count'] == formal.json()['selected_count'] == 5


@pytest.mark.parametrize('kind', ['missing', 'unrecognized', 'unsupported'])
def test_counts_page_does_not_initialize_or_migrate_unknown_database(tmp_path, kind):
    db = tmp_path / 'unknown.sqlite3'
    if kind == 'unrecognized':
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE foreign_data (value TEXT)')
            c.execute('PRAGMA user_version=2')
    elif kind == 'unsupported':
        initialize_database(db)
        with sqlite3.connect(db) as c:
            c.execute('PRAGMA user_version=5')
    before = db.read_bytes() if db.exists() else None
    with TestClient(create_app(db)) as client:
        response = client.get('/evidence/counts')
        assert response.status_code == 500
        assert response.json()['error']['code'] == {'missing': 'DATABASE_NOT_INITIALIZED',
                'unrecognized': 'SCHEMA_UNRECOGNIZED', 'unsupported': 'SCHEMA_VERSION_UNSUPPORTED'}[kind]
        assert str(db) not in response.text
    if before is None:
        assert not db.exists()
    else:
        assert db.read_bytes() == before
    assert not Path(str(db) + '.lock').exists()
