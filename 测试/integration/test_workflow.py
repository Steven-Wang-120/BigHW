"""F-only v4 data, migrations, work rules, audit injection and real connection races."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

import pytest

from 测试.integration.test_reconcile import imported
from 主程序.domain.permissions import Actor, allowed, in_scope
from 主程序.domain.states import WorkState, RuleError, transition
from 主程序.repositories.database import database_transaction, database_health, initialize_database, upgrade_database, DatabaseError
from 主程序.repositories import workflow_repository as repository
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.auth_service import create_user, set_active
from 主程序.services.import_service import counts
from 主程序.services.reconcile_service import reconcile
from 主程序.services.workflow_service import change, detail, differences

PASSWORD = "Synthetic-Only-2026!"


@pytest.fixture
def workdb(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    run = reconcile(db, batch)["run_id"]
    upgrade_database(db, target_version=4)
    admin = create_user(db, None, "admin", PASSWORD, "ADMIN", "fixture-admin", bootstrap=True)
    actor = Actor(admin["id"], "ADMIN")
    users = {"ADMIN": admin}
    for role in ("FINANCE", "SUPERVISOR", "HANDLER"):
        users[role] = create_user(db, actor, role.lower(), PASSWORD, role, "fixture-" + role)
    users["H2"] = create_user(db, actor, "handler2", PASSWORD, "HANDLER", "fixture-H2")
    with database_transaction(db, readonly=True) as c:
        ids = [row[0] for row in c.execute("SELECT id FROM differences ORDER BY id")]
    return db, run, ids, users


def actors(users, role):
    user = users[role]
    return Actor(user["id"], user["role"])


def test_tc36_pure_state_and_error_priority():
    state = WorkState()
    with pytest.raises(RuleError, match="NOT_ASSIGNED"):
        transition(state, "start", 0)
    assigned = transition(state, "assign", 0, assignee_id="H")
    assert state == WorkState() and assigned == WorkState("PENDING", 1, "H")
    with pytest.raises(RuleError, match="ALREADY_ASSIGNED") as exc:
        transition(assigned, "assign", 999, assignee_id="H")
    assert exc.value.details == {"current_assignee": "H"}
    with pytest.raises(RuleError, match="VERSION_CONFLICT"):
        transition(assigned, "close", 0, resolution_note="note")
    with pytest.raises(RuleError, match="INVALID_TRANSITION"):
        transition(assigned, "close", 1, resolution_note="note")
    running = transition(assigned, "start", 1)
    for note in ("", " \t\n ", " "):
        with pytest.raises(RuleError, match="EMPTY_RESOLUTION"):
            transition(running, "close", 2, resolution_note=note)
    closed = transition(running, "close", 2, resolution_note="  checked  ")
    assert closed == WorkState("CLOSED", 3, "H", "checked")
    for event in ("start", "close", "reopen", "back"):
        with pytest.raises(RuleError, match="INVALID_TRANSITION"):
            transition(closed, event, 3, resolution_note="another")


@pytest.mark.parametrize("role", ["FINANCE", "SUPERVISOR", "HANDLER", "ADMIN"])
def test_permission_matrix(role):
    actor = Actor("H", role)
    assert allowed(actor, "read") == (role != "ADMIN")
    assert allowed(actor, "assign") == (role == "SUPERVISOR")
    assert allowed(actor, "start") == (role == "HANDLER")
    assert allowed(actor, "close") == (role == "HANDLER")
    assert allowed(actor, "users") == (role == "ADMIN")
    assert in_scope(actor, "H2") == (role in {"FINANCE", "SUPERVISOR"})


def test_v4_migration_preserves_all_facts_and_v4_import_reconcile(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    result = reconcile(db, batch)
    with database_transaction(db, readonly=True) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name!='schema_metadata'")]
        before = {name: c.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall() for name in tables}
        old_objects = dict(c.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
    assert upgrade_database(db, target_version=4)
    assert database_health(db) == {"schema_version": 4, "foreign_keys": True, "scope": "business-workflow"}
    with database_transaction(db, readonly=True) as c:
        for name, rows in before.items():
            actual = c.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall()
            assert [r[:13] for r in actual] == rows if name == "differences" else actual == rows
        objects = dict(c.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
        assert all(objects[name] == ddl for name, ddl in old_objects.items() if name != "differences")
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    unchanged = db.read_bytes()
    assert upgrade_database(db, target_version=4) is False
    assert upgrade_database(db) is False and db.read_bytes() == unchanged
    assert reconcile(db, batch)["run_id"] == result["run_id"]
    assert counts(db, batch)["selected_count"] == 5
    # Real import_snapshot path on a new v4 tmp database, then first publication.
    from 主程序.services.import_service import import_snapshot
    from 主程序.services.snapshot_service import sha256
    fresh = db.parent / "fresh-v4.sqlite3"
    initialize_database(fresh)
    upgrade_database(fresh, target_version=4)
    with database_transaction(db, readonly=True) as c:
        manifest_digest = c.execute("SELECT manifest_digest FROM snapshots").fetchone()[0]
    new_batch = import_snapshot(fresh, db.parent / "snapshot", db.parent / "source.xlsx", manifest_digest,
                                expected_sha=sha256(db.parent / "source.xlsx"))["batch_id"]
    new = reconcile(fresh, new_batch)
    assert new["difference_count"] == 2 and new["matched"] == 2


@pytest.mark.parametrize("version", [1, 2, 3])
def test_v4_explicit_from_each_version_and_tampered_unchanged(tmp_path, version):
    db = tmp_path / "fresh.sqlite3"
    initialize_database(db)
    if version > 1:
        upgrade_database(db, target_version=version)
    assert upgrade_database(db, target_version=4)
    assert database_health(db)["schema_version"] == 4
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE foreign_object (x TEXT)")
    before = db.read_bytes()
    with pytest.raises(DatabaseError, match="SCHEMA_UNRECOGNIZED"):
        upgrade_database(db, target_version=4)
    assert db.read_bytes() == before


def test_v3_tampered_migration_rejected_without_byte_change(imported):
    db, _ = imported
    upgrade_database(db, target_version=3)
    with sqlite3.connect(db) as c:
        c.execute("DROP TRIGGER differences_source_update")
    before = db.read_bytes()
    with pytest.raises(DatabaseError, match="SCHEMA_UNRECOGNIZED"):
        upgrade_database(db, target_version=4)
    assert db.read_bytes() == before


def test_tc33_34_37_38_39_40_service_state_and_matching_unchanged(workdb):
    db, run, ids, users = workdb
    sup, handler = actors(users, "SUPERVISOR"), actors(users, "HANDLER")
    did = ids[0]
    with database_transaction(db, readonly=True) as c:
        before = {table: c.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in ("import_rows", "allocations", "sale_lots", "runs")}
    assigned = change(db, sup, did, "assign", 0, "tc33", assignee_id=handler.id)
    assert assigned["work_status"] == "PENDING" and assigned["version"] == 1
    for target in (handler.id, users["H2"]["id"]):
        with pytest.raises(InputError, match="ALREADY_ASSIGNED") as exc:
            change(db, sup, did, "assign", 0, "tc34", assignee_id=target)
        assert exc.value.status == 409 and exc.value.details["current_assignee"] == handler.id
    with pytest.raises(InputError, match="INVALID_TRANSITION"):
        change(db, handler, did, "close", 1, "tc38", resolution_note="checked")
    assert change(db, handler, did, "start", 1, "tc37")["version"] == 2
    with pytest.raises(InputError, match="EMPTY_RESOLUTION"):
        change(db, handler, did, "close", 2, "tc39", resolution_note=" \t ")
    closed = change(db, handler, did, "close", 2, "tc40", resolution_note=" checked ")
    assert (closed["work_status"], closed["version"], closed["resolution_note"], closed["match_status"]) == ("CLOSED", 3, "checked", "UNMATCHED")
    for event in ("close", "start"):
        with pytest.raises(InputError, match="INVALID_TRANSITION"):
            change(db, handler, did, event, 3, "tc40-repeat", resolution_note="again")
    with database_transaction(db, readonly=True) as c:
        assert before == {table: c.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() for table in before}
        assert c.execute("SELECT COUNT(*) FROM assignments WHERE difference_id=?", (did,)).fetchone()[0] == 1
        audits = c.execute("SELECT actor_id,request_id,action,summary_json FROM audit_events WHERE target_id=? AND outcome='SUCCESS' ORDER BY created_at", (did,)).fetchall()
        assert [a[2] for a in audits] == ["ASSIGN", "START", "CLOSE"]
        assert [a[1] for a in audits] == ["tc33", "tc37", "tc40"]
        assert '"before"' in audits[0][3] and '"after"' in audits[0][3]


@pytest.mark.parametrize("invalid", ["missing", "inactive", "finance"])
def test_invalid_assignee_no_write(workdb, invalid):
    db, _, ids, users = workdb
    target = "missing" if invalid == "missing" else users["HANDLER" if invalid == "inactive" else "FINANCE"]["id"]
    if invalid == "inactive":
        set_active(db, actors(users, "ADMIN"), target, False, 0, "disable")
    with pytest.raises(InputError, match="INVALID_ASSIGNEE"):
        change(db, actors(users, "SUPERVISOR"), ids[0], "assign", 0, "invalid", assignee_id=target)
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
        assert repository.get(c, ids[0])["version"] == 0


def test_tc43_success_audit_failure_rolls_back_and_rejection_failure_explicit(workdb):
    db, _, ids, users = workdb
    did, sup, handler = ids[0], actors(users, "SUPERVISOR"), actors(users, "HANDLER")
    def fail_audit(c):
        c.execute("CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises(InputError, match="AUDIT_WRITE_FAILED") as exc:
        change(db, sup, did, "assign", 0, "tc43-success", assignee_id=handler.id, fault=fail_audit)
    assert exc.value.status == 500 and exc.value.details["audit_persisted"] is False
    with database_transaction(db, readonly=True) as c:
        assert repository.get(c, did)["version"] == 0
        assert c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    change(db, sup, did, "assign", 0, "assigned", assignee_id=handler.id)
    for event, version in (("start", 1), ("close", 2)):
        with pytest.raises(InputError, match="AUDIT_WRITE_FAILED"):
            change(db, handler, did, event, version, "tc43-" + event, resolution_note="checked", fault=fail_audit)
        with database_transaction(db, readonly=True) as c:
            assert repository.get(c, did)["version"] == version
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE request_id=?", ("tc43-" + event,)).fetchone()[0] == 0
        if event == "start":
            change(db, handler, did, "start", 1, "started")
    with pytest.raises(InputError, match="AUDIT_WRITE_FAILED") as exc:
        change(db, sup, did, "assign", 1, "tc43-reject", assignee_id=handler.id, rejection_fault=fail_audit)
    assert exc.value.details == {"audit_persisted": False, "rejected_code": "ALREADY_ASSIGNED"}
    with database_transaction(db, readonly=True) as c:
        assert repository.get(c, did)["version"] == 2
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE request_id='tc43-reject'").fetchone()[0] == 0


def test_v4_migration_fault_is_atomic_on_populated_v3(imported, monkeypatch):
    db, batch = imported
    upgrade_database(db, target_version=3)
    reconcile(db, batch)
    before = db.read_bytes()
    from 主程序.repositories import workflow_schema
    original = workflow_schema.migrate_v4
    def fail(c):
        original(c)
        raise RuntimeError("synthetic migration failure")
    monkeypatch.setattr(workflow_schema, "migrate_v4", fail)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        upgrade_database(db, target_version=4)
    assert db.read_bytes() == before
    assert database_health(db)["schema_version"] == 3


def test_v4_db_guards_sync_unique_fk_and_note(workdb):
    db, _, ids, users = workdb
    sup, handler = actors(users, "SUPERVISOR"), actors(users, "HANDLER")
    with pytest.raises(DatabaseError):
        with database_transaction(db) as c:
            c.execute("UPDATE differences SET assignee_id=? WHERE id=?", (handler.id, ids[0]))
    with pytest.raises(DatabaseError):
        with database_transaction(db) as c:
            repository.assign(c, ids[0], sup.id, handler.id)
    change(db, sup, ids[0], "assign", 0, "guard-assign", assignee_id=handler.id)
    with database_transaction(db) as c:
        for sql, params in (("DELETE FROM assignments WHERE difference_id=?", (ids[0],)),
                            ("UPDATE differences SET work_status='CLOSED',resolution_note=' \t ' WHERE id=?", (ids[0],)),
                            ("UPDATE differences SET version=1.5 WHERE id=?", (ids[0],))):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql, params)
    with pytest.raises(DatabaseError):
        with database_transaction(db) as c:
            c.execute("UPDATE differences SET assignee_id=NULL WHERE id=?", (ids[0],))


@pytest.mark.parametrize("event", ["assign", "close"])
def test_tc35_tc42_true_independent_connection_barrier_ten_rounds(workdb, event, monkeypatch):
    db, run, ids, users = workdb
    sup, handler = actors(users, "SUPERVISOR"), actors(users, "HANDLER")
    # Reuse the synthetic run with ten new source rows/differences; no reset of
    # successful business state, each race gets a new object.
    with database_transaction(db) as c:
        source = list(c.execute("SELECT * FROM import_rows WHERE id=(SELECT cancel_id FROM differences WHERE id=?)", (ids[0],)).fetchone())
        difference = list(c.execute("SELECT * FROM differences WHERE id=?", (ids[0],)).fetchone())
        for i in range(10):
            row = source.copy()
            row[0], row[4] = f"race-source-{i}", 100 + i
            c.execute("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in row) + ")", row)
            d = difference.copy()
            d[0], d[3] = f"race-difference-{i}", row[0]
            c.execute("INSERT INTO differences VALUES (" + ",".join("?" for _ in d) + ")", d)
    from 主程序.repositories import database as database_module
    from 主程序.services import workflow_service
    original_connect = database_module.connect_database
    seen = []
    def tracked(*args, **kwargs):
        c = original_connect(*args, **kwargs)
        seen.append(c)
        return c
    monkeypatch.setattr(database_module, "connect_database", tracked)
    outcomes = []
    for i in range(10):
        did = f"race-difference-{i}"
        if event == "close":
            change(db, sup, did, "assign", 0, f"prep-{i}", assignee_id=handler.id)
            change(db, handler, did, "start", 1, f"prep-start-{i}")
        barrier = Barrier(2)
        actor = sup if event == "assign" else handler
        def worker(n):
            # This barrier runs after opening each independent connection but
            # before BEGIN IMMEDIATE: true overlapping writer acquisition.
            from contextlib import contextmanager
            @contextmanager
            def synchronized_transaction(path, *, readonly=False):
                c = tracked(path, readonly=readonly)
                try:
                    if not readonly:
                        barrier.wait(timeout=10)
                    c.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                    database_module.validate_schema(c)
                    yield c
                    c.commit()
                except sqlite3.Error as exc:
                    c.rollback()
                    raise database_module._translate(exc) from exc
                except BaseException:
                    c.rollback()
                    raise
                finally:
                    c.close()
            # Only the operation transaction synchronizes; rejection uses the
            # real independent database_transaction, never an extra barrier.
            return synchronized_transaction
        from contextlib import contextmanager
        local_transactions = {n: worker(n) for n in (0, 1)}
        # Thread-local distinction avoids monkeypatch swapping while live.
        from threading import local
        thread_state = local()
        @contextmanager
        def concurrent_transaction(path, *, readonly=False):
            count = getattr(thread_state, "count", 0)
            thread_state.count = count + 1
            tx = local_transactions[thread_state.number] if count == 0 else database_module.database_transaction
            with tx(path, readonly=readonly) as c:
                yield c
        monkeypatch.setattr(workflow_service, "database_transaction", concurrent_transaction)
        def request(n):
            thread_state.number, thread_state.count = n, 0
            kw = {"assignee_id": users["HANDLER" if n == 0 else "H2"]["id"]} if event == "assign" else {"resolution_note": f"winner-{n}"}
            try:
                payload = change(db, actor, did, event, 0 if event == "assign" else 2, f"race-{event}-{i}-{n}", **kw)
                return 200, payload
            except InputError as exc:
                return exc.status, exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, (0, 1)))
        monkeypatch.setattr(workflow_service, "database_transaction", database_module.database_transaction)
        expected = "ALREADY_ASSIGNED" if event == "assign" else "VERSION_CONFLICT"
        assert sorted(r[0] for r in results) == [200, 409], results
        assert [r[1] for r in results if r[0] == 409] == [expected]
        with database_transaction(db, readonly=True) as c:
            row = repository.get(c, did)
            assert row["version"] == (1 if event == "assign" else 3)
            assert c.execute("SELECT COUNT(*) FROM assignments WHERE difference_id=?", (did,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM audit_events WHERE target_id=? AND action=? AND outcome='SUCCESS'", (did, event.upper())).fetchone()[0] == 1
            if event == "close":
                assert row["resolution_note"] == next(r[1]["resolution_note"] for r in results if r[0] == 200)
        outcomes.append([r[0] if r[0] == 200 else r[1] for r in results])
    assert len({id(c) for c in seen}) == len(seen)
    print(f"TC race {event}: independent connections={len(seen)}, rounds=10, outcomes={outcomes}")
