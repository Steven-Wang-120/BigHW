import csv
import json

import pytest

from 测试.synthetic_xlsx import synthetic_workbook, rewrite_workbook
from 主程序.repositories.xlsx_reader import InputError, SHEETS, WorkbookReader, NS
from 主程序.services.snapshot_service import prepare_snapshot, sha256


def test_tc03_sparse_rows_format_tail_and_repeatable_hash(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx")
    sha = sha256(source)
    a = prepare_snapshot(source, tmp_path / "a", expected_sha=sha, selection=(SHEETS[1], 2, 4))
    b = prepare_snapshot(source, tmp_path / "b", expected_sha=sha, selection=(SHEETS[1], 2, 4))
    assert a["original_total"] == 6
    assert a["csv_sha256"] == b["csv_sha256"]
    assert a["manifest_digest"] == b["manifest_digest"]
    assert a["sheets"][SHEETS[0]]["last_value_row"] == 4
    assert a["sheets"][SHEETS[0]]["aggregate"]["empty_positions"] == 1
    with (tmp_path / "a/retail_2010_rows_2_10001.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["source_row"] for r in rows] == ["2", "3", "4"]
    assert rows[0]["price"] == "2.5499999999999998"
    assert rows[1]["invoice"] == ""
    before = (tmp_path / "a/snapshot_10k.json").read_bytes()
    with pytest.raises(InputError, match="OUTPUT_ALREADY_EXISTS"):
        prepare_snapshot(source, tmp_path / "a", expected_sha=sha, selection=(SHEETS[1], 2, 4))
    assert (tmp_path / "a/snapshot_10k.json").read_bytes() == before


def test_tc52_formula_without_cache_counts_as_value_and_preserves_metadata(tmp_path):
    rows = {2: ["S1", "SKU", "test", ("1+1", None), "40500", "2", None, "UK"]}
    source = synthetic_workbook(tmp_path / "formula.xlsx", rows, epoch="1904")
    result = prepare_snapshot(source, tmp_path / "out", expected_sha=sha256(source), selection=(SHEETS[1], 2, 2))
    assert result["original_total"] == 2
    assert result["sheets"][SHEETS[1]]["aggregate"]["reasons"] == {"FORMULA_UNSUPPORTED": 1}
    with WorkbookReader(source) as book:
        row = list(book.rows(SHEETS[1]))[1]
        assert book.epoch == "1904"
        assert row.cells["quantity"]["formula"] == "1+1"
        assert row.types["quantity"] == "f"
        assert row.values["quantity"] is None


def test_tc02_header_mismatch_does_not_publish(tmp_path):
    source = synthetic_workbook(tmp_path / "bad.xlsx", bad_header=True)
    with pytest.raises(InputError, match="HEADER_MISMATCH"):
        prepare_snapshot(source, tmp_path / "out", expected_sha=sha256(source), selection=(SHEETS[1], 2, 4))
    assert not (tmp_path / "out").exists()


def test_tc53_source_hash_mismatch_never_scans(tmp_path):
    source = synthetic_workbook(tmp_path / "synthetic.xlsx")
    with pytest.raises(InputError, match="SOURCE_HASH_MISMATCH"):
        prepare_snapshot(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_tc52_formula_only_no_cache_not_empty_row(tmp_path):
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: [None, None, None, ("1+1", None)]})
    result = prepare_snapshot(source, tmp_path / "out", expected_sha=sha256(source), selection=(SHEETS[1], 2, 2))
    aggregate = result["sheets"][SHEETS[1]]["aggregate"]
    assert aggregate["empty_positions"] == 0
    assert aggregate["reasons"] == {"FORMULA_UNSUPPORTED": 1}


def test_tc54_boundary_invoice_risk(tmp_path):
    row = ["S1", "SKU", "synthetic", "1", "40500", "2", "123", "UK"]
    source = synthetic_workbook(tmp_path / "source.xlsx", {2: row, 3: row, 4: row})
    result = prepare_snapshot(source, tmp_path / "out", expected_sha=sha256(source), selection=(SHEETS[1], 2, 3))
    assert result["truncation"]["boundary_invoice_outside_rows"] == 1
    assert result["truncation"]["warning"] is True


@pytest.mark.parametrize("case", ["missing_row1", "empty_header", "missing_header_cell", "no_rows"])
def test_tc02_missing_header_or_row1_rejected(tmp_path, case):
    source = synthetic_workbook(tmp_path / "missing-header.xlsx")
    def mutate(root):
        data = root.find(NS + "sheetData")
        header = data.find(NS + "row")
        if case == "missing_row1":
            data.remove(header)
        elif case == "empty_header":
            for cell in list(header):
                header.remove(cell)
        elif case == "missing_header_cell":
            header.remove(list(header)[5])
        else:
            data.clear()
    rewrite_workbook(source, "xl/worksheets/sheet1.xml", mutate)
    with pytest.raises(InputError, match="^HEADER_MISMATCH$"):
        prepare_snapshot(source, tmp_path / "out", expected_sha=sha256(source), selection=(SHEETS[1], 2, 4))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("case", ["missing_sheets", "empty_sheets", "duplicate_sheet_name", "duplicate_sheet_id",
                                 "missing_cellXfs", "empty_cellXfs"])
def test_tc02_workbook_structure_errors_controlled(tmp_path, case):
    source = synthetic_workbook(tmp_path / "bad-structure.xlsx")
    def mutate(root):
        sheets = root.find(NS + "sheets")
        if case == "missing_sheets":
            root.remove(sheets)
        elif case == "empty_sheets":
            sheets.clear()
        elif case == "duplicate_sheet_name":
            sheets[1].set("name", sheets[0].attrib["name"])
        elif case == "duplicate_sheet_id":
            sheets[1].set("sheetId", sheets[0].attrib["sheetId"])
        elif case == "empty_cellXfs":
            import xml.etree.ElementTree as ET
            ET.SubElement(root, NS + "cellXfs")
    member = "xl/styles.xml" if "cellXfs" in case else "xl/workbook.xml"
    rewrite_workbook(source, member, mutate)
    with pytest.raises(InputError, match="^WORKBOOK_STRUCTURE_MISMATCH$"):
        with WorkbookReader(source):
            pass
