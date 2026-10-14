"""Synthetic OOXML builders: never sampled from official transaction rows."""

from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

from 主程序.repositories.xlsx_reader import NS, SHEETS, HEADERS


def synthetic_workbook(path: Path, rows=None, *, tail=10, epoch="1900", bad_header=False):
    rows = rows or {2: ["S1", "SKU-A", "synthetic", "2", "40500", "2.5499999999999998", "12345", "UK"],
                    4: ["C1", "POST", "synthetic fee", "-1", "40501", "2", "12345", "UK"]}
    workbook = ET.Element(NS + "workbook")
    ET.SubElement(workbook, NS + "workbookPr", date1904="1" if epoch == "1904" else "0")
    sheets = ET.SubElement(workbook, NS + "sheets")
    rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    with zipfile.ZipFile(path, "w") as z:
        for i, name in enumerate(SHEETS, 1):
            ET.SubElement(sheets, NS + "sheet", name=name, sheetId=str(i), attrib={"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": f"rId{i}"})
            ET.SubElement(rels, "Relationship", Id=f"rId{i}", Target=f"worksheets/sheet{i}.xml")
            root = ET.Element(NS + "worksheet")
            ET.SubElement(root, NS + "dimension", ref="A1:H999999")
            data = ET.SubElement(root, NS + "sheetData")
            header = ET.SubElement(data, NS + "row", r="1")
            for col, text in enumerate(HEADERS):
                c = ET.SubElement(header, NS + "c", r=f"{chr(65+col)}1", t="inlineStr")
                ET.SubElement(ET.SubElement(c, NS + "is"), NS + "t").text = "WRONG" if bad_header and col == 5 else text
            for n, values in sorted(rows.items()):
                row = ET.SubElement(data, NS + "row", r=str(n))
                for col, val in enumerate(values):
                    if val is None:
                        continue
                    kind = "n" if col in (3, 4, 5, 6) else "inlineStr"
                    c = ET.SubElement(row, NS + "c", r=f"{chr(65+col)}{n}", t=kind)
                    if isinstance(val, tuple):
                        ET.SubElement(c, NS + "f").text = val[0]
                        ET.SubElement(c, NS + "v").text = val[1]
                    elif kind == "n":
                        ET.SubElement(c, NS + "v").text = val
                    else:
                        ET.SubElement(ET.SubElement(c, NS + "is"), NS + "t").text = val
            row = ET.SubElement(data, NS + "row", r=str(tail))
            ET.SubElement(row, NS + "c", r=f"A{tail}", s="0")
            z.writestr(f"xl/worksheets/sheet{i}.xml", ET.tostring(root))
        z.writestr("xl/workbook.xml", ET.tostring(workbook))
        z.writestr("xl/_rels/workbook.xml.rels", ET.tostring(rels))
    return path


def rewrite_workbook(path, member, transform):
    """Mutate only a test-owned synthetic ZIP, never official raw files."""
    with zipfile.ZipFile(path) as z:
        members = {name: z.read(name) for name in z.namelist()}
    root = ET.fromstring(members[member]) if member in members else ET.Element(NS + "styleSheet")
    transform(root)
    members[member] = ET.tostring(root)
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path
