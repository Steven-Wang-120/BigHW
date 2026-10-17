"""Pure-synthetic cross-process lifecycle lock and schema-4 recovery checks."""

import subprocess
from pathlib import Path
import sys
import time
import queue
import threading
import os

import pytest

from 测试.integration.test_reconcile import imported
from 主程序.repositories.database import database_transaction, initialize_database, upgrade_database
from 主程序.repositories.process_lock import ProcessLockError, database_process_lock, lock_path
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.import_service import import_snapshot
from 主程序.services.reconcile_service import reconcile
from 主程序.services.recovery_service import recover_jobs
from 主程序.services.snapshot_service import sha256

ROOT = Path(__file__).resolve().parents[2]


def _worker(db, mode):
    code = """import sys
from 主程序.repositories.process_lock import database_process_lock
with database_process_lock(sys.argv[1], sys.argv[2]):
    print('READY', flush=True)
    sys.stdin.buffer.read(1)
"""
    return subprocess.Popen([sys.executable, "-c", code, str(db), mode], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")


def _ready(process, timeout=8):
    # readline in the controlling thread can block forever despite a deadline.
    result = queue.Queue()
    reader = threading.Thread(target=lambda: result.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise AssertionError('worker readiness timeout') from exc
    assert line.strip() == 'READY', f'worker exited before READY (exit={process.poll()})'
    reader.join(timeout=1)


def _stop(process, *, graceful=False):
    if process.poll() is None:
        if graceful:
            process.stdin.write('x')
            process.stdin.flush()
        else:
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError('owned worker failed to exit within deadline')
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()
    if graceful:
        assert process.returncode == 0


def _schema4(db):
    initialize_database(db)
    upgrade_database(db, target_version=4)


def _copy_import_source(source_db, db):
    with database_transaction(source_db, readonly=True) as source:
        source_files = source.execute("SELECT * FROM source_files").fetchall()
        snapshots = source.execute("SELECT * FROM snapshots").fetchall()
        batch = source.execute("SELECT * FROM import_batches").fetchone()
        rows = source.execute("SELECT * FROM import_rows").fetchall()
    with database_transaction(db) as target:
        target.executemany("INSERT INTO source_files VALUES (?,?,?,?,?,?,?)", source_files)
        target.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", snapshots)
    return batch, rows


def test_two_shared_processes_coexist_and_exclusive_recovery_is_bounded(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    _schema4(db)
    first = second = None
    try:
        first, second = _worker(db, "shared"), _worker(db, "shared")
        _ready(first)
        _ready(second)
        with pytest.raises(InputError, match="RECOVERY_LOCK_BUSY"):
            recover_jobs(db, request_id="recovery-live")
        assert lock_path(db).exists()
    finally:
        for process in (first, second):
            if process is not None:
                _stop(process, graceful=True)
    with database_process_lock(db, "exclusive"):
        pass


def test_killed_shared_holder_releases_lock_without_deleting_sidecar(tmp_path):
    db = tmp_path / "killed.sqlite3"
    _schema4(db)
    process = _worker(db, "shared")
    try:
        _ready(process)
        sidecar = lock_path(db)
        assert sidecar.exists()
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                with database_process_lock(db, "exclusive"):
                    break
            except ProcessLockError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert sidecar.exists()
    finally:
        _stop(process)


def test_recovery_without_lock_owner_marks_clean_active_jobs_and_retries(imported, tmp_path):
    source_db, _ = imported
    source = next(source_db.parent.glob("source.xlsx"))
    snapshot = source_db.parent / "snapshot"
    db = tmp_path / "recovery.sqlite3"
    _schema4(db)
    with database_transaction(source_db, readonly=True) as source_connection:
        source_batch = source_connection.execute("SELECT snapshot_id,normalization_version FROM import_batches").fetchone()
        snapshot_id = source_batch[0]
        manifest_digest = source_connection.execute("SELECT manifest_digest FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()[0]
    with database_transaction(source_db, readonly=True) as source_connection:
        source_files = source_connection.execute("SELECT * FROM source_files").fetchall()
        snapshots = source_connection.execute("SELECT * FROM snapshots").fetchall()
    with database_transaction(db) as c:
        c.executemany("INSERT INTO source_files VALUES (?,?,?,?,?,?,?)", source_files)
        c.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", snapshots)
        c.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)",
                  ("interrupted-batch", snapshot_id, source_batch[1], "IMPORTING", 1, None, None, "2026-10-17T00:00:00+00:00", None))
    result = recover_jobs(db, request_id="recover-clean")
    assert result["imports"][0]["batch_id"] == "interrupted-batch"
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status,attempt,error_code FROM import_batches WHERE id='interrupted-batch'").fetchone() == ("FAILED", 1, "INTERRUPTED_PROCESS")
        audit = c.execute("SELECT actor_id,request_id,action,outcome,error_code FROM audit_events WHERE target_id='interrupted-batch'").fetchone()
        assert audit == (None, "recover-clean", "RECOVER_IMPORT", "FAILED", "INTERRUPTED_PROCESS")
    retried = import_snapshot(db, snapshot, source, manifest_digest, expected_sha=sha256(source))
    assert retried["attempt"] == 2 and retried["status"] == "SUCCESS"


def test_recovery_run_reuses_run_and_increments_attempt_after_retry(imported, tmp_path):
    source_db, source_batch_id = imported
    db = tmp_path / "run-recovery.sqlite3"
    _schema4(db)
    batch, rows = _copy_import_source(source_db, db)
    with database_transaction(db) as c:
        c.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)", batch)
        c.executemany("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in rows[0]) + ")", rows)
        from 主程序.repositories import reconcile_repository
        run_id, attempt, reused = reconcile_repository.start_run(c, source_batch_id)
        assert attempt == 1 and not reused
    result = recover_jobs(db, request_id="recover-run")
    assert result["runs"][0]["run_id"] == run_id
    retried = reconcile(db, source_batch_id)
    assert retried["run_id"] == run_id and retried["attempt"] == 2 and retried["status"] == "COMPLETED"


def test_partial_active_facts_are_refused_without_audit_or_mutation(imported, tmp_path):
    source_db, _ = imported
    db = tmp_path / "partial.sqlite3"
    _schema4(db)
    with database_transaction(source_db, readonly=True) as source:
        source_files = source.execute("SELECT * FROM source_files").fetchall()
        snapshots = source.execute("SELECT * FROM snapshots").fetchall()
        row = list(source.execute("SELECT * FROM import_rows LIMIT 1").fetchone())
        snapshot_id, normalization = source.execute("SELECT snapshot_id,normalization_version FROM import_batches LIMIT 1").fetchone()
    row[0], row[1] = "partial-row", "partial-import"
    with database_transaction(db) as c:
        c.executemany("INSERT INTO source_files VALUES (?,?,?,?,?,?,?)", source_files)
        c.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", snapshots)
        c.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)",
                  ("partial-import", snapshot_id, normalization, "IMPORTING", 1, None, None, "2026-10-17T00:00:00+00:00", None))
        c.execute("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in row) + ")", row)
    before = db.read_bytes()
    with pytest.raises(InputError, match="RECOVERY_PARTIAL_FACTS"):
        recover_jobs(db, request_id="partial")
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status,attempt FROM import_batches WHERE id='partial-import'").fetchone() == ("IMPORTING", 1)
        assert c.execute("SELECT COUNT(*) FROM import_rows WHERE batch_id='partial-import'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert db.read_bytes() == before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_recovery_requires_explicit_schema4_without_mutation(tmp_path, version):
    db = tmp_path / f"v{version}.sqlite3"
    initialize_database(db)
    if version > 1:
        upgrade_database(db, target_version=version)
    before = db.read_bytes()
    with pytest.raises(InputError, match="RECOVERY_SCHEMA4_REQUIRED"):
        recover_jobs(db, request_id="schema")
    assert db.read_bytes() == before
