"""FR-31 exact same-sheet range-quality tests, synthetic only."""

import json
from pathlib import Path

import pytest

from 测试.synthetic_xlsx import synthetic_workbook
from 主程序.repositories.database import database_transaction, initialize_database, upgrade_database
from 主程序.repositories.xlsx_reader import InputError, SHEETS
from 主程序.services.snapshot_service import prepare_snapshot, sha256, QUALITY_VERSION
from 主程序.services.manifest_service import verify_snapshot
from 主程序.services.import_service import import_snapshot


def test_v2_marks_only_all_selected_rows_for_invoice_found_outside_same_sheet(tmp_path):
    row = ["INV-1", "SKU", "synthetic", "1", "40500", "2", "123", "UK"]
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: row, 3: row, 4: row}, tail=5)
    result = prepare_snapshot(source, tmp_path / "v2", expected_sha=sha256(source), selection=(SHEETS[1], 2, 3), quality_version=QUALITY_VERSION)
    manifest = json.loads((tmp_path / "v2/snapshot_10k.json").read_text(encoding="utf-8"))
    assert manifest["selection_version"] == "source-position-v2-quality"
    assert manifest["quality"]["row_flags"]["TRUNCATED_INVOICE_RANGE"] == [2, 3]
    assert manifest["quality"]["row_flag_counts"]["TRUNCATED_INVOICE_RANGE"] == 2
    assert result["snapshot_id"] == manifest["snapshot_id"]


def test_v2_does_not_mark_invoice_only_at_selected_edge_or_other_sheet(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: ["EDGE", "SKU", "x", "1", "40500", "2", "123", "UK"], 4: ["OUT", "SKU", "x", "1", "40500", "2", "123", "UK"]}, tail=5)
    result = prepare_snapshot(source, tmp_path / "v2", expected_sha=sha256(source), selection=(SHEETS[1], 2, 3), quality_version=QUALITY_VERSION)
    manifest = json.loads((tmp_path / "v2/snapshot_10k.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["row_flags"]["TRUNCATED_INVOICE_RANGE"] == []


def test_v2_quality_digest_and_tamper_rejected(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: ["INV", "SKU", "x", "1", "40500", "2", "123", "UK"], 4: ["INV", "SKU", "x", "1", "40500", "2", "123", "UK"]})
    result = prepare_snapshot(source, tmp_path / "v2", expected_sha=sha256(source), selection=(SHEETS[1], 2, 2), quality_version=QUALITY_VERSION)
    with pytest.raises(InputError, match="MANIFEST_MISMATCH"):
        manifest_path = tmp_path / "v2/snapshot_10k.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")); manifest["quality"]["row_flags"]["TRUNCATED_INVOICE_RANGE"] = [99]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        verify_snapshot(tmp_path / "v2", source, result["manifest_digest"], sha256(source))


def test_v1_manifest_remains_strict_and_quality_is_unavailable(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx")
    result = prepare_snapshot(source, tmp_path / "v1", expected_sha=sha256(source), selection=(SHEETS[1], 2, 4))
    manifest = json.loads((tmp_path / "v1/snapshot_10k.json").read_text(encoding="utf-8"))
    assert "quality" not in manifest
    assert manifest["selection_version"] == "source-position-v1"
    db = tmp_path / "db.sqlite3"; initialize_database(db); upgrade_database(db)
    imported = import_snapshot(db, tmp_path / "v1", source, result["manifest_digest"], expected_sha=sha256(source))
    assert imported["quality"]["status"] == "UNAVAILABLE"


def test_v2_import_adds_flag_without_changing_classification(tmp_path):
    row = ["INV", "SKU", "synthetic", "1", "40500", "2", "123", "UK"]
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: row, 3: row, 4: ["INV", "SKU", "cancel", "-1", "40501", "2", "123", "UK"]})
    result = prepare_snapshot(source, tmp_path / "v2", expected_sha=sha256(source), selection=(SHEETS[1], 2, 3), quality_version=QUALITY_VERSION)
    db = tmp_path / "db.sqlite3"; initialize_database(db); upgrade_database(db)
    imported = import_snapshot(db, tmp_path / "v2", source, result["manifest_digest"], expected_sha=sha256(source))
    with database_transaction(db, readonly=True) as c:
        flags = [json.loads(row[0]) for row in c.execute("SELECT flags_json FROM import_rows ORDER BY source_row")]
        classes = [row[0] for row in c.execute("SELECT class FROM import_rows ORDER BY source_row")]
    assert classes == ["SALE", "SALE"]
    assert all("TRUNCATED_INVOICE_RANGE" in item for item in flags)
