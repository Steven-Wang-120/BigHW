"""Read-only streaming OOXML; never derive row coverage from dimension."""

from dataclasses import dataclass
from pathlib import Path
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from 主程序.domain.models import RawRow, SOURCE_FIELDS, SourceRowRef

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
HEADERS = ("Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country")
SHEETS = ("Year 2009-2010", "Year 2010-2011")


class InputError(ValueError):
    def __init__(self, code: str, status: int = 422, **details):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details


@dataclass(frozen=True)
class XMLRow:
    number: int
    values: dict
    types: dict
    cells: dict
    has_formula: bool

    @property
    def has_value(self):
        return self.has_formula or any(v is not None and str(v).strip() for v in self.values.values())

    def raw(self, sha: str, sheet: str, epoch: str) -> RawRow:
        return RawRow(SourceRowRef(sha, sheet, self.number), self.values, self.has_formula, self.types, epoch)


class WorkbookReader:
    def __init__(self, path: Path):
        self.path = Path(path)

    def __enter__(self):
        try:
            self.zip = zipfile.ZipFile(self.path)
            if len(set(self.zip.namelist())) != len(self.zip.namelist()):
                raise InputError("CORRUPT_FILE", 400)
            workbook = ET.fromstring(self.zip.read("xl/workbook.xml"))
            relationships = ET.fromstring(self.zip.read("xl/_rels/workbook.xml.rels"))
            targets = {}
            for rel in relationships:
                if rel.attrib.get("TargetMode") == "External":
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
                target = rel.attrib["Target"]
                name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not name.startswith("xl/") or ".." in name.split("/"):
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
                rel_id = rel.attrib["Id"]
                if not rel_id or rel_id in targets:
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
                targets[rel_id] = name
            sheets = workbook.find(NS + "sheets")
            if workbook.tag != NS + "workbook" or sheets is None or not len(sheets):
                raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
            self.sheets = {}
            sheet_ids, sheet_targets = set(), set()
            for sheet in sheets:
                name = sheet.attrib.get("name")
                sheet_id = sheet.attrib.get("sheetId")
                target = targets.get(sheet.attrib.get(REL + "id"))
                if (sheet.tag != NS + "sheet" or not name or not sheet_id or name in self.sheets
                        or sheet_id in sheet_ids or target in sheet_targets or target not in self.zip.namelist()):
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
                self.sheets[name] = target
                sheet_ids.add(sheet_id)
                sheet_targets.add(target)
            props = workbook.find(NS + "workbookPr")
            self.epoch = "1904" if props is not None and props.attrib.get("date1904") in {"1", "true"} else "1900"
            self.strings = []
            if "xl/sharedStrings.xml" in self.zip.namelist():
                with self.zip.open("xl/sharedStrings.xml") as stream:
                    root = None
                    for event, elem in ET.iterparse(stream, events=("start", "end")):
                        if root is None:
                            root = elem
                        if event == "end" and elem.tag == NS + "si":
                            self.strings.append("".join(t.text or "" for t in elem.iter(NS + "t")))
                            root.clear()
            self.styles = []
            if "xl/styles.xml" in self.zip.namelist():
                styles = ET.fromstring(self.zip.read("xl/styles.xml"))
                cell_xfs = styles.find(NS + "cellXfs")
                if styles.tag != NS + "styleSheet" or cell_xfs is None or not len(cell_xfs):
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
                self.styles = [s.attrib.get("numFmtId", "0") for s in cell_xfs]
                if any(s.tag != NS + "xf" for s in cell_xfs) or any(not n.isdigit() for n in self.styles):
                    raise InputError("WORKBOOK_STRUCTURE_MISMATCH")
            return self
        except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OSError) as exc:
            if hasattr(self, "zip"):
                self.zip.close()
            if isinstance(exc, InputError):
                raise
            raise InputError("CORRUPT_FILE", 400) from exc

    def __exit__(self, *_):
        self.zip.close()

    def rows(self, sheet: str):
        previous = 0
        saw_header = False
        try:
            with self.zip.open(self.sheets[sheet]) as stream:
                data = None
                for event, elem in ET.iterparse(stream, events=("start", "end")):
                    if event == "start" and elem.tag == NS + "sheetData":
                        data = elem
                    if event != "end" or elem.tag != NS + "row":
                        continue
                    number = int(elem.attrib["r"])
                    if number <= previous or number > 1048576:
                        raise InputError("SOURCE_POSITION_INVALID")
                    previous = number
                    if not saw_header:
                        if number != 1:
                            raise InputError("HEADER_MISMATCH")
                        saw_header = True
                    values = dict.fromkeys(SOURCE_FIELDS)
                    types = {f: "absent" for f in SOURCE_FIELDS}
                    cells = {}
                    formula = False
                    last_col = -1
                    for c in elem.findall(NS + "c"):
                        ref = c.attrib.get("r", "")
                        match = re.fullmatch(r"([A-H])([1-9][0-9]*)", ref)
                        if not match or int(match[2]) != number:
                            raise InputError("SOURCE_POSITION_INVALID")
                        col = ord(match[1]) - ord("A")
                        if col <= last_col:
                            raise InputError("SOURCE_POSITION_INVALID")
                        last_col = col
                        name = SOURCE_FIELDS[col]
                        kind = c.attrib.get("t", "n")
                        v = c.find(NS + "v")
                        xml_value = v.text if v is not None else None
                        f = c.find(NS + "f")
                        value = xml_value
                        if kind == "s" and xml_value is not None:
                            index = int(xml_value)
                            if index < 0 or index >= len(self.strings):
                                raise InputError("CORRUPT_FILE", 400)
                            value = self.strings[index]
                        elif kind == "inlineStr":
                            value = "".join(t.text or "" for t in c.iter(NS + "t"))
                        if kind in {"n", "b", "e"} and v is not None and value == "":
                            value = None
                        if kind == "e" and value is None:
                            value = "#ERROR"
                        style = c.attrib.get("s", "0")
                        if self.styles and not 0 <= int(style) < len(self.styles):
                            raise InputError("CORRUPT_FILE", 400)
                        cells[name] = {"ref": ref, "type": kind, "xml_value": xml_value,
                                       "style": style, "numFmtId": self.styles[int(style)] if self.styles else "0",
                                       "formula": f.text or "" if f is not None else None,
                                       "formula_attributes": dict(f.attrib) if f is not None else {}}
                        values[name], types[name] = value, "f" if f is not None else kind
                        formula = formula or f is not None
                    row = XMLRow(number, values, types, cells, formula)
                    if number == 1 and tuple(values.values()) != HEADERS:
                        raise InputError("HEADER_MISMATCH")
                    if number != 1 and previous == number and data is None:
                        raise InputError("CORRUPT_FILE", 400)
                    yield row
                    if data is not None:
                        data.clear()
            if not saw_header:
                raise InputError("HEADER_MISMATCH")
        except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError) as exc:
            if isinstance(exc, InputError):
                raise
            raise InputError("CORRUPT_FILE", 400) from exc


def blank_row(number: int) -> XMLRow:
    return XMLRow(number, dict.fromkeys(SOURCE_FIELDS), {f: "absent" for f in SOURCE_FIELDS}, {}, False)
