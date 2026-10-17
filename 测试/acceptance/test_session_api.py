"""TC33-43 / 41.a-l real session HTTP, using only synthetic tmp data."""

import hashlib
from http.cookies import SimpleCookie
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from 测试.integration.test_reconcile import imported
from 测试.integration.test_workflow import workdb, PASSWORD, actors
from 主程序.repositories.database import database_transaction, connect_database, DatabaseError
from 主程序.repositories import workflow_repository as repository
from 主程序.services.workflow_service import change
from 主程序.services.auth_service import digest, LoginLimiter, create_user
from 主程序.repositories.xlsx_reader import InputError
from 主程序.web.app import create_app

ORIGIN = "http://127.0.0.1:8765"


def login(client, username):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["csrf_token"]}


def error(response, status, code):
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["error"]["code"] == code, payload
    assert isinstance(payload["error"]["message"], str) and isinstance(payload["error"]["details"], dict)
    assert payload["request_id"] == response.headers["X-Request-ID"]
    return payload


@pytest.fixture
def http(workdb):
    db, run, ids, users = workdb
    app = create_app(db, app_origin=ORIGIN)
    clients = {key: TestClient(app, base_url=ORIGIN) for key in users}
    for key, client in clients.items():
        login(client, users[key]["username"])
    yield db, run, ids, users, app, clients
    for client in clients.values():
        client.close()


def test_tc33_34_37_38_39_40_http_actual_closed_loop(http):
    db, run, ids, users, app, clients = http
    sup, handler = clients["SUPERVISOR"], clients["HANDLER"]
    did = ids[0]
    path = "/api/v1/differences/" + did
    assigned = sup.post(path + "/assign", json={"assignee_id": users["HANDLER"]["id"], "expected_version": 0}, headers=headers(sup))
    assert assigned.status_code == 200 and assigned.json()["version"] == 1 and assigned.json()["work_status"] == "PENDING"
    for who in ("HANDLER", "H2"):
        response = sup.post(path + "/assign", json={"assignee_id": users[who]["id"], "expected_version": 0}, headers=headers(sup))
        assert error(response, 409, "ALREADY_ASSIGNED")["error"]["details"]["current_assignee"] == users["HANDLER"]["id"]
    error(handler.post(path + "/close", json={"expected_version": 1, "resolution_note": "checked"}, headers=headers(handler)), 409, "INVALID_TRANSITION")
    assert handler.post(path + "/start", json={"expected_version": 1}, headers=headers(handler)).json()["version"] == 2
    error(handler.post(path + "/close", json={"expected_version": 2, "resolution_note": " \t "}, headers=headers(handler)), 422, "EMPTY_RESOLUTION")
    error(handler.post(path + "/close", json={"expected_version": 1, "resolution_note": " \t "}, headers=headers(handler)), 409, "VERSION_CONFLICT")
    closed = handler.post(path + "/close", json={"expected_version": 2, "resolution_note": " checked "}, headers=headers(handler))
    assert closed.status_code == 200 and closed.json()["version"] == 3
    assert closed.json()["resolution_note"] == "checked" and closed.json()["match_status"] == "UNMATCHED"
    error(handler.post(path + "/start", json={"expected_version": 3}, headers=headers(handler)), 409, "INVALID_TRANSITION")
    detail = handler.get(path).json()
    assert detail["work_status"] == "CLOSED" and len(detail["history"]) == 3
    assert all(key in detail for key in ("source", "rule", "flags", "candidates", "request_id"))
    assert type(detail["source"]["amount_units"]) is str
    handlers = sup.get("/api/v1/users/handlers").json()["items"]
    assert {u["id"] for u in handlers} == {users[k]["id"] for k in ("HANDLER", "H2")}
    assert all(set(u) == {"id", "name"} for u in handlers)


def test_tc41a_handler_scope_forced_despite_query_and_injection(http):
    db, run, ids, users, _, clients = http
    sup = actors(users, "SUPERVISOR")
    for did, key in zip(ids, ("HANDLER", "H2")):
        change(db, sup, did, "assign", 0, "scope-" + key, assignee_id=users[key]["id"])
    handler = clients["HANDLER"]
    url = f"/api/v1/runs/{run}/differences"
    for query in ({}, {"assignee_id": users["H2"]["id"]}, {"assignee_id": "' OR 1=1--"}):
        listing = handler.get(url, params=query)
        assert listing.status_code == 200 and listing.json()["total"] == 1
        assert [item["id"] for item in listing.json()["items"]] == ids[:1]
    for method, suffix, data in (("GET", "", None), ("POST", "/start", {"expected_version": 1}), ("POST", "/close", {"expected_version": 1, "resolution_note": "x"})):
        response = handler.request(method, "/api/v1/differences/" + ids[1] + suffix, json=data, headers=headers(handler))
        error(response, 404, "NOT_FOUND")
    assert clients["FINANCE"].get(url).json()["total"] == 2
    filtered = clients["FINANCE"].get(url, params={"assignee_id": users["H2"]["id"], "status": "PENDING", "page_size": 1})
    assert filtered.json()["total"] == 1 and filtered.json()["items"][0]["id"] == ids[1]


def test_tc41b_c_h_role_before_shape_and_admin_not_superuser(http):
    db, _, ids, users, app, clients = http
    path = "/api/v1/differences/" + ids[0]
    payload = {"assignee_id": users["HANDLER"]["id"], "expected_version": 0}
    error(clients["HANDLER"].post(path + "/assign", json=payload, headers=headers(clients["HANDLER"])), 403, "FORBIDDEN")
    sup = clients["SUPERVISOR"]
    for extra in ("role", "user_id"):
        response = sup.post(path + "/assign", json={**payload, extra: "SUPERVISOR"}, headers=headers(sup))
        assert extra in error(response, 422, "VALIDATION_ERROR")["error"]["details"]["fields"]
    admin = clients["ADMIN"]
    for event, data in (("assign", payload), ("start", {"expected_version": 0}), ("close", {"expected_version": 0, "resolution_note": "x"})):
        error(admin.post(path + "/" + event, json=data, headers=headers(admin)), 403, "FORBIDDEN")
    error(admin.get(path), 403, "FORBIDDEN")
    for key in ("FINANCE", "HANDLER"):
        error(clients[key].get("/api/v1/users/handlers"), 403, "FORBIDDEN")
    # Deferred endpoints enforce ADMIN denial without pretending the import/run
    # implementation is delivered; v1-3 retain historical 404 behavior.
    error(admin.post("/api/v1/imports", files={"file": ("synthetic.csv", b"x")}, headers=headers(admin)), 403, "FORBIDDEN")
    error(admin.post("/api/v1/runs", json={"batch_id": "x"}, headers=headers(admin)), 403, "FORBIDDEN")
    with database_transaction(db, readonly=True) as c:
        assert repository.get(c, ids[0])["version"] == 0
        assert c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0


def test_tc41d_disable_revokes_all_sessions_and_last_admin_protected(http):
    db, _, ids, users, app, clients = http
    handler, admin = clients["HANDLER"], clients["ADMIN"]
    other = TestClient(app, base_url=ORIGIN)
    login(other, "handler")
    change(db, actors(users, "SUPERVISOR"), ids[0], "assign", 0, "disable-prep", assignee_id=users["HANDLER"]["id"])
    response = admin.patch("/api/v1/users/" + users["HANDLER"]["id"], json={"active": False, "expected_version": 0}, headers=headers(admin))
    assert response.status_code == 200 and response.json()["user"]["active"] is False
    for old in (handler, other):
        error(old.get("/api/v1/auth/me"), 401, "SESSION_EXPIRED")
        error(old.post("/api/v1/differences/" + ids[0] + "/start", json={"expected_version": 1}), 401, "SESSION_EXPIRED")
    error(admin.patch("/api/v1/users/" + users["ADMIN"]["id"], json={"active": False, "expected_version": 0}, headers=headers(admin)), 409, "LAST_ACTIVE_ADMIN")
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL", (users["HANDLER"]["id"],)).fetchone()[0] == 0
        assert repository.get(c, ids[0])["version"] == 1
    other.close()


def test_tc41e_logout_clear_same_attributes_and_old_cookie_401(http):
    db, _, ids, users, app, clients = http
    handler = clients["HANDLER"]
    old = dict(handler.cookies)
    response = handler.post("/api/v1/auth/logout", headers=headers(handler))
    assert response.status_code == 204 and response.content == b""
    for value in response.headers.get_list("set-cookie"):
        assert "Path=/" in value and "SameSite=strict" in value and "Domain=" not in value and "Max-Age=0" in value
        assert ("HttpOnly" in value) == value.startswith("session=")
    handler.cookies.update(old)
    error(handler.get("/api/v1/auth/me"), 401, "SESSION_EXPIRED")
    error(handler.post("/api/v1/auth/logout", headers=headers(handler)), 401, "SESSION_EXPIRED")


def test_tc41f_expired_server_record_first401(http):
    db, _, ids, users, _, clients = http
    handler = clients["HANDLER"]
    with database_transaction(db) as c:
        c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (users["HANDLER"]["id"],))
    error(handler.get("/api/v1/auth/me?role=ADMIN"), 401, "SESSION_EXPIRED")
    error(handler.post("/api/v1/differences/" + ids[0] + "/start", content=b"not-json"), 401, "SESSION_EXPIRED")


def test_tc41g_users_duplicate_invalid_role_no_secrets_and_version(http):
    db, _, _, users, _, clients = http
    admin = clients["ADMIN"]
    user = {"username": "created", "password": PASSWORD, "role": "HANDLER"}
    response = admin.post("/api/v1/users", json=user, headers=headers(admin))
    assert response.status_code == 201
    created = response.json()["user"]
    assert set(created) == {"id", "username", "role", "active", "version"}
    error(admin.post("/api/v1/users", json=user, headers=headers(admin)), 409, "DUPLICATE_USERNAME")
    for role in ("ROOT", "admin", None):
        error(admin.post("/api/v1/users", json={**user, "username": "new", "role": role}, headers=headers(admin)), 422, "VALIDATION_ERROR")
    error(admin.patch("/api/v1/users/" + created["id"], json={"active": False, "expected_version": 3}, headers=headers(admin)), 409, "VERSION_CONFLICT")
    with database_transaction(db, readonly=True) as c:
        stored = c.execute("SELECT password_hash FROM users WHERE id=?", (created["id"],)).fetchone()[0]
        assert stored.startswith("$2b$") and PASSWORD not in stored
        audit = " ".join(row[0] for row in c.execute("SELECT summary_json FROM audit_events"))
        assert PASSWORD not in audit and stored not in audit


@pytest.mark.parametrize("origin_value", [ORIGIN, "https://127.0.0.1:8765"])
def test_tc41i_cookie_hash_only_stable_me_and_secure_scheme(workdb, origin_value):
    db, _, _, _, = workdb
    app = create_app(db, app_origin=origin_value)
    with TestClient(app, base_url=origin_value) as client:
        response = client.post("/api/v1/auth/login", json={"username": "handler", "password": PASSWORD}, headers={"Origin": origin_value})
        assert response.status_code == 200 and set(response.json()) == {"user", "request_id"}
        assert set(response.json()["user"]) == {"id", "username", "role"}
        token, csrf = client.cookies["session"], client.cookies["csrf_token"]
        assert token != csrf and len(token) >= 43 and len(csrf) >= 43
        for value in response.headers.get_list("set-cookie"):
            assert "Path=/" in value and "SameSite=strict" in value and "Domain=" not in value
            assert ("HttpOnly" in value) == value.startswith("session=")
            assert ("Secure" in value) == origin_value.startswith("https")
        for _ in range(3):
            me = client.get("/api/v1/auth/me")
            assert me.status_code == 200 and set(me.json()) == {"user", "request_id"}
            assert "set-cookie" not in me.headers
            assert client.cookies["session"] == token and client.cookies["csrf_token"] == csrf
        with database_transaction(db, readonly=True) as c:
            row = c.execute("SELECT token_hash,csrf_hash FROM sessions WHERE token_hash=?", (digest(token),)).fetchone()
            assert row == (digest(token), digest(csrf)) and token not in str(row) and csrf not in str(row)
            audit = str(c.execute("SELECT * FROM audit_events").fetchall())
            assert token not in audit and csrf not in audit and PASSWORD not in audit


def test_tc41j_login_origin_precedes_malformed_body_credentials_and_host_spoof(workdb):
    db, _, _, _ = workdb
    with TestClient(create_app(db, app_origin=ORIGIN), base_url=ORIGIN) as client:
        for value in (None, "null", "https://example.invalid", "http://127.0.0.1:8766", "https://127.0.0.1:8765", ORIGIN + "/"):
            hdr = {"Host": "example.invalid"}
            if value is not None:
                hdr["Origin"] = value
            error(client.post("/api/v1/auth/login", content=b"bad", headers=hdr), 403, "ORIGIN_FAILED")
        error(client.post("/api/v1/auth/login", json={"username": "handler", "password": PASSWORD}, headers={"Host": "evil.invalid", "Origin": "http://evil.invalid"}), 403, "ORIGIN_FAILED")
        with database_transaction(db, readonly=True) as c:
            assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert login(client, "handler").status_code == 200


def test_tc41k_l_server_bound_csrf_security_and_role_field_object_order(http):
    db, _, ids, users, app, clients = http
    handler, sup = clients["HANDLER"], clients["SUPERVISOR"]
    path = "/api/v1/differences/" + ids[0] + "/start"
    for csrf in (None, "wrong"):
        hdr = {"Origin": ORIGIN}
        if csrf:
            hdr["X-CSRF-Token"] = csrf
        error(handler.post(path, json={"expected_version": 0}, headers=hdr), 403, "CSRF_FAILED")
    handler.cookies.set("csrf_token", "forged", domain="127.0.0.1", path="/")
    error(handler.post(path, json={"expected_version": 0}, headers={"Origin": ORIGIN, "X-CSRF-Token": "forged"}), 403, "CSRF_FAILED")
    login(handler, "handler")
    for origin_value in (None, "null", "https://example.invalid"):
        hdr = {"X-CSRF-Token": handler.cookies["csrf_token"]}
        if origin_value:
            hdr["Origin"] = origin_value
        error(handler.post(path, json={"expected_version": 0}, headers=hdr), 403, "ORIGIN_FAILED")
    assert handler.get("/api/v1/auth/me").status_code == 200
    error(handler.post(path, json={"expected_version": 0}, headers=headers(handler)), 404, "NOT_FOUND")
    error(sup.post(path, json={"expected_version": 0}, headers=headers(sup)), 403, "FORBIDDEN")
    # Structure before object scope, but after role.
    error(handler.post(path, json={"expected_version": 0, "role": "HANDLER"}, headers=headers(handler)), 422, "VALIDATION_ERROR")
    error(sup.post(path, content=b"broken-json", headers=headers(sup)), 403, "FORBIDDEN")


def test_strict_unknown_queries_json_duplicate_fields_and_safe_framework_errors(http):
    db, run, ids, users, _, clients = http
    sup = clients["SUPERVISOR"]
    for params in ({"role": "ADMIN"}, {"page_size": 101}, {"page": -1}, {"page": "1.5"}, {"status": "bad"}, {"type": "bad"}):
        error(sup.get(f"/api/v1/runs/{run}/differences", params=params), 422, "VALIDATION_ERROR")
    error(sup.get(f"/api/v1/runs/{run}/differences?page=1&page=2"), 422, "VALIDATION_ERROR")
    error(sup.get("/api/v1/auth/me?password=not-a-real-secret"), 422, "VALIDATION_ERROR")
    path = "/api/v1/differences/" + ids[0] + "/assign"
    for value in (True, 0.1, "0", -1, 9223372036854775807):
        error(sup.post(path, json={"assignee_id": users["HANDLER"]["id"], "expected_version": value}, headers=headers(sup)), 422, "VALIDATION_ERROR")
    hdr = {**headers(sup), "Content-Type": "application/json"}
    error(sup.post(path, content='{"expected_version":0,"expected_version":1}', headers=hdr), 422, "VALIDATION_ERROR")
    error(sup.post(path, content='{"expected_version":NaN}', headers=hdr), 422, "VALIDATION_ERROR")
    error(sup.get(path), 405, "METHOD_NOT_ALLOWED")
    response = sup.post(path, json={"password": PASSWORD, "path": "D:/private"}, headers=headers(sup))
    error(response, 422, "VALIDATION_ERROR")
    assert PASSWORD not in response.text and "D:/private" not in response.text
    error(sup.post("/api/v1/auth/me"), 405, "METHOD_NOT_ALLOWED")


def test_login_rate_limit_unknown_and_inactive_credentials_generic(workdb):
    db, _, _, users = workdb
    app = create_app(db, app_origin=ORIGIN)
    app.state.auth_service.limiter = LoginLimiter(limit=3)
    with TestClient(app, base_url=ORIGIN) as client:
        for username in ("missing", "handler", "' OR 1=1--"):
            response = client.post("/api/v1/auth/login", json={"username": username, "password": "incorrect"}, headers={"Origin": ORIGIN})
            error(response, 401, "INVALID_CREDENTIALS")
        error(client.post("/api/v1/auth/login", json={"username": "handler", "password": PASSWORD}, headers={"Origin": ORIGIN}), 429, "LOGIN_RATE_LIMIT")
        error(client.post("/api/v1/auth/login", json={}, headers={"Origin": "null"}), 403, "ORIGIN_FAILED")


def test_db_busy_http503_and_every_connection_foreign_keys(workdb):
    db, _, _, _ = workdb
    with TestClient(create_app(db, app_origin=ORIGIN), base_url=ORIGIN) as client:
        login(client, "handler")
        connection = connect_database(db)
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
            connection.execute("BEGIN EXCLUSIVE")
            error(client.get("/api/v1/auth/me"), 503, "DATABASE_BUSY")
        finally:
            connection.rollback()
            connection.close()


def test_http_audit_failure_rollback_and_precheck_rejection_failure(http, monkeypatch):
    db, _, ids, users, _, clients = http
    sup = clients["SUPERVISOR"]
    from 主程序.repositories import audit_repository
    def failed(*args, **kwargs):
        raise InputError("AUDIT_WRITE_FAILED", 500, audit_persisted=False)
    monkeypatch.setattr(audit_repository, "append", failed)
    response = sup.post("/api/v1/differences/" + ids[0] + "/assign", json={"assignee_id": users["HANDLER"]["id"], "expected_version": 0}, headers=headers(sup))
    error(response, 500, "AUDIT_WRITE_FAILED")
    with database_transaction(db, readonly=True) as c:
        assert repository.get(c, ids[0])["version"] == 0
        assert c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    error(sup.post("/api/v1/auth/logout", headers={"Origin": "null"}), 500, "AUDIT_WRITE_FAILED")


def test_admin_audit_endpoint_secret_free_and_business_restricted(http):
    db, _, _, _, _, clients = http
    response = clients["ADMIN"].get("/api/v1/audit", params={"target_type": "user", "page_size": 2})
    assert response.status_code == 200 and response.json()["total"] >= 5 and len(response.json()["items"]) == 2
    assert PASSWORD not in response.text and "password_hash" not in response.text and "csrf_hash" not in response.text
    error(clients["FINANCE"].get("/api/v1/audit"), 403, "FORBIDDEN")


def test_revoked_after_http_precheck_cannot_write(http, monkeypatch):
    db, _, ids, users, app, clients = http
    sup = clients["SUPERVISOR"]
    original = app.state.auth_service.csrf
    def revoke_after_precheck(identity, token):
        original(identity, token)
        with database_transaction(db) as c:
            c.execute("UPDATE sessions SET revoked_at='2026-10-15T00:00:00+00:00' WHERE id=?", (identity.session_id,))
    monkeypatch.setattr(app.state.auth_service, "csrf", revoke_after_precheck)
    error(sup.post("/api/v1/differences/" + ids[0] + "/assign", json={"assignee_id": users["HANDLER"]["id"], "expected_version": 0}, headers=headers(sup)), 401, "SESSION_EXPIRED")
    with database_transaction(db, readonly=True) as c:
        assert repository.get(c, ids[0])["version"] == 0


def test_cli_admin_getpass_empty_v4_and_no_echo(tmp_path, monkeypatch, capsys):
    from 主程序 import cli
    from 主程序.repositories.database import initialize_database, upgrade_database
    db = tmp_path / "admin-init.sqlite3"
    initialize_database(db)
    upgrade_database(db, target_version=4)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(cli, "windows_console_available", lambda streams: True)
    assert cli.interactive_password_available()
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: PASSWORD)
    assert cli.main(["create-admin", "--database", str(db), "--username", "local-admin"]) == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and "password_hash" not in output.out
    assert json.loads(output.out)["user"]["role"] == "ADMIN"


@pytest.mark.parametrize("origin_value", ["null", "http://a/", "http://a:0", "http://a:", "http://a:65536", "http://user@a", "http://@a", "http://:@a", "http://a?x", " http://a", "http://a\n"])
def test_app_origin_strict_configuration(workdb, origin_value):
    with pytest.raises(ValueError):
        create_app(workdb[0], app_origin=origin_value)


def test_cli_admin_external_getpass_and_actual_origin_forwarded(workdb, monkeypatch):
    from 主程序 import cli
    db, _, _, _ = workdb
    observed = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: observed.update(app=app, **kwargs))
    assert cli.main(["serve", "--database", str(db), "--host", "127.0.0.1", "--port", "8787"]) == 0
    assert observed["app"].state.app_origin == "http://127.0.0.1:8787" and observed["access_log"] is False
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run([sys.executable, "-m", "主程序.cli", "create-admin", "--database", str(db), "--username", "admin2"], cwd=root, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
    assert process.returncode == 1 and b"INTERACTIVE_PASSWORD_REQUIRED" in process.stderr
    assert PASSWORD.encode() not in process.stdout + process.stderr


@pytest.mark.parametrize("redirect", ["nul", "pipe"])
def test_cli_admin_no_console_exits_before_getpass(tmp_path, redirect):
    root = Path(__file__).resolve().parents[2]
    db = tmp_path / "must-not-be-created.sqlite3"
    command = [sys.executable, "-m", "主程序.cli", "create-admin", "--database", str(db), "--username", "synthetic-admin"]
    if redirect == "nul":
        # On Windows both NUL handles claim isatty=True; neither is a console.
        process = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, timeout=8)
        assert process.stdout == b""
    else:
        process = subprocess.run(command, cwd=root, input=b"", capture_output=True, timeout=8)
        assert b"INTERACTIVE_PASSWORD_REQUIRED" in process.stderr
        assert b"New admin password" not in process.stderr
    assert process.returncode == 1 and not db.exists()


def test_windows_console_mode_uses_pointer_sized_handle(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows console API")
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace
    import msvcrt
    from 主程序 import cli

    observed = []
    class ConsoleMode:
        def __call__(self, handle, mode):
            observed.append(handle.value)
            return handle.value == 0x123456789
    check = ConsoleMode()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(GetConsoleMode=check))
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: 0x123456789 if fd == 0 else 7)
    stream = SimpleNamespace(fileno=lambda: 0)
    assert cli.windows_console_available((stream, stream))
    assert check.argtypes == (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    assert observed == [0x123456789, 0x123456789]
    assert not cli.windows_console_available((SimpleNamespace(fileno=lambda: 2),))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(cli, "windows_console_available", lambda streams: False)
    assert not cli.interactive_password_available()


@pytest.mark.parametrize("password,valid", [
    ("s" * 8, True), ("s" * 72, True), ("合成测试", True), ("测" * 24, True),
    ("s" * 7, False), ("s" * 73, False), ("测" * 25, False), ("synthetic\ud800", False),
])
def test_create_password_utf8_bytes_match_service_and_schema(password, valid):
    from pydantic import ValidationError
    from 主程序.services.auth_service import validate_credentials
    from 主程序.web.schemas import CreateUser
    if valid:
        validate_credentials("synthetic", password, "HANDLER")
        assert CreateUser(username="synthetic", password=password, role="HANDLER").password == password
    else:
        with pytest.raises(InputError, match="VALIDATION_ERROR"):
            validate_credentials("synthetic", password, "HANDLER")
        with pytest.raises(ValidationError):
            CreateUser(username="synthetic", password=password, role="HANDLER")


def test_http_utf8_password_and_surrogate_safe_validation(http):
    _, _, _, _, _, clients = http
    admin = clients["ADMIN"]
    password = "合成测试"  # Synthetic only: 4 characters, 12 UTF-8 bytes.
    payload = {"username": "synthetic-utf8", "password": password, "role": "HANDLER"}
    response = admin.post("/api/v1/users", json=payload, headers=headers(admin))
    assert response.status_code == 201 and password not in response.text
    response = clients["H2"].post("/api/v1/auth/login", json={"username": payload["username"], "password": password}, headers={"Origin": ORIGIN})
    assert response.status_code == 200 and password not in response.text
    for bad in ("s" * 7, "s" * 73, "测" * 25, "synthetic\ud800"):
        response = admin.post("/api/v1/users", content=json.dumps({**payload, "password": bad}),
                              headers={**headers(admin), "Content-Type": "application/json"})
        error(response, 422, "VALIDATION_ERROR")
        assert "synthetic\\ud800" not in response.text
    response = clients["H2"].post("/api/v1/auth/login", content=json.dumps({"username": "synthetic-utf8", "password": "synthetic\ud800"}),
                                  headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    error(response, 422, "VALIDATION_ERROR")


def legacy_error(response, status, code):
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload['error']['code'] == code, payload
    return payload


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_schema_new_apis_absent_no_audit_or_migration(tmp_path, version):
    from 主程序.repositories.database import initialize_database, upgrade_database
    db = tmp_path / "legacy.sqlite3"
    initialize_database(db)
    if version > 1:
        upgrade_database(db, target_version=version)
    before = db.read_bytes()
    with TestClient(create_app(db, app_origin=ORIGIN), base_url=ORIGIN) as client:
        assert client.get("/health").json()["schema_version"] == version
        assert client.get("/evidence/counts").status_code == 200
        assert client.get("/evidence/run").status_code == 200
        for method, path in (("POST", "/auth/login"), ("GET", "/auth/me"), ("POST", "/auth/logout"),
                             ("POST", "/users"), ("GET", "/audit"), ("POST", "/imports"),
                             ("POST", "/pipeline"), ("POST", "/runs")):
            response = client.request(method, "/api/v1" + path, content=b"malformed")
            error(response, 404, "NOT_FOUND")
            assert "AUDIT_WRITE_FAILED" not in response.text
    assert db.read_bytes() == before


def test_v4_legacy_evidence_routes_enforce_scope(workdb):
    db, run_id, _, users = workdb
    with database_transaction(db, readonly=True) as c:
        batch_id = c.execute('SELECT batch_id FROM runs WHERE id=?', (run_id,)).fetchone()[0]
    with TestClient(create_app(db, app_origin=ORIGIN), base_url=ORIGIN) as client:
        legacy_error(client.get('/evidence/run/' + run_id), 401, 'SESSION_EXPIRED')
        login(client, 'admin')
        legacy_error(client.get('/evidence/run/' + run_id), 403, 'FORBIDDEN')
        legacy_error(client.get('/evidence/counts/' + batch_id), 403, 'FORBIDDEN')
        login(client, 'handler')
        legacy_error(client.get('/evidence/run/' + run_id), 404, 'NOT_FOUND')
        legacy_error(client.get('/evidence/counts/' + batch_id), 403, 'FORBIDDEN')
        login(client, 'finance')
        assert client.get('/evidence/run/' + run_id).status_code == 200
        assert client.get('/evidence/counts/' + batch_id).status_code == 200
        login(client, 'supervisor')
        assert client.get('/evidence/counts/' + batch_id).status_code == 200
