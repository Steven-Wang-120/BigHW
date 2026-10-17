"""Explicit business schema v2; B04 tables intentionally absent."""

SCHEMA_SQL = [
    """CREATE TABLE source_files (
 id TEXT PRIMARY KEY NOT NULL, sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256)=64),
 filename TEXT NOT NULL, bytes INTEGER NOT NULL CHECK(bytes>0), scan_json TEXT NOT NULL,
 scan_version TEXT NOT NULL, registered_at TEXT NOT NULL)""",
    """CREATE TABLE snapshots (
 id TEXT PRIMARY KEY NOT NULL, source_id TEXT NOT NULL REFERENCES source_files(id),
 sheet TEXT NOT NULL, start_row INTEGER NOT NULL CHECK(start_row>=2),
 end_row INTEGER NOT NULL CHECK(end_row>=start_row), sha256 TEXT NOT NULL UNIQUE,
 selection_version TEXT NOT NULL, selected_count INTEGER NOT NULL CHECK(selected_count=end_row-start_row+1),
 outside_count INTEGER NOT NULL CHECK(outside_count>=0), manifest_digest TEXT NOT NULL,
 manifest_json TEXT NOT NULL, UNIQUE(source_id,sheet,start_row,end_row,selection_version))""",
    """CREATE TABLE import_batches (
 id TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
 normalization_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('IMPORTING','SUCCESS','FAILED')),
 attempt INTEGER NOT NULL CHECK(attempt>0), counts_json TEXT, error_code TEXT,
 created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(snapshot_id,normalization_version,attempt),
 CHECK((status='SUCCESS' AND counts_json IS NOT NULL AND error_code IS NULL) OR status!='SUCCESS'))""",
    "CREATE UNIQUE INDEX one_success_import ON import_batches(snapshot_id,normalization_version) WHERE status='SUCCESS'",
    """CREATE TABLE import_rows (
 id TEXT PRIMARY KEY NOT NULL, batch_id TEXT NOT NULL REFERENCES import_batches(id),
 source_sha TEXT NOT NULL CHECK(length(source_sha)=64), sheet TEXT NOT NULL,
 source_row INTEGER NOT NULL CHECK(source_row>=2), invoice TEXT, stock TEXT, customer TEXT,
 country TEXT, description TEXT, quantity INTEGER, price_units INTEGER, amount_units INTEGER,
 invoice_time TEXT, class TEXT NOT NULL CHECK(class IN ('SALE','CANCEL','EXCLUDED')),
 primary_reason TEXT, flags_json TEXT NOT NULL, raw_json TEXT NOT NULL,
 category TEXT NOT NULL, matching_eligible INTEGER NOT NULL CHECK(matching_eligible IN (0,1)),
 raw_c_prefix INTEGER NOT NULL CHECK(raw_c_prefix IN (0,1)),
 UNIQUE(batch_id,source_sha,sheet,source_row),
 CHECK((class='EXCLUDED' AND primary_reason IS NOT NULL) OR
       (class='SALE' AND quantity>0 AND primary_reason IS NULL AND price_units>0 AND amount_units>0 AND invoice_time IS NOT NULL) OR
       (class='CANCEL' AND quantity<0 AND primary_reason IS NULL AND price_units>0 AND amount_units<0 AND invoice_time IS NOT NULL)),
 CHECK(matching_eligible=0 OR (class!='EXCLUDED' AND category='PRODUCT')),
 CHECK(class='EXCLUDED' OR (invoice IS NOT NULL AND stock IS NOT NULL AND quantity IS NOT NULL
 AND price_units IS NOT NULL AND amount_units IS NOT NULL AND invoice_time IS NOT NULL)),
 CHECK(quantity IS NULL OR typeof(quantity)='integer'),
 CHECK(price_units IS NULL OR typeof(price_units)='integer'),
 CHECK(amount_units IS NULL OR typeof(amount_units)='integer'),
 CHECK(class!='SALE' OR raw_c_prefix=0), CHECK(class!='CANCEL' OR raw_c_prefix=1))""",
    "CREATE INDEX row_matching_key ON import_rows(batch_id,class,customer,stock,price_units,invoice_time)",
    """CREATE TABLE audit_events (
 id TEXT PRIMARY KEY NOT NULL, actor_id TEXT, request_id TEXT NOT NULL,
 target_type TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('SUCCESS','FAILED','REJECTED')),
 error_code TEXT, summary_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX audit_target ON audit_events(target_type,target_id,created_at)",
    "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit append only'); END",
    "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit append only'); END",
]


def expected_objects(version=2):
    import sqlite3
    with sqlite3.connect(":memory:") as c:
        from 主程序.repositories.database import METADATA_SQL
        c.execute(METADATA_SQL)
        for sql in SCHEMA_SQL:
            c.execute(sql)
        if version >= 3:
            from 主程序.repositories.reconcile_schema import SCHEMA_V3_SQL
            for sql in SCHEMA_V3_SQL:
                c.execute(sql)
        if version == 4:
            from 主程序.repositories.workflow_schema import migrate_v4
            c.execute("PRAGMA foreign_keys=ON")
            migrate_v4(c)
        return c.execute("SELECT name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
