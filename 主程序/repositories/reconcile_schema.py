"""Additive B04 schema; strict v2 objects remain unchanged."""

SCHEMA_V3_SQL = [
    "CREATE UNIQUE INDEX batch_run_identity ON import_batches(id,snapshot_id,normalization_version)",
    "CREATE UNIQUE INDEX row_batch_identity ON import_rows(id,batch_id)",
    """CREATE TABLE runs (
 id TEXT PRIMARY KEY NOT NULL, batch_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 normalization_version TEXT NOT NULL, rule_digest TEXT NOT NULL CHECK(length(rule_digest)=64),
 rule_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED')),
 attempt INTEGER NOT NULL CHECK(typeof(attempt)='integer' AND attempt>0), summary_json TEXT, error_code TEXT,
 created_at TEXT NOT NULL, finished_at TEXT,
 FOREIGN KEY(batch_id,snapshot_id,normalization_version) REFERENCES import_batches(id,snapshot_id,normalization_version),
 UNIQUE(snapshot_id,normalization_version,rule_digest), UNIQUE(id,batch_id),
 CHECK((status='COMPLETED' AND summary_json IS NOT NULL AND error_code IS NULL) OR
       (status!='COMPLETED' AND summary_json IS NULL)))""",
    """CREATE TABLE sale_lots (
 id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, lot_key TEXT NOT NULL,
 invoice TEXT NOT NULL, customer TEXT, stock TEXT NOT NULL,
 price_units INTEGER NOT NULL CHECK(typeof(price_units)='integer' AND price_units>0),
 invoice_time TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
 remaining INTEGER NOT NULL CHECK(typeof(remaining)='integer' AND remaining BETWEEN 0 AND quantity),
 amount_units INTEGER NOT NULL CHECK(typeof(amount_units)='integer' AND amount_units>0),
 countries_json TEXT NOT NULL, flags_json TEXT NOT NULL,
 FOREIGN KEY(run_id,batch_id) REFERENCES runs(id,batch_id), UNIQUE(run_id,lot_key), UNIQUE(id,run_id,batch_id),
 CHECK(typeof(price_units*quantity)='integer' AND amount_units=price_units*quantity))""",
    "CREATE INDEX lot_matching_key ON sale_lots(run_id,customer,stock,price_units,invoice_time)",
    """CREATE TABLE sale_lot_rows (
 lot_id TEXT NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, row_id TEXT NOT NULL,
 PRIMARY KEY(lot_id,row_id), UNIQUE(run_id,row_id),
 FOREIGN KEY(lot_id,run_id,batch_id) REFERENCES sale_lots(id,run_id,batch_id),
 FOREIGN KEY(row_id,batch_id) REFERENCES import_rows(id,batch_id))""",
    """CREATE TABLE allocations (
 id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL,
 cancel_id TEXT NOT NULL, lot_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
 amount_units INTEGER NOT NULL CHECK(typeof(amount_units)='integer' AND amount_units>0),
 UNIQUE(run_id,cancel_id),
 FOREIGN KEY(run_id,batch_id) REFERENCES runs(id,batch_id),
 FOREIGN KEY(lot_id,run_id,batch_id) REFERENCES sale_lots(id,run_id,batch_id),
 FOREIGN KEY(cancel_id,batch_id) REFERENCES import_rows(id,batch_id))""",
    """CREATE TABLE differences (
 id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, cancel_id TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('MISSING_CUSTOMER','SUSPECTED_DUPLICATE','COUNTRY_CONFLICT',
 'MULTIPLE_CANDIDATES','INSUFFICIENT_QUANTITY','PRICE_MISMATCH','NO_CANDIDATE_IN_RANGE','NON_PRODUCT_REVIEW')),
 flags_json TEXT NOT NULL, candidates_json TEXT NOT NULL, message TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
 amount_units INTEGER NOT NULL CHECK(typeof(amount_units)='integer' AND amount_units>0),
 match_status TEXT NOT NULL CHECK(match_status='UNMATCHED'),
 work_status TEXT NOT NULL CHECK(work_status='PENDING'), version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version=0),
 UNIQUE(run_id,cancel_id),
 FOREIGN KEY(run_id,batch_id) REFERENCES runs(id,batch_id),
 FOREIGN KEY(cancel_id,batch_id) REFERENCES import_rows(id,batch_id))""",
    "CREATE INDEX difference_run_reason ON differences(run_id,reason)",
    """CREATE TRIGGER run_success_batch BEFORE INSERT ON runs
 WHEN NOT EXISTS(SELECT 1 FROM import_batches WHERE id=NEW.batch_id AND status='SUCCESS')
 BEGIN SELECT RAISE(ABORT,'run requires successful batch'); END""",
]

# INSERT and UPDATE must enforce the same source/run associations.
GUARDS = {
    "sale_lot_rows": """NOT EXISTS(SELECT 1 FROM import_rows r JOIN sale_lots l ON l.id=NEW.lot_id
 WHERE r.id=NEW.row_id AND r.batch_id=NEW.batch_id AND l.run_id=NEW.run_id AND l.batch_id=NEW.batch_id
 AND r.class='SALE' AND r.matching_eligible=1 AND r.category='PRODUCT'
 AND r.invoice=l.invoice AND r.customer IS l.customer AND r.stock=l.stock
 AND r.price_units=l.price_units AND r.invoice_time=l.invoice_time)""",
    "allocations": """NOT EXISTS(SELECT 1 FROM import_rows r JOIN sale_lots l ON l.id=NEW.lot_id
 WHERE r.id=NEW.cancel_id AND r.batch_id=NEW.batch_id AND l.run_id=NEW.run_id AND l.batch_id=NEW.batch_id
 AND r.class='CANCEL' AND r.matching_eligible=1 AND r.category='PRODUCT' AND r.customer IS NOT NULL
 AND r.customer=l.customer AND r.stock=l.stock AND r.price_units=l.price_units
 AND NEW.quantity=-r.quantity AND typeof(-r.amount_units)='integer' AND NEW.amount_units=-r.amount_units)
 OR EXISTS(SELECT 1 FROM differences d WHERE d.run_id=NEW.run_id AND d.cancel_id=NEW.cancel_id)""",
    "differences": """NOT EXISTS(SELECT 1 FROM import_rows r WHERE r.id=NEW.cancel_id AND r.batch_id=NEW.batch_id
 AND r.class='CANCEL' AND NEW.quantity=-r.quantity AND typeof(-r.amount_units)='integer' AND NEW.amount_units=-r.amount_units
 AND ((r.category!='PRODUCT' AND NEW.reason='NON_PRODUCT_REVIEW') OR
      (r.category='PRODUCT' AND NEW.reason!='NON_PRODUCT_REVIEW')))
 OR EXISTS(SELECT 1 FROM allocations a WHERE a.run_id=NEW.run_id AND a.cancel_id=NEW.cancel_id)""",
}
for table, guard in GUARDS.items():
    for action in ("INSERT", "UPDATE"):
        SCHEMA_V3_SQL.append(f"CREATE TRIGGER {table}_source_{action.lower()} BEFORE {action} ON {table} WHEN {guard} BEGIN SELECT RAISE(ABORT,'invalid source association'); END")
