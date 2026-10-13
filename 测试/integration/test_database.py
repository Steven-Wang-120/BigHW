from pathlib import Path
import sqlite3
import time

import pytest

from 主程序.repositories.database import (
    APPLICATION_ID, BUSY_TIMEOUT_MS, DatabaseError, connect_database,
    database_health, database_transaction, initialize_database,
)


def test_bootstrap_missing_database_is_never_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    for operation in [connect_database, database_health]:
        with pytest.raises(DatabaseError, match="DATABASE_NOT_INITIALIZED"):
            operation(path)
        assert not path.exists()


def test_bootstrap_initialization_is_idempotent_and_metadata_only(tmp_path):
    path = tmp_path / "bootstrap.sqlite3"
    assert initialize_database(path) is True
    before = path.read_bytes()
    assert initialize_database(path) is False
    assert path.read_bytes() == before
    assert database_health(path) == {"schema_version": 1, "foreign_keys": True, "scope": "bootstrap-only"}
    with database_transaction(path, readonly=True) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == [("schema_metadata",)]


@pytest.mark.parametrize("kind", ["empty-existing", "business-table", "higher-version", "corrupt", "foreign-app"])
def test_bootstrap_rejects_existing_unknown_or_future_database_without_changes(kind, tmp_path):
    path = tmp_path / "existing.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"not a SQLite database")
    else:
        connection = sqlite3.connect(path)
        if kind == "business-table":
            connection.execute("CREATE TABLE business_data (value TEXT)")
            connection.execute("INSERT INTO business_data VALUES ('synthetic')")
        elif kind == "higher-version":
            connection.execute("PRAGMA user_version = 2")
        elif kind == "foreign-app":
            connection.execute("PRAGMA application_id = 123")
        connection.commit()
        connection.close()
    before = path.read_bytes()
    with pytest.raises(DatabaseError):
        initialize_database(path)
    assert path.read_bytes() == before
    with pytest.raises(DatabaseError):
        database_health(path)
    assert path.read_bytes() == before


def test_bootstrap_unknown_table_cannot_hide_behind_like_wildcard(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    connection = connect_database(path)
    connection.execute("CREATE TABLE sqliteXbusiness (id INTEGER)")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(DatabaseError, match="SCHEMA_UNRECOGNIZED"):
        database_health(path)
    assert before == path.read_bytes()


def test_bootstrap_metadata_tampering_is_rejected(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    connection = connect_database(path)
    connection.execute("UPDATE schema_metadata SET value='unexpected' WHERE key='scope'")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(DatabaseError, match="SCHEMA_UNRECOGNIZED"):
        database_health(path)
    assert before == path.read_bytes()


def test_bootstrap_transaction_rolls_back_on_failure(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with database_transaction(path) as connection:
            connection.execute("INSERT INTO schema_metadata VALUES ('test', 'synthetic')")
            raise RuntimeError("synthetic failure")
    with database_transaction(path, readonly=True) as connection:
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='test'").fetchone() is None


def test_connection_factory_enforces_fk_on_every_connection_and_finite_timeout(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    for _ in range(2):
        connection = connect_database(path)
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
            connection.execute("CREATE TEMP TABLE parent (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE child (parent_id INTEGER REFERENCES parent(id))")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO child VALUES (999)")
        finally:
            connection.close()


def test_connection_factory_bounded_busy_timeout_under_real_two_connection_lock(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    first = connect_database(path)
    second = connect_database(path, busy_timeout_ms=50)
    try:
        first.execute("BEGIN IMMEDIATE")
        clock = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError):
            second.execute("BEGIN IMMEDIATE")
        assert time.perf_counter() - clock < 2
    finally:
        first.rollback()
        first.close()
        second.close()


@pytest.mark.parametrize("timeout", [0, 5001, True, 1.5])
def test_connection_factory_rejects_unbounded_timeout(timeout, tmp_path):
    with pytest.raises(ValueError):
        connect_database(tmp_path / "missing.sqlite3", busy_timeout_ms=timeout)


def test_health_is_readonly(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize_database(path)
    with database_transaction(path, readonly=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO schema_metadata VALUES ('test', 'synthetic')")
    assert database_health(path)["schema_version"] == 1
