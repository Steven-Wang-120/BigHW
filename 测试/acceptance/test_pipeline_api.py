"""B07 formal import/pipeline/run API on synthetic snapshots only."""

import hashlib
import json

from starlette.datastructures import UploadFile

from fastapi.testclient import TestClient
import pytest

from 测试.integration.test_reconcile import imported
from 测试.integration.test_workflow import PASSWORD
from 主程序.repositories.database import database_transaction, initialize_database, upgrade_database
from 主程序.services.auth_service import create_user
from 主程序.domain.permissions import Actor
from 主程序.web.app import create_app
from 主程序.services.snapshot_catalog import Registration, SnapshotCatalog

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def b07(imported):
    base_db, _ = imported
    # Use a fresh synthetic database so this API fixture has no pre-existing
    # import fact that would correctly trigger DUPLICATE_IMPORT.
    db = base_db.parent / "api.sqlite3"
    initialize_database(db)
    upgrade_database(db, target_version=2)
    upgrade_database(db, target_version=3)
    # The fixture snapshot is synthetic and is explicitly registered only for
    # this test; no production catalog is mutated.
    source = next(base_db.parent.glob("source.xlsx"))
    snapshot = base_db.parent / "snapshot"
    manifest = snapshot / "snapshot_10k.json"
    registration = Registration(
        json.loads(manifest.read_text(encoding="utf-8"))["snapshot_id"],
        snapshot.name,
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    upgrade_database(db, target_version=4)
    admin = create_user(db, None, "admin", PASSWORD, "ADMIN", "api-admin", bootstrap=True)
    users = {"ADMIN": admin}
    actor = Actor(admin["id"], "ADMIN")
    for role in ("FINANCE", "SUPERVISOR", "HANDLER"):
        users[role] = create_user(db, actor, role.lower(), PASSWORD, role, "api-" + role)
    catalog = SnapshotCatalog(root=db.parent, source=source, expected_sha=hashlib.sha256(source.read_bytes()).hexdigest(), registrations=(registration,))
    app = create_app(db, app_origin=ORIGIN, snapshot_catalog=catalog)
    return db, snapshot, registration, users, app


def login(client, username):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text


def write_headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["csrf_token"]}


def post_upload(client, b07, *, endpoint="imports", fields=None, content=None, extra_files=()):
    _, snapshot, registration, _, _ = b07
    values = {"snapshot_id": registration.snapshot_id, "manifest_digest": registration.manifest_digest}
    if endpoint == "pipeline":
        values["matching_rule_version"] = "conservative-v1-90d"
    if fields:
        values.update(fields)
    files = [(k, (None, v)) for k, v in values.items()]
    csv = (snapshot / "retail_2010_rows_2_10001.csv").read_bytes() if content is None else content
    files.append(("file", ("rows.csv", csv, "text/csv")))
    return client.post("/api/v1/" + endpoint, files=files + list(extra_files), headers=write_headers(client))


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    return response.json()["error"]["details"]


def test_form_security_and_snapshot_catalog(b07):
    _, snapshot, registration, users, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, "finance")
        assert client.get("/api/v1/snapshots").json()["items"][0]["snapshot_id"] == registration.snapshot_id
        response = client.post("/api/v1/imports", data={"snapshot_id": registration.snapshot_id, "manifest_digest": registration.manifest_digest, "extra": "x"}, files={"file": ("rows.csv", b"bad", "text/csv")}, headers=write_headers(client))
        assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
        response = client.post("/api/v1/imports", data={"snapshot_id": registration.snapshot_id, "manifest_digest": "0" * 64}, files={"file": ("rows.csv", b"bad", "text/csv")}, headers=write_headers(client))
        assert response.status_code == 422 and response.json()["error"]["code"] == "MANIFEST_MISMATCH"


def test_pipeline_import_and_run_reuse(b07):
    _, snapshot, registration, users, app = b07
    csv = (snapshot / "retail_2010_rows_2_10001.csv").read_bytes()
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, "finance")
        fields = {"snapshot_id": registration.snapshot_id, "manifest_digest": registration.manifest_digest, "matching_rule_version": "conservative-v1-90d"}
        response = client.post("/api/v1/pipeline", data=fields, files={"file": ("rows.csv", csv, "text/csv")}, headers=write_headers(client))
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "COMPLETED"
        run = client.post("/api/v1/runs", json={"batch_id": payload["batch_id"], "matching_rule_version": "conservative-v1-90d"}, headers=write_headers(client))
        assert run.status_code == 200 and run.json()["reused"] is True
        assert client.get("/api/v1/imports/" + payload["batch_id"]).status_code == 200
        assert client.get("/api/v1/runs/" + payload["run_id"]).json()["status"] == "COMPLETED"
        duplicate = post_upload(client, b07)
        assert assert_error(duplicate, 409, "DUPLICATE_IMPORT")["batch_id"] == payload["batch_id"]
        with database_transaction(b07[0], readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
            events = c.execute("SELECT actor_id,request_id FROM audit_events WHERE action IN ('IMPORT','RECONCILE') AND outcome='SUCCESS'").fetchall()
            assert events == [(users['FINANCE']['id'], response.headers['X-Request-ID'])] * 2


@pytest.mark.parametrize("role", ["ADMIN", "SUPERVISOR", "HANDLER"])
def test_security_role_before_bad_form(b07, role):
    with TestClient(b07[4], base_url=ORIGIN) as client:
        assert_error(client.post('/api/v1/imports', content=b'bad'), 401, 'SESSION_EXPIRED')
        login(client, b07[3][role]['username'])
        assert_error(client.post('/api/v1/imports', content=b'bad'), 403, 'ORIGIN_FAILED')
        assert_error(client.post('/api/v1/imports', content=b'bad', headers={'Origin': ORIGIN}), 403, 'CSRF_FAILED')
        for path in ('imports', 'pipeline', 'runs'):
            assert_error(client.post('/api/v1/' + path, content=b'bad', headers=write_headers(client)), 403, 'FORBIDDEN')
        if role != 'FINANCE':
            assert_error(client.get('/api/v1/snapshots?role=FINANCE'), 403, 'FORBIDDEN')


@pytest.mark.parametrize("kind", ['extra', 'duplicate_text', 'duplicate_file', 'wrong_part', 'json', 'query', 'unknown_snapshot', 'wrong_csv', 'empty'])
def test_strict_multipart_and_trust_no_batch(b07, kind):
    db, _, reg, _, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        code, status = 'VALIDATION_ERROR', 422
        if kind == 'extra':
            response = post_upload(client, b07, fields={'path': 'D:/private'})
        elif kind == 'duplicate_text':
            response = post_upload(client, b07, extra_files=[('snapshot_id', (None, reg.snapshot_id))])
        elif kind == 'duplicate_file':
            response = post_upload(client, b07, extra_files=[('file', ('other.csv', b'x', 'text/csv'))])
        elif kind == 'wrong_part':
            response = client.post('/api/v1/imports', files={'file': (None, 'not a file'), 'snapshot_id': (None, reg.snapshot_id), 'manifest_digest': (None, reg.manifest_digest)}, headers=write_headers(client))
        elif kind == 'json':
            response = client.post('/api/v1/imports', json={}, headers=write_headers(client))
        elif kind == 'query':
            response = client.post('/api/v1/imports?path=x', content=b'bad', headers=write_headers(client))
        elif kind == 'unknown_snapshot':
            response = post_upload(client, b07, fields={'snapshot_id': '0' * 64})
            code, status = 'SNAPSHOT_NOT_FOUND', 404
        elif kind == 'wrong_csv':
            response = post_upload(client, b07, content=b'not,the,trusted,csv\n')
            code = 'SNAPSHOT_HASH_MISMATCH'
        else:
            response = post_upload(client, b07, content=b'')
            code, status = 'CORRUPT_FILE', 400
        assert_error(response, status, code)
        assert 'D:/private' not in response.text and PASSWORD not in response.text
        with database_transaction(db, readonly=True) as c:
            assert c.execute('SELECT COUNT(*) FROM import_batches').fetchone()[0] == 0


def test_size_limits_and_close_on_success_error_and_stream_limit(b07, monkeypatch):
    from 主程序.web import uploads
    files = []
    original = uploads.BoundedParser.on_headers_finished
    def capture(parser):
        original(parser)
        if parser._current_part.file:
            files.append(parser._current_part.file.file)
    monkeypatch.setattr(uploads.BoundedParser, 'on_headers_finished', capture)
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'finance')
        monkeypatch.setattr(uploads, 'MAX_FILE_BYTES', 16)
        assert_error(post_upload(client, b07, content=b'x' * 17), 413, 'UPLOAD_TOO_LARGE')
        assert files and all(f.closed for f in files)
        monkeypatch.setattr(uploads, 'MAX_BODY_BYTES', 128)
        assert_error(post_upload(client, b07, content=b'x' * 1000), 413, 'UPLOAD_TOO_LARGE')
        assert all(f.closed for f in files)


@pytest.mark.parametrize('target', ['snapshot_10k.json', 'full_scan.json', 'retail_2010_rows_2_10001.csv'])
def test_registered_local_tampering_rejected(b07, target):
    db, directory, _, _, app = b07
    path = directory / target
    path.write_bytes(path.read_bytes() + b' ')
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        response = post_upload(client, b07)
        assert response.status_code == 422
        assert str(path) not in response.text
        with database_transaction(db, readonly=True) as c:
            assert c.execute('SELECT COUNT(*) FROM import_batches').fetchone()[0] == 0


def test_run_new_reuse_and_pipeline_failure_same_run_retry(b07, monkeypatch):
    from 主程序.repositories import reconcile_repository
    db, _, _, users, app = b07
    original = reconcile_repository.publish
    def failed(c, run_id, batch_id, *args, **kwargs):
        original(c, run_id, batch_id, *args, **kwargs)
        raise RuntimeError('synthetic publish failure')
    monkeypatch.setattr(reconcile_repository, 'publish', failed)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        details = assert_error(post_upload(client, b07, endpoint='pipeline'), 500, 'RECONCILE_WRITE_FAILED')
        assert details['retry_action'] == 'POST_RUNS' and details['attempt'] == 1
        batch, run = details['batch_id'], details['run_id']
        state = client.get('/api/v1/runs/' + run).json()
        assert state['status'] == 'FAILED' and 'matched' not in state and 'summary' not in state
        assert client.get('/api/v1/imports/' + batch).json()['status'] == 'SUCCESS'
        with database_transaction(db, readonly=True) as c:
            assert c.execute('SELECT COUNT(*) FROM allocations').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM differences').fetchone()[0] == 0
        monkeypatch.setattr(reconcile_repository, 'publish', original)
        response = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client))
        assert response.status_code == 200 and response.json()['run_id'] == run and response.json()['attempt'] == 2
        assert response.json()['stage'] == 'HTTP_RECONCILED'
        reused = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client))
        assert reused.status_code == 200 and reused.json()['reused'] is True
        with database_transaction(db, readonly=True) as c:
            assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM import_rows').fetchone()[0] == 5


def test_post_run_201_rule_and_read_roles(b07):
    db, _, _, _, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        batch = post_upload(client, b07).json()['batch_id']
        assert_error(client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'unapproved'}, headers=write_headers(client)), 422, 'RULE_UNCONFIRMED')
        response = client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client))
        assert response.status_code == 201, response.text
        run = response.json()['run_id']
        login(client, 'supervisor')
        assert client.get('/api/v1/imports/' + batch + '/counts').json()['delta_selected'] == 0
        assert client.get('/api/v1/runs/' + run).status_code == 200
        login(client, 'handler')
        assert_error(client.get('/api/v1/imports/' + batch), 403, 'FORBIDDEN')
        assert_error(client.get('/api/v1/runs/' + run), 404, 'NOT_FOUND')
        login(client, 'admin')
        assert_error(client.get('/api/v1/runs/' + run), 403, 'FORBIDDEN')


@pytest.mark.parametrize('phase', ['precheck', 'publish'])
def test_session_revocation_serializes_against_import(b07, monkeypatch, phase):
    from 主程序.services import import_service
    db, _, _, users, app = b07
    if phase == 'precheck':
        original = app.state.auth_service.csrf
        def revoke(identity, token):
            original(identity, token)
            with database_transaction(db) as c:
                c.execute("UPDATE sessions SET revoked_at='2026-10-16' WHERE id=?", (identity.session_id,))
        monkeypatch.setattr(app.state.auth_service, 'csrf', revoke)
    else:
        original = import_service.derive_counts
        def deactivate(*args, **kwargs):
            result = original(*args, **kwargs)
            with database_transaction(db) as c:
                c.execute('UPDATE users SET active=0 WHERE id=?', (users['FINANCE']['id'],))
            return result
        monkeypatch.setattr(import_service, 'derive_counts', deactivate)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        assert_error(post_upload(client, b07), 401, 'SESSION_EXPIRED')
    with database_transaction(db, readonly=True) as c:
        assert c.execute('SELECT COUNT(*) FROM import_rows').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM import_batches WHERE status='SUCCESS'").fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['IMPORT', 'RECONCILE'])
def test_success_audit_failure_rolls_back_business(b07, monkeypatch, operation):
    from 主程序.repositories import audit_repository
    original = audit_repository.append
    def fail_success(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs):
        if action == operation and outcome == 'SUCCESS':
            from 主程序.repositories.xlsx_reader import InputError
            raise InputError('AUDIT_WRITE_FAILED', 500)
        return original(c, actor_id, request_id, target_type, target_id, action, outcome, **kwargs)
    monkeypatch.setattr(audit_repository, 'append', fail_success)
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'finance')
        assert_error(post_upload(client, b07, endpoint='pipeline'), 500, 'AUDIT_WRITE_FAILED')
    with database_transaction(b07[0], readonly=True) as c:
        assert c.execute('SELECT COUNT(*) FROM allocations').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM differences').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM runs WHERE status='COMPLETED'").fetchone()[0] == 0
        if operation == 'IMPORT':
            assert c.execute('SELECT COUNT(*) FROM import_rows').fetchone()[0] == 0


def test_rejection_audit_failure_explicit(b07, monkeypatch):
    from 主程序.repositories import audit_repository
    from 主程序.repositories.xlsx_reader import InputError
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'finance')
        def failed(*args, **kwargs):
            raise InputError('AUDIT_WRITE_FAILED', 500)
        monkeypatch.setattr(audit_repository, 'append', failed)
        assert_error(post_upload(client, b07, content=b'wrong'), 500, 'AUDIT_WRITE_FAILED')


def test_running_status_no_partial_and_http_conflict(b07):
    from 主程序.repositories.reconcile_repository import start_run
    db, _, _, _, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        batch = post_upload(client, b07).json()['batch_id']
        with database_transaction(db) as c:
            run, _, _ = start_run(c, batch)
        state = client.get('/api/v1/runs/' + run).json()
        assert state['status'] == 'RUNNING' and 'summary' not in state and 'matched' not in state
        assert_error(client.post('/api/v1/runs', json={'batch_id': batch, 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client)), 409, 'RUN_IN_PROGRESS')


@pytest.mark.parametrize('mime', ['application/vnd.ms-excel', 'text/plain', '', 'TEXT/CSV; charset=UTF-8', 'Application/Vnd.Ms-Excel ; charset=utf-8'])
def test_native_csv_mime_is_metadata_not_trust(b07, mime):
    _, directory, registration, _, app = b07
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, 'finance')
        fields = {'snapshot_id': registration.snapshot_id, 'manifest_digest': registration.manifest_digest}
        csv = (directory / 'retail_2010_rows_2_10001.csv').read_bytes()
        files = [('file', ('rows.CSV', csv, mime))]
        assert client.post('/api/v1/imports', data=fields, files=files, headers=write_headers(client)).status_code == 201
        files = [('file', ('rows.csv', b'fabricated', mime))]
        assert_error(client.post('/api/v1/imports', data=fields, files=files, headers=write_headers(client)), 422, 'SNAPSHOT_HASH_MISMATCH')


@pytest.mark.parametrize('failure', ['input', 'database'])
def test_pipeline_failure_before_run_created_preserves_import(b07, monkeypatch, failure):
    from 主程序.services import pipeline_service
    from 主程序.repositories.database import DatabaseError
    from 主程序.repositories.xlsx_reader import InputError
    original = pipeline_service.start_run
    def failed(*args, **kwargs):
        raise DatabaseError('DATABASE_BUSY') if failure == 'database' else InputError('RUN_START_UNAVAILABLE', 503)
    monkeypatch.setattr(pipeline_service, 'start_run', failed)
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'finance')
        details = assert_error(post_upload(client, b07, endpoint='pipeline'), 503, 'DATABASE_BUSY' if failure == 'database' else 'RUN_START_UNAVAILABLE')
        assert details['run_id'] is None and details['attempt'] is None and details['retry_action'] == 'POST_RUNS'
        with database_transaction(b07[0], readonly=True) as c:
            assert c.execute('SELECT status FROM import_batches WHERE id=?', (details['batch_id'],)).fetchone() == ('SUCCESS',)
            assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
        monkeypatch.setattr(pipeline_service, 'start_run', original)
        response = client.post('/api/v1/runs', json={'batch_id': details['batch_id'], 'matching_rule_version': 'conservative-v1-90d'}, headers=write_headers(client))
        assert response.status_code == 201 and response.json()['attempt'] == 1


def test_snapshots_strict_pagination_and_run_fields(b07):
    with TestClient(b07[4], base_url=ORIGIN) as client:
        login(client, 'finance')
        for query in ('page=1&page=2', 'page_size=101', 'page=' + '9' * 5000, 'path=private'):
            assert_error(client.get('/api/v1/snapshots?' + query), 422, 'VALIDATION_ERROR')
        listing = client.get('/api/v1/snapshots?page=2&page_size=1').json()
        assert listing['items'] == [] and listing['total'] == 1
        for content in ('{"batch_id":"x","batch_id":"y","matching_rule_version":"conservative-v1-90d"}',
                        '{"batch_id":1,"matching_rule_version":"conservative-v1-90d"}',
                        '{"batch_id":"x","matching_rule_version":"conservative-v1-90d","role":"FINANCE"}'):
            assert_error(client.post('/api/v1/runs', content=content, headers={**write_headers(client), 'Content-Type': 'application/json'}), 422, 'VALIDATION_ERROR')
