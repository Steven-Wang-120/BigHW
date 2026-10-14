import json
import sqlite3
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from 测试.synthetic_xlsx import synthetic_workbook, rewrite_workbook
from 主程序.domain.serialization import json_text
from 主程序.repositories.database import initialize_database, upgrade_database, database_transaction, database_health, DatabaseError
from 主程序.repositories.schema import SCHEMA_SQL
from 主程序.repositories.xlsx_reader import InputError, SHEETS
from 主程序.services.snapshot_service import prepare_snapshot, sha256
from 主程序.services.import_service import import_snapshot, counts
from 主程序.services.reconcile_service import reconcile, run_summary
from 主程序.web.app import create_app


@pytest.fixture
def imported(tmp_path):
    rows = {2: ["S1", "SKU", "first", "10", "40500", "2", "123", "UK"],
            3: ["C1", "SKU", "other description", "-3", "40501", "2", "123", "UK"],
            4: ["C2", "SKU", "remaining", "-7", "40502", "2", "123", "UK"],
            5: ["C3", "SKU", "excess", "-1", "40503", "2", "123", "UK"],
            6: ["C4", "POST", "synthetic fee", "-1", "40504", "2", "123", "UK"]}
    source = synthetic_workbook(tmp_path / "source.xlsx", rows)
    out = tmp_path / "snapshot"
    m = prepare_snapshot(source, out, expected_sha=sha256(source), selection=(SHEETS[1], 2, 6))
    db = tmp_path / "isolated.sqlite3"
    initialize_database(db)
    upgrade_database(db)
    batch = import_snapshot(db, out, source, m["manifest_digest"], expected_sha=sha256(source))["batch_id"]
    return db, batch


def test_v2_to_v3_additive_preserves_exact_facts_and_audits(imported):
    db, batch = imported
    with database_transaction(db, readonly=True) as c:
        before = {name: c.execute(f"SELECT * FROM {name}").fetchall() for name in ("source_files", "snapshots", "import_batches", "import_rows", "audit_events")}
        objects = dict(c.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
    assert database_health(db)["schema_version"] == 2
    assert upgrade_database(db, target_version=3)
    assert database_health(db) == {"schema_version": 3, "foreign_keys": True, "scope": "business-reconcile"}
    with database_transaction(db, readonly=True) as c:
        assert before == {name: c.execute(f"SELECT * FROM {name}").fetchall() for name in before}
        after_objects = dict(c.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
        assert all(after_objects[k] == v for k, v in objects.items())
    before_bytes = db.read_bytes()
    assert upgrade_database(db, target_version=3) is False
    assert db.read_bytes() == before_bytes
    assert counts(db, batch)["selected_count"] == 5


def test_v1_direct_explicit_v3_and_loose_v2_rejected(tmp_path):
    fresh = tmp_path / "fresh.sqlite3"
    initialize_database(fresh)
    assert upgrade_database(fresh, target_version=3)
    loose = tmp_path / "loose.sqlite3"
    initialize_database(loose)
    with sqlite3.connect(loose) as c:
        for sql in SCHEMA_SQL:
            sql = sql.replace("CHECK(quantity IS NULL OR typeof(quantity)='integer')", "CHECK(quantity IS NULL OR quantity>0)")
            c.execute(sql)
        c.execute("UPDATE schema_metadata SET value='business-import' WHERE key='scope'")
        c.execute("UPDATE schema_metadata SET value='2' WHERE key='version'")
        c.execute("PRAGMA user_version=2")
    original = loose.read_bytes()
    with pytest.raises(DatabaseError, match="SCHEMA_UNRECOGNIZED"):
        upgrade_database(loose, target_version=3)
    assert original == loose.read_bytes()


def test_tc31_complete_reuse_no_new_success_audit_and_import_stays_historical(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    result = reconcile(db, batch)
    assert result["reused"] is False
    assert (result["matched"], result["suspected"], result["no_original"], result["non_product_review"]) == (2, 1, 0, 1)
    assert result["effective_cancel_count"] == 4
    assert result["quantity_delta"] == result["amount_delta_units"] == "0"
    before = db.read_bytes()
    reused = reconcile(db, batch)
    assert reused["reused"] is True and reused["run_id"] == result["run_id"]
    assert before == db.read_bytes()
    assert run_summary(db, result["run_id"])["rule_digest"] == result["rule_digest"]
    assert counts(db, batch)["cancel_coverage"]["matched"] is None
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM differences").fetchone()[0] == 2
        assert c.execute("SELECT match_status,work_status,version FROM differences").fetchall() == [("UNMATCHED", "PENDING", 0)] * 2
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE target_type='reconciliation_run' AND outcome='SUCCESS'").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["write", "audit"])
def test_tc32_publish_failure_rollback_and_same_run_retry(imported, failure):
    db, batch = imported
    upgrade_database(db, target_version=3)
    def fault(c):
        assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 2
        if failure == "write":
            raise RuntimeError("synthetic publish failure")
        c.execute("CREATE TEMP TRIGGER fail_reconcile_audit BEFORE INSERT ON audit_events WHEN NEW.target_type='reconciliation_run' AND NEW.outcome='SUCCESS' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(InputError) as exc:
        reconcile(db, batch, fault=fault)
    error = exc.value
    assert error.code == ("AUDIT_WRITE_FAILED" if failure == "audit" else "RECONCILE_WRITE_FAILED")
    assert error.details["batch_id"] == batch
    assert error.details["attempt"] == 1 and error.details["retry_action"] == "LOCAL_RECONCILE"
    assert error.details["formal_POST_RUNS_implemented"] is False
    with database_transaction(db, readonly=True) as c:
        for table in ("sale_lots", "sale_lot_rows", "allocations", "differences"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert c.execute("SELECT status FROM runs").fetchone()[0] == "FAILED"
        assert c.execute("SELECT status FROM import_batches WHERE id=?", (batch,)).fetchone()[0] == "SUCCESS"
        assert c.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0] == 5
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchone()[0] == "FAILED"
    with pytest.raises(InputError, match="RUN_NOT_READY"):
        run_summary(db, error.details["run_id"])
    completed = reconcile(db, batch)
    assert completed["run_id"] == error.details["run_id"] and completed["attempt"] == 2
    assert reconcile(db, batch)["reused"] is True


def test_running_409_missing_batch_and_v2_guard(imported):
    db, batch = imported
    with pytest.raises(InputError, match="SCHEMA_UPGRADE_REQUIRED"):
        reconcile(db, batch)
    upgrade_database(db, target_version=3)
    with pytest.raises(InputError, match="BATCH_NOT_FOUND"):
        reconcile(db, "missing")
    from 主程序.repositories.reconcile_repository import start_run
    with database_transaction(db) as c:
        run_id, _, _ = start_run(c, batch)
    with pytest.raises(InputError, match="RUN_IN_PROGRESS") as exc:
        reconcile(db, batch)
    assert exc.value.status == 409 and exc.value.details["run_id"] == run_id


def test_tc14g_overflow_marks_failed_without_changing_import_verdicts(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    # Test-owned persisted valid integer SALE rows; each amount fits, lot total does not.
    with database_transaction(db) as c:
        sale = list(c.execute("SELECT * FROM import_rows WHERE class='SALE'").fetchone())
        c.execute("UPDATE import_rows SET quantity=9223372036854775807,price_units=1,amount_units=9223372036854775807 WHERE id=?", (sale[0],))
        sale[0], sale[4], sale[10], sale[11], sale[12] = "extra-sale", 99, 1, 1, 1
        c.execute("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in sale) + ")", sale)
        saved = json.loads(c.execute("SELECT counts_json FROM import_batches WHERE id=?", (batch,)).fetchone()[0])
        saved["selected_count"] += 1
        saved["sale_count"] += 1
        saved["categories"]["PRODUCT"] += 1
        saved["category_validity"]["PRODUCT|SALE"] += 1
        saved["product_count"] += 1
        saved["matching_eligible_count"] += 1
        saved["original_total"] += 1
        c.execute("UPDATE import_batches SET counts_json=? WHERE id=?", (json_text(saved), batch))
        manifest = json.loads(c.execute("SELECT manifest_json FROM snapshots").fetchone()[0])
        manifest["selected_count"] += 1
        manifest["original_total"] += 1
        c.execute("UPDATE snapshots SET end_row=end_row+1,selected_count=selected_count+1,manifest_json=?", (json_text(manifest),))
        before = c.execute("SELECT id,class,quantity,price_units,amount_units FROM import_rows ORDER BY id").fetchall()
    with pytest.raises(InputError, match="NUMERIC_OVERFLOW"):
        reconcile(db, batch)
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT id,class,quantity,price_units,amount_units FROM import_rows ORDER BY id").fetchall() == before
        assert c.execute("SELECT status,error_code FROM runs").fetchone() == ("FAILED", "NUMERIC_OVERFLOW")
        assert c.execute("SELECT COUNT(*) FROM sale_lots").fetchone()[0] == 0


def test_cross_run_batch_sources_numeric_unique_and_exclusive_constraints(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    run_id = reconcile(db, batch)["run_id"]
    with database_transaction(db) as c:
        second = list(c.execute("SELECT * FROM import_batches").fetchone())
        second[0], second[2], second[4] = "other-batch", "synthetic-v2", 2
        c.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?)", second)
        other_row = list(c.execute("SELECT * FROM import_rows WHERE class='CANCEL' LIMIT 1").fetchone())
        other_row[0], other_row[1] = "other-cancel", "other-batch"
        c.execute("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in other_row) + ")", other_row)
        run = list(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        run[0], run[1], run[3], run[6], run[8] = "other-run", "other-batch", "synthetic-v2", "RUNNING", None
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", run)
        allocation = list(c.execute("SELECT * FROM allocations LIMIT 1").fetchone())
        lot = list(c.execute("SELECT * FROM sale_lots").fetchone())
        cases = ["UPDATE allocations SET quantity=1.5", "UPDATE differences SET amount_units='invalid'",
                 "UPDATE sale_lots SET remaining=-1", "UPDATE allocations SET run_id='other-run'",
                 "UPDATE allocations SET batch_id='other-batch',cancel_id='other-cancel'",
                 "UPDATE sale_lot_rows SET row_id='other-cancel'",
                 "UPDATE differences SET cancel_id='other-cancel'", "UPDATE differences SET work_status='CLOSED'"]
        for sql in cases:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        duplicate = allocation.copy()
        duplicate[0] = "duplicate-allocation"
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?)", duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO differences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("double", run_id, batch, allocation[3], "MULTIPLE_CANDIDATES", "[]", "[]", "test", allocation[5], allocation[6], "UNMATCHED", "PENDING", 0))
        invalid_lot = lot.copy()
        invalid_lot[0], invalid_lot[1], invalid_lot[3] = "bad-lot", "other-run", "bad-key"
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO sale_lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", invalid_lot)


def test_read_only_run_json_amount_strings_counts_links_and_no_write_api(imported):
    db, batch = imported
    upgrade_database(db, target_version=3)
    result = reconcile(db, batch)
    before = db.read_bytes()
    with TestClient(create_app(db)) as client:
        assert client.get("/health").json()["schema_version"] == 3
        assert client.get("/evidence/run?run_id=" + result["run_id"]).status_code == 200
        assert client.get("/static/js/run.js").status_code == 200
        data = client.get("/evidence/run/" + result["run_id"]).json()
        assert type(data["cancel_amount_units"]) is str and data["amount_delta_units"] == "0"
        historical = client.get("/evidence/counts/" + batch).json()
        assert historical["cancel_coverage"]["matched"] is None
        assert historical["runs"] == [{"run_id": result["run_id"], "status": "COMPLETED", "attempt": 1}]
        assert client.get("/evidence/run/missing").status_code == 404
        assert client.post("/evidence/run/" + result["run_id"]).status_code == 405
        assert client.post("/api/v1/runs").status_code == 404
    assert db.read_bytes() == before


def test_cli_reconcile_and_summary_real_boundary(imported):
    db, batch = imported
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    def command(*args):
        return subprocess.run([sys.executable, "-m", "主程序.cli", *args], cwd=root, env=env, capture_output=True, timeout=20)
    upgraded = command("upgrade-db", "--database", str(db), "--target-version", "3")
    assert upgraded.returncode == 0
    first = command("reconcile", "--database", str(db), "--batch-id", batch)
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    again = json.loads(command("reconcile", "--database", str(db), "--batch-id", batch).stdout)
    assert again["run_id"] == result["run_id"] and again["reused"] is True
    summary = command("run-summary", "--database", str(db), "--run-id", result["run_id"])
    assert summary.returncode == 0 and json.loads(summary.stdout)["matched"] == 2
    missing = command("run-summary", "--database", str(db), "--run-id", "missing")
    assert missing.returncode == 1 and json.loads(missing.stderr)["error"]["code"] == "RUN_NOT_FOUND"


def test_tc18_imported_duplicate_sales_retained_but_suppressed(tmp_path):
    sale = ["S1", "SKU", "same synthetic", "5", "40500", "2", "123", "UK"]
    source = synthetic_workbook(tmp_path / "duplicate.xlsx", {2: sale, 3: sale, 4: ["C1", "SKU", "cancel", "-2", "40501", "2", "123", "UK"]})
    out = tmp_path / "snapshot"
    manifest = prepare_snapshot(source, out, expected_sha=sha256(source), selection=(SHEETS[1], 2, 4))
    db = tmp_path / "duplicate.sqlite3"
    initialize_database(db)
    upgrade_database(db, target_version=3)
    batch = import_snapshot(db, out, source, manifest["manifest_digest"], expected_sha=sha256(source))["batch_id"]
    result = reconcile(db, batch)
    assert result["difference_reasons"] == {"SUSPECTED_DUPLICATE": 1}
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT quantity FROM sale_lots").fetchone()[0] == 10
        assert c.execute("SELECT COUNT(*) FROM sale_lot_rows").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 0


def test_failure_audit_unavailable_is_explicit_not_claimed_persisted(imported, monkeypatch):
    db, batch = imported
    upgrade_database(db, target_version=3)
    def fail_metadata(*args):
        raise RuntimeError("synthetic failure metadata unavailable")
    monkeypatch.setattr("主程序.repositories.reconcile_repository.fail_run", fail_metadata)
    def fault(c):
        raise RuntimeError("synthetic write failed")
    with pytest.raises(InputError, match="AUDIT_WRITE_FAILED") as exc:
        reconcile(db, batch, fault=fault)
    assert exc.value.details["failure_metadata_persisted"] is False
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status FROM runs").fetchone()[0] == "RUNNING"
        assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 0


@pytest.mark.parametrize("mixed", [False, True])
def test_invoice_formula_cached_c_preserves_raw_denominator_through_import(tmp_path, mixed):
    from 主程序.repositories.xlsx_reader import NS
    from 主程序.repositories.reconcile_repository import read_rows
    import xml.etree.ElementTree as ET
    rows = {2: ["C-SYNTHETIC", "SKU", "synthetic formula invoice", "-1", "40500", "2", "123", "UK"]}
    if mixed:
        rows.update({3: ["S1", "SKU", "sale", "5", "40500", "2", "123", "UK"],
                     4: ["C1", "SKU", "cancel", "-2", "40501", "2", "123", "UK"],
                     5: ["C2", "POST", "fee", "-1", "40501", "2", "123", "UK"]})
    source = synthetic_workbook(tmp_path / "formula-invoice.xlsx", rows)
    def formula_cache(root):
        invoice = root.find(NS + "sheetData").findall(NS + "row")[1].find(NS + "c")
        invoice.clear()
        invoice.attrib.update({"r": "A2", "t": "str"})
        ET.SubElement(invoice, NS + "f").text = '"C-SYNTHETIC"'
        ET.SubElement(invoice, NS + "v").text = "C-SYNTHETIC"
    for n in (1, 2):
        rewrite_workbook(source, f"xl/worksheets/sheet{n}.xml", formula_cache)
    out = tmp_path / "formula-snapshot"
    manifest = prepare_snapshot(source, out, expected_sha=sha256(source), selection=(SHEETS[1], 2, max(rows)))
    db = tmp_path / "formula.sqlite3"
    initialize_database(db)
    upgrade_database(db, target_version=3)
    batch = import_snapshot(db, out, source, manifest["manifest_digest"], expected_sha=sha256(source))["batch_id"]
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT class,invoice,raw_c_prefix FROM import_rows WHERE source_row=2").fetchone() == ("EXCLUDED", None, 1)
        formula = next(r for r in read_rows(c, batch) if r.ref.source_row == 2)
        assert formula.raw_c_prefix == 1
    result = reconcile(db, batch)
    assert result["raw_c_prefix_count"] == (3 if mixed else 1)
    assert result["excluded_c_prefix"] == 1
    assert result["product_valid_cancel"] == (1 if mixed else 0)
    assert result["non_product_review"] == (1 if mixed else 0)
    assert result["effective_cancel_count"] == (2 if mixed else 0)
    assert result["difference_count"] == (1 if mixed else 0)
    assert result["raw_c_delta"] == result["product_delta"] == result["coverage_delta"] == 0
    assert counts(db, batch)["cancel_coverage"]["excluded_c_prefix"] == 1


def test_first_publication_never_calls_postcommit_run_summary(imported, monkeypatch):
    db, batch = imported
    upgrade_database(db, target_version=3)
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise DatabaseError("DATABASE_UNAVAILABLE")
    monkeypatch.setattr("主程序.services.reconcile_service.run_summary", unavailable)
    result = reconcile(db, batch)
    assert calls == []
    assert result["status"] == "COMPLETED" and result["reused"] is False
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status FROM runs").fetchone()[0] == "COMPLETED"
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchall() == [("SUCCESS",)]
    # A later read/reuse failure may propagate but must never append FAILED to a completed run.
    with pytest.raises(DatabaseError, match="DATABASE_UNAVAILABLE"):
        reconcile(db, batch)
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchall() == [("SUCCESS",)]


def test_same_transaction_summary_failure_rolls_back_and_retries_same_run(imported, monkeypatch):
    from 主程序.repositories import reconcile_repository as repository
    db, batch = imported
    upgrade_database(db, target_version=3)
    original = repository.completed_summary
    calls = []
    def unavailable(c, run_id):
        assert c.in_transaction
        assert c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "COMPLETED"
        assert c.execute("SELECT COUNT(*) FROM differences").fetchone()[0] == 2
        calls.append(run_id)
        raise DatabaseError("DATABASE_UNAVAILABLE")
    monkeypatch.setattr(repository, "completed_summary", unavailable)
    with pytest.raises(InputError, match="DATABASE_UNAVAILABLE") as exc:
        reconcile(db, batch)
    run_id = exc.value.details["run_id"]
    assert calls == [run_id] and exc.value.details["failure_metadata_persisted"] is True
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status FROM runs").fetchone()[0] == "FAILED"
        for name in ("sale_lots", "sale_lot_rows", "allocations", "differences"):
            assert c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchall() == [("FAILED",)]
        assert c.execute("SELECT status FROM import_batches").fetchone()[0] == "SUCCESS"
    monkeypatch.setattr(repository, "completed_summary", original)
    result = reconcile(db, batch)
    assert result["run_id"] == run_id and result["attempt"] == 2
    assert reconcile(db, batch)["reused"] is True
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run' ORDER BY created_at").fetchall() == [("FAILED",), ("SUCCESS",)]


@pytest.mark.parametrize("operation", ["publish", "fail_run"])
def test_zero_row_transition_never_appends_result_audit(imported, operation):
    from 主程序.repositories import reconcile_repository as repository
    from 主程序.domain.matching import reconcile_rows
    db, batch = imported
    upgrade_database(db, target_version=3)
    result = reconcile(db, batch)
    with database_transaction(db) as c:
        if operation == "publish":
            # Empty writes reach the transition guard without duplicating existing facts.
            with pytest.raises(InputError, match="RUN_STATE_CONFLICT"):
                repository.publish(c, result["run_id"], batch, reconcile_rows([]), {})
        else:
            with pytest.raises(InputError, match="RUN_STATE_CONFLICT"):
                repository.fail_run(c, result["run_id"], "SYNTHETIC")
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchall() == [("SUCCESS",)]


@pytest.mark.parametrize("field", ["raw_c_prefix_count", "product_valid_cancel", "non_product_review", "excluded_c_prefix",
                                  "effective_cancel_count", "cancel_quantity", "allocated_quantity", "unmatched_quantity",
                                  "cancel_amount_units", "allocated_amount_units", "unmatched_amount_units"])
def test_publish_rejects_summary_batch_or_triangle_mismatch(imported, monkeypatch, field):
    from 主程序.services import reconcile_service as service
    db, batch = imported
    upgrade_database(db, target_version=3)
    original = service.reconcile_rows
    def inconsistent(rows):
        result = original(rows)
        value = result.summary[field]
        result.summary[field] = str(int(value) + 1) if isinstance(value, str) else value + 1
        return result
    monkeypatch.setattr(service, "reconcile_rows", inconsistent)
    with pytest.raises(InputError, match="COUNT_MISMATCH") as exc:
        reconcile(db, batch)
    assert exc.value.details["failure_metadata_persisted"] is True
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT status,error_code FROM runs").fetchone() == ("FAILED", "COUNT_MISMATCH")
        for name in ("sale_lots", "sale_lot_rows", "allocations", "differences"):
            assert c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
        assert c.execute("SELECT outcome FROM audit_events WHERE target_type='reconciliation_run'").fetchall() == [("FAILED",)]
