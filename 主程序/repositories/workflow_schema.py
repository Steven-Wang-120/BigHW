"""Explicit v3 -> v4 migration; the historical v3 DDL is never edited."""

from 主程序.repositories.reconcile_schema import SCHEMA_V3_SQL

USERS_SQL = """CREATE TABLE users (
 id TEXT PRIMARY KEY NOT NULL, username TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('FINANCE','SUPERVISOR','HANDLER','ADMIN')),
 active INTEGER NOT NULL CHECK(typeof(active)='integer' AND active IN (0,1)),
 version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=0))"""

DIFFERENCES_SQL = """CREATE TABLE differences_v4 (
 id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, cancel_id TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('MISSING_CUSTOMER','SUSPECTED_DUPLICATE','COUNTRY_CONFLICT',
 'MULTIPLE_CANDIDATES','INSUFFICIENT_QUANTITY','PRICE_MISMATCH','NO_CANDIDATE_IN_RANGE','NON_PRODUCT_REVIEW')),
 flags_json TEXT NOT NULL, candidates_json TEXT NOT NULL, message TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
 amount_units INTEGER NOT NULL CHECK(typeof(amount_units)='integer' AND amount_units>0),
 match_status TEXT NOT NULL CHECK(match_status='UNMATCHED'),
 work_status TEXT NOT NULL CHECK(work_status IN ('PENDING','IN_PROGRESS','CLOSED')),
 version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=0),
 assignee_id TEXT REFERENCES users(id), resolution_note TEXT,
 UNIQUE(run_id,cancel_id), UNIQUE(id,assignee_id),
 FOREIGN KEY(run_id,batch_id) REFERENCES runs(id,batch_id),
 FOREIGN KEY(cancel_id,batch_id) REFERENCES import_rows(id,batch_id),
 FOREIGN KEY(id,assignee_id) REFERENCES assignments(difference_id,assignee_id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(work_status='PENDING' OR assignee_id IS NOT NULL),
 CHECK((work_status='CLOSED' AND resolution_note IS NOT NULL AND length(trim(resolution_note, char(9)||char(10)||char(13)||' '))>0)
 OR (work_status!='CLOSED' AND resolution_note IS NULL)))"""

V4_SQL = [
    "CREATE INDEX user_active_role ON users(active,role)",
    """CREATE TABLE sessions (
 id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
 token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
 csrf_hash TEXT NOT NULL CHECK(length(csrf_hash)=64),
 expires_at TEXT NOT NULL, revoked_at TEXT)""",
    "CREATE INDEX session_expiry ON sessions(expires_at)",
    "CREATE INDEX session_user ON sessions(user_id)",
    """CREATE TABLE assignments (
 id TEXT PRIMARY KEY NOT NULL, difference_id TEXT NOT NULL UNIQUE REFERENCES differences(id),
 assignee_id TEXT NOT NULL REFERENCES users(id), assigned_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, UNIQUE(difference_id,assignee_id),
 FOREIGN KEY(difference_id,assignee_id) REFERENCES differences(id,assignee_id) DEFERRABLE INITIALLY DEFERRED)""",
    "CREATE INDEX difference_work_scope ON differences(run_id,work_status,assignee_id)",
    """CREATE TRIGGER assignment_valid BEFORE INSERT ON assignments
 WHEN NOT EXISTS(SELECT 1 FROM users WHERE id=NEW.assignee_id AND active=1 AND role='HANDLER')
 OR NOT EXISTS(SELECT 1 FROM users WHERE id=NEW.assigned_by AND active=1 AND role='SUPERVISOR')
 OR NOT EXISTS(SELECT 1 FROM differences WHERE id=NEW.difference_id AND work_status='PENDING' AND assignee_id IS NULL)
 BEGIN SELECT RAISE(ABORT,'invalid assignment'); END""",
    "CREATE TRIGGER assignment_no_update BEFORE UPDATE ON assignments BEGIN SELECT RAISE(ABORT,'assignment immutable'); END",
    "CREATE TRIGGER assignment_no_delete BEFORE DELETE ON assignments BEGIN SELECT RAISE(ABORT,'assignment immutable'); END",
]

OLD_COLUMNS = "id,run_id,batch_id,cancel_id,reason,flags_json,candidates_json,message,quantity,amount_units,match_status,work_status,version"


def migrate_v4(c):
    # No other v3 table has a foreign key to differences. Drop referencing triggers
    # temporarily so SQLite can rename the replacement without dangling SQL.
    guards = [sql for sql in SCHEMA_V3_SQL if sql.startswith("CREATE TRIGGER allocations_source_")
              or sql.startswith("CREATE TRIGGER differences_source_")]
    for sql in guards:
        c.execute("DROP TRIGGER " + sql.split()[2])
    c.execute(USERS_SQL)
    assignment_sql = next(sql for sql in V4_SQL if sql.startswith("CREATE TABLE assignments"))
    c.execute(assignment_sql)
    c.execute(DIFFERENCES_SQL)
    c.execute(f"INSERT INTO differences_v4 ({OLD_COLUMNS}) SELECT {OLD_COLUMNS} FROM differences")
    c.execute("DROP TABLE differences")
    c.execute("ALTER TABLE differences_v4 RENAME TO differences")
    c.execute("CREATE INDEX difference_run_reason ON differences(run_id,reason)")
    for sql in guards + V4_SQL:
        if sql != assignment_sql:
            c.execute(sql)
