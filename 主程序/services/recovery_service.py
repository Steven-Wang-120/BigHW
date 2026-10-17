"""Explicit local-only interruption recovery."""

from pathlib import Path

from 主程序.repositories.database import database_transaction, DatabaseError
from 主程序.repositories.process_lock import ProcessLockError, database_process_lock
from 主程序.repositories import recovery_repository
from 主程序.repositories.xlsx_reader import InputError


def recover_jobs(database: Path, *, request_id: str):
    try:
        with database_process_lock(database, "exclusive"):
            try:
                with database_transaction(database) as connection:
                    return recovery_repository.recover(connection, request_id)
            except InputError:
                raise
            except DatabaseError:
                raise
            except Exception as exc:
                if isinstance(exc, InputError):
                    raise
                raise InputError("AUDIT_WRITE_FAILED", 500, audit_persisted=False) from exc
    except ProcessLockError as exc:
        raise InputError("RECOVERY_LOCK_BUSY", 409) from exc
