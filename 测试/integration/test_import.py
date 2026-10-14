import csv
import hashlib
import json
import sqlite3

import pytest

from 测试.synthetic_xlsx import synthetic_workbook
from 主程序.repositories.database import initialize_database, upgrade_database, database_transaction, DatabaseError
from 主程序.repositories.xlsx_reader import InputError, SHEETS
from 主程序.services.import_service import import_snapshot, counts
from 主程序.services.snapshot_service import prepare_snapshot, sha256, json_text
from 主程序.services.manifest_service import verify_snapshot


@pytest.fixture
def prepared(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx")
    out = tmp_path / "snapshot"
    manifest = prepare_snapshot(source, out, expected_sha=sha256(source), selection=(SHEETS[1], 2, 4))
    db = tmp_path / "isolated.sqlite3"
    initialize_database(db)
    upgrade_database(db)
    return db, out, source, manifest["manifest_digest"], sha256(source)


def run_import(prepared, **kwargs):
    db, out, source, digest, sha = prepared
    return import_snapshot(db, out, source, digest, expected_sha=sha, **kwargs)


def test_tc55_upgrade_explicit_idempotent_and_unknown_rejected(tmp_path):
    db = tmp_path / "db.sqlite3"
    initialize_database(db)
    assert upgrade_database(db) is True
    before = db.read_bytes()
    assert upgrade_database(db) is False
    assert db.read_bytes() == before
    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as c:
        c.execute("CREATE TABLE unrelated (id INTEGER)")
    before = unknown.read_bytes()
    with pytest.raises(DatabaseError):
        upgrade_database(unknown)
    assert unknown.read_bytes() == before


def test_tc03_tc21_import_preserves_all_source_positions_and_counts(prepared):
    result = run_import(prepared)
    assert result["original_total"] == 6
    assert result["outside_count"] == 3
    assert result["selected_count"] == 3
    assert (result["sale_count"], result["cancel_count"], result["excluded_count"]) == (1, 1, 1)
    assert result["reason_counts"] == {"EMPTY_ROW": 1}
    assert result["categories"] == {"POST": 1, "PRODUCT": 2}
    assert result["non_product_count"] == 1
    assert result["cancel_coverage"]["non_product_valid_cancel"] == 1
    assert result["cancel_coverage"]["matching_denominator"] == 0
    assert counts(prepared[0], result["batch_id"])["selected_count"] == 3
    with database_transaction(prepared[0], readonly=True) as c:
        assert c.execute("SELECT source_row FROM import_rows ORDER BY source_row").fetchall() == [(2,), (3,), (4,)]
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE outcome='SUCCESS'").fetchone()[0] == 1


def test_tc46_duplicate_409_existing_batch_and_no_new_rows(prepared):
    first = run_import(prepared)
    with pytest.raises(InputError) as exc:
        run_import(prepared)
    assert exc.value.status == 409
    assert exc.value.code == "DUPLICATE_IMPORT"
    assert exc.value.details["batch_id"] == first["batch_id"]
    with database_transaction(prepared[0], readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE outcome='REJECTED'").fetchone()[0] == 1


def test_tc56_injected_write_failure_rolls_back_rows_persists_failed_and_retry(prepared):
    def fault(c):
        assert c.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0] == 3
        raise RuntimeError("synthetic write failure")
    with pytest.raises(InputError) as exc:
        run_import(prepared, fault=fault)
    assert exc.value.details["failure_metadata_persisted"] is True
    with database_transaction(prepared[0], readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0] == 0
        assert c.execute("SELECT status FROM import_batches").fetchone()[0] == "FAILED"
        assert c.execute("SELECT outcome FROM audit_events").fetchone()[0] == "FAILED"
    assert run_import(prepared)["attempt"] == 2


def test_tc57_success_audit_failure_rolls_back_rows(prepared):
    def fault(c):
        c.execute("CREATE TEMP TRIGGER fail_success BEFORE INSERT ON audit_events WHEN NEW.outcome='SUCCESS' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(InputError, match="AUDIT_WRITE_FAILED"):
        run_import(prepared, fault=fault)
    with database_transaction(prepared[0], readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0] == 0
        assert c.execute("SELECT status FROM import_batches").fetchone()[0] == "FAILED"


@pytest.mark.parametrize("kind", ["manifest", "csv", "full", "source"])
def test_tc46_tampering_rejected_before_any_batch(prepared, kind):
    db, out, source, _, _ = prepared
    path = {"manifest": out / "snapshot_10k.json", "csv": out / "retail_2010_rows_2_10001.csv", "full": out / "full_scan.json", "source": source}[kind]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(InputError):
        run_import(prepared)
    with database_transaction(db, readonly=True) as c:
        assert c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0


def rehash(out):
    p = out / "snapshot_10k.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["csv_sha256"] = sha256(out / m["csv_filename"])
    m.pop("snapshot_id")
    m["snapshot_id"] = hashlib.sha256(json_text(m).encode()).hexdigest()
    p.write_text(json_text(m) + "\n", encoding="utf-8")
    return sha256(p)


@pytest.mark.parametrize("field,value,code", [("source_row", "99", "SOURCE_IDENTITY_MISMATCH"),
                                             ("sheet", "Other", "SOURCE_IDENTITY_MISMATCH"),
                                             ("price", "3", "SOURCE_VALUE_MISMATCH")])
def test_tc58_rehashed_fabricated_source_identity_or_values_rejected(prepared, field, value, code):
    db, out, source, _, sha = prepared
    p = out / "retail_2010_rows_2_10001.csv"
    with p.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        names, rows = reader.fieldnames, list(reader)
    rows[0][field] = value
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(InputError, match=code):
        import_snapshot(db, out, source, rehash(out), expected_sha=sha)


def test_tc22_deleted_row_count_cannot_be_hidden_by_saved_json(prepared):
    batch = run_import(prepared)["batch_id"]
    with database_transaction(prepared[0]) as c:
        c.execute("DELETE FROM import_rows WHERE source_row=3")
    with pytest.raises(InputError, match="COUNT_MISMATCH"):
        counts(prepared[0], batch)


def test_tc59_database_constraints_and_audit_append_only(prepared):
    batch = run_import(prepared)["batch_id"]
    with database_transaction(prepared[0]) as c:
        for sql in ["UPDATE import_rows SET quantity=0 WHERE class='SALE'", "UPDATE import_rows SET quantity=NULL WHERE class='SALE'", "UPDATE import_rows SET category='POST',matching_eligible=1 WHERE class='SALE'",
                    "UPDATE audit_events SET outcome='FAILED'", "DELETE FROM audit_events"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE import_rows SET batch_id='missing'")


@pytest.mark.parametrize("field", ["quantity", "price_units", "amount_units"])
@pytest.mark.parametrize("value", [1.5, "invalid", "9223372036854775808", "-9223372036854775809"])
@pytest.mark.parametrize("kind", ["SALE", "EXCLUDED"])
def test_tc59_numeric_storage_rejects_real_text_and_overflow(prepared, field, value, kind):
    run_import(prepared)
    with database_transaction(prepared[0]) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"UPDATE import_rows SET {field}=? WHERE class=?", (value, kind))


def test_tc59_integer_boundaries_and_excluded_nulls(prepared):
    run_import(prepared)
    with database_transaction(prepared[0]) as c:
        for value in (-9223372036854775808, 9223372036854775807):
            c.execute("UPDATE import_rows SET quantity=?,price_units=?,amount_units=? WHERE class='EXCLUDED'", (value,) * 3)
            assert c.execute("SELECT typeof(quantity),typeof(price_units),typeof(amount_units) FROM import_rows WHERE class='EXCLUDED'").fetchone() == ("integer",) * 3
        c.execute("UPDATE import_rows SET quantity=NULL,price_units=NULL,amount_units=NULL WHERE class='EXCLUDED'")
        for field in ("quantity", "price_units", "amount_units"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(f"UPDATE import_rows SET {field}=NULL WHERE class='SALE'")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE import_rows SET quantity=1.5,price_units=20000,amount_units=30000 WHERE class='SALE'")


def test_counts_reads_persisted_version_not_current_constant(prepared, monkeypatch):
    result = run_import(prepared)
    monkeypatch.setattr("主程序.services.import_service.NORMALIZATION_VERSION", "future-normalization")
    assert counts(prepared[0], result["batch_id"])["normalization_version"] == result["normalization_version"]


def write_manifest(out, manifest):
    if isinstance(manifest, dict) and "snapshot_id" in manifest:
        original = {k: v for k, v in manifest.items() if k != "snapshot_id"}
        manifest["snapshot_id"] = hashlib.sha256(json_text(original).encode()).hexdigest()
    path = out / "snapshot_10k.json"
    path.write_text(json_text(manifest) + "\n", encoding="utf-8")
    return sha256(path)


@pytest.mark.parametrize("root", [[], None, "text", 1])
def test_manifest_wrong_root_returns_stable_input_error(prepared, root):
    _, out, source, _, sha = prepared
    digest = write_manifest(out, root)
    with pytest.raises(InputError, match="^MANIFEST_MISMATCH$"):
        verify_snapshot(out, source, digest, sha)


@pytest.mark.parametrize("field,value", [("sheet", []), ("selected_count", True), ("source_bytes", 1.5),
                                        ("csv_columns", {}), ("truncation", []), ("start_row", "2"),
                                        ("outside_count", -1), ("excel_epoch", {})])
def test_manifest_wrong_fields_return_stable_error(prepared, field, value):
    _, out, source, _, sha = prepared
    manifest = json.loads((out / "snapshot_10k.json").read_text(encoding="utf-8"))
    manifest[field] = value
    with pytest.raises(InputError, match="^MANIFEST_MISMATCH$"):
        verify_snapshot(out, source, write_manifest(out, manifest), sha)


@pytest.mark.parametrize("target", ["manifest", "full", "sheet", "aggregate"])
def test_manifest_required_field_missing(prepared, target):
    _, out, source, _, sha = prepared
    manifest = json.loads((out / "snapshot_10k.json").read_text(encoding="utf-8"))
    full_path = out / "full_scan.json"
    full = json.loads(full_path.read_text(encoding="utf-8"))
    obj, key = {"manifest": (manifest, "snapshot_id"), "full": (full, "sheets"),
                "sheet": (full["sheets"][SHEETS[0]], "last_value_row"),
                "aggregate": (full["sheets"][SHEETS[0]]["aggregate"], "classes")}[target]
    obj.pop(key)
    full_path.write_text(json_text(full), encoding="utf-8")
    manifest["full_scan_sha256"] = sha256(full_path)
    with pytest.raises(InputError, match="^MANIFEST_MISMATCH$"):
        verify_snapshot(out, source, write_manifest(out, manifest), sha)


@pytest.mark.parametrize("case", ["root", "sheets", "sheet", "aggregate", "classes", "count", "category_validity"])
def test_full_scan_wrong_shapes_and_counts(prepared, case):
    _, out, source, _, sha = prepared
    path = out / "full_scan.json"
    full = json.loads(path.read_text(encoding="utf-8"))
    aggregate = full["sheets"][SHEETS[0]]["aggregate"]
    if case == "root":
        full = []
    elif case == "sheets":
        full["sheets"] = []
    elif case == "sheet":
        full["sheets"][SHEETS[0]] = []
    elif case == "aggregate":
        full["sheets"][SHEETS[0]]["aggregate"] = []
    elif case == "count":
        aggregate["classes"]["SALE"] = "1"
    else:
        aggregate[case] = []
    path.write_text(json_text(full), encoding="utf-8")
    manifest = json.loads((out / "snapshot_10k.json").read_text(encoding="utf-8"))
    manifest["full_scan_sha256"] = sha256(path)
    with pytest.raises(InputError, match="^MANIFEST_MISMATCH$"):
        verify_snapshot(out, source, write_manifest(out, manifest), sha)


@pytest.mark.parametrize("field,value", [("cell_types", []), ("cell_types", {"quantity": "n"}),
                                        ("cells", []), ("cells", {"quantity": {}}),
                                        ("cells", {"quantity": None})])
def test_cell_metadata_wrong_root_or_missing_fields(prepared, field, value):
    _, out, source, _, sha = prepared
    path = out / "retail_2010_rows_2_10001.csv"
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields, records = reader.fieldnames, list(reader)
    records[0][field] = json_text(value)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
    with pytest.raises(InputError, match="^MANIFEST_MISMATCH$"):
        verify_snapshot(out, source, rehash(out), sha)


def test_manifest_internal_programming_errors_are_not_masked(prepared, monkeypatch):
    def programming_error(*args):
        raise KeyError("synthetic internal defect")
    monkeypatch.setattr("主程序.services.manifest_service.validate_source", programming_error)
    _, out, source, digest, sha = prepared
    with pytest.raises(KeyError, match="synthetic internal defect"):
        verify_snapshot(out, source, digest, sha)
