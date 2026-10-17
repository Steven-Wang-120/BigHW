"""Additional recovery atomicity, partial table and Windows ABI checks."""
import os
import pytest
from 测试.integration.test_reconcile import imported
from 主程序.repositories.database import database_transaction, upgrade_database
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.reconcile_service import reconcile
from 主程序.services.recovery_service import recover_jobs


@pytest.mark.parametrize('table', ['sale_lots', 'sale_lot_rows', 'allocations', 'differences'])
def test_each_partial_run_fact_table_refuses_all_recovery(imported, table):
    db, batch = imported
    upgrade_database(db, target_version=4)
    run = reconcile(db, batch)['run_id']
    with database_transaction(db) as c:
        # Retain real FK parents where required; never disable FK/domain guards.
        if table != 'allocations':
            c.execute('DELETE FROM allocations')
        if table != 'differences':
            c.execute('DELETE FROM differences')
        if table != 'sale_lot_rows':
            c.execute('DELETE FROM sale_lot_rows')
        if table == 'differences':
            c.execute('DELETE FROM sale_lots')
        c.execute("UPDATE runs SET status='RUNNING',summary_json=NULL,finished_at=NULL WHERE id=?", (run,))
        count = c.execute(f'SELECT COUNT(*) FROM {table} WHERE run_id=?', (run,)).fetchone()[0]
        assert count > 0
        audit_before = c.execute('SELECT * FROM audit_events ORDER BY id').fetchall()
    before = db.read_bytes()
    with pytest.raises(InputError, match='RECOVERY_PARTIAL_FACTS') as failure:
        recover_jobs(db, request_id='partial-' + table)
    diagnostic = failure.value.details['diagnostics']
    assert diagnostic[0]['target_id'] == run and diagnostic[0]['facts'][table] == count
    with database_transaction(db, readonly=True) as c:
        assert c.execute('SELECT * FROM audit_events ORDER BY id').fetchall() == audit_before
        assert c.execute('SELECT status,attempt,error_code FROM runs WHERE id=?', (run,)).fetchone() == ('RUNNING', 1, None)
        assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE run_id=?', (run,)).fetchone()[0] == count
    assert db.read_bytes() == before


def test_multijob_audit_failure_rolls_back_every_job_then_idempotent(imported, monkeypatch):
    from 主程序.repositories import recovery_repository, reconcile_repository
    db, batch = imported
    upgrade_database(db, target_version=4)
    with database_transaction(db) as c:
        snapshot = c.execute('SELECT snapshot_id FROM import_batches WHERE id=?', (batch,)).fetchone()[0]
        for index in range(2):
            c.execute('INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)',
                      (f'pending-{index}', snapshot, f'synthetic-recovery-{index}', 'IMPORTING', index + 1,
                       None, None, '2026-10-17T00:00:00+00:00', None))
        run, _, _ = reconcile_repository.start_run(c, batch)
        before = {table: c.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                  for table in ('import_batches', 'runs', 'audit_events')}
    append = recovery_repository.append
    calls = []
    def fail_third(*args, **kwargs):
        calls.append(args[4])
        if len(calls) == 3:
            raise RuntimeError('synthetic audit persistence failure')
        return append(*args, **kwargs)
    monkeypatch.setattr(recovery_repository, 'append', fail_third)
    with pytest.raises(InputError, match='AUDIT_WRITE_FAILED'):
        recover_jobs(db, request_id='rollback-three')
    assert len(calls) == 3
    with database_transaction(db, readonly=True) as c:
        assert {table: c.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in before} == before
    monkeypatch.setattr(recovery_repository, 'append', append)
    recovered = recover_jobs(db, request_id='retry-three')
    assert len(recovered['imports']) == 2 and [r['run_id'] for r in recovered['runs']] == [run]
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE request_id='rollback-three'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE request_id='retry-three' AND actor_id IS NULL AND outcome='FAILED' AND error_code='INTERRUPTED_PROCESS'").fetchone()[0] == 3
        after = {table: c.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in before}
    repeated = recover_jobs(db, request_id='idempotent')
    assert repeated['imports'] == repeated['runs'] == []
    with database_transaction(db, readonly=True) as c:
        assert {table: c.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in before} == after


@pytest.mark.skipif(os.name != 'nt', reason='Windows handle release contract')
@pytest.mark.parametrize('failure', ['false', 'exception'])
def test_windows_release_always_closes_handle_on_unlock_failure(monkeypatch, failure):
    from 主程序.repositories import process_lock
    calls = []
    class Kernel:
        def UnlockFileEx(self, *args):
            calls.append('unlock')
            if failure == 'exception':
                raise OSError('injected unlock error')
            return False
        def CloseHandle(self, handle):
            calls.append(('close', handle))
            return True
    monkeypatch.setattr(process_lock, '_kernel32', Kernel())
    with pytest.raises(OSError):
        process_lock._release(0x100000001, process_lock.OVERLAPPED())
    assert calls == ['unlock', ('close', 0x100000001)]


@pytest.mark.skipif(os.name != 'nt', reason='Windows ABI declarations')
def test_windows_handle_and_overlapped_are_pointer_sized():
    import ctypes
    from ctypes import wintypes
    from 主程序.repositories import process_lock
    assert ctypes.sizeof(wintypes.HANDLE) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(process_lock.OVERLAPPED) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)
    assert process_lock._kernel32.CreateFileW.restype == wintypes.HANDLE
    assert process_lock._kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)
