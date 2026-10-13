"""Explicit, non-destructive SQLite bootstrap; business tables are not implemented."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1
APPLICATION_ID = 0x42485731
BUSY_TIMEOUT_MS = 1000
METADATA_SQL = "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
METADATA_ROWS = [("scope", "bootstrap-only"), ("version", "1")]


class DatabaseError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def connect_database(path: Path, *, readonly: bool = False, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 5000:
        raise ValueError("busy_timeout_ms must be between 1 and 5000")
    path = Path(path).resolve()
    if not path.is_file():
        raise DatabaseError("DATABASE_NOT_INITIALIZED")
    mode = "ro" if readonly else "rw"
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True,
                                     timeout=busy_timeout_ms / 1000, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseError("FOREIGN_KEYS_DISABLED")
        return connection
    except (sqlite3.Error, DatabaseError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError("DATABASE_UNAVAILABLE") from exc


def validate_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DatabaseError("SCHEMA_VERSION_UNSUPPORTED")
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    objects = connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
    ).fetchall()
    if version != SCHEMA_VERSION or application != APPLICATION_ID or objects != [("schema_metadata", METADATA_SQL)]:
        raise DatabaseError("SCHEMA_UNRECOGNIZED")
    rows = connection.execute("SELECT key, value FROM schema_metadata ORDER BY key").fetchall()
    if rows != METADATA_ROWS:
        raise DatabaseError("SCHEMA_UNRECOGNIZED")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DatabaseError("FOREIGN_KEYS_DISABLED")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DatabaseError("FOREIGN_KEY_CHECK_FAILED")


def _translate(exc: sqlite3.Error) -> DatabaseError:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return DatabaseError("DATABASE_BUSY")
    return DatabaseError("DATABASE_UNAVAILABLE")


def initialize_database(path: Path) -> bool:
    """Return True only for a new bootstrap database; never migrate an unknown file."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        with database_transaction(path, readonly=True):
            return False
    else:
        os.close(descriptor)
    connection = connect_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 0 or connection.execute(
            "SELECT 1 FROM sqlite_schema"
        ).fetchone() is not None:
            raise DatabaseError("SCHEMA_UNRECOGNIZED")
        connection.execute(METADATA_SQL)
        connection.executemany("INSERT INTO schema_metadata (key, value) VALUES (?, ?)", METADATA_ROWS)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        validate_schema(connection)
        connection.commit()
        return True
    except sqlite3.Error as exc:
        connection.rollback()
        raise _translate(exc) from exc
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def database_transaction(path: Path, *, readonly: bool = False):
    connection = connect_database(path, readonly=readonly)
    try:
        connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        validate_schema(connection)
        yield connection
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise _translate(exc) from exc
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def database_health(path: Path) -> dict[str, object]:
    with database_transaction(path, readonly=True):
        return {"schema_version": SCHEMA_VERSION, "foreign_keys": True, "scope": "bootstrap-only"}
