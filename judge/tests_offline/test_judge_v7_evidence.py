"""Offline: judge v7 evidence fixes (2026-09-09) — the acceptance probe from
the v7 handoff turned into assertions, plus the cases found in review.

Covers the cell extractor (utils/excel_utils.py):
  - dates rendered like Excel (yyyy-mm-dd, mmm-yy, d mmmm yyyy, AM/PM, locale prefix)
  - accounting padding (_x / *x / ?) incl. the zero section reading `-`, not `-0`
  - blank-format hiding for numbers AND text -> [HIDDEN BY FORMAT]
  - a formula cell keeps its [ref] even with an empty display
  - what-if data tables tagged on every member cell
  - 'wrap' restored in the formatting view
and the properties block (utils/workbook_properties.py):
  - cell hyperlinks, page breaks, grouping vs hidden, CF styling,
    hidden defined names, zip-based VBA test, original filename, schema 2.

Run from judge/:  uv run pytest tests_offline/test_judge_v7_evidence.py -q
"""
import csv
import datetime
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402
from openpyxl.formatting.rule import CellIsRule  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.worksheet.formula import DataTableFormula  # noqa: E402
from openpyxl.worksheet.pagebreak import Break  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402

from utils import workbook_properties as wp  # noqa: E402
from utils.excel_utils import (  # noqa: E402
    _render_number_format,
    extract_all_cell_data,
)

ACCT = '_(* #,##0_);_(* \\(#,##0\\);_(* "-"??_);_(@_)'


def _build(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contents"
    wb.create_sheet("Assumptions")
    ws["A1"] = datetime.datetime(2027, 1, 1); ws["A1"].number_format = "yyyy-mm-dd"
    ws["A2"] = datetime.datetime(2027, 1, 1); ws["A2"].number_format = "yyyy-mm-dd hh:mm:ss"
    ws["A3"] = datetime.datetime(2027, 1, 1); ws["A3"].number_format = "mmm-yy"
    ws["A4"] = 0; ws["A4"].number_format = ACCT
    ws["A5"] = -12345.678; ws["A5"].number_format = ACCT
    ws["A6"] = 100; ws["A6"].number_format = ";;;"
    ws["A7"] = "long label"; ws["A7"].alignment = Alignment(wrap_text=True)
    ws["A8"] = "Assumptions"; ws["A8"].hyperlink = "#'Assumptions'!A1"
    ws["A9"] = 12345.678; ws["A9"].number_format = ACCT
    ws["A10"] = datetime.datetime(2027, 1, 1); ws["A10"].number_format = "d mmmm yyyy"
    ws["A11"] = datetime.datetime(2027, 3, 5, 14, 7, 9); ws["A11"].number_format = "h:mm AM/PM"
    ws["A12"] = datetime.datetime(2027, 3, 5); ws["A12"].number_format = "mm-dd-yy"
    ws["A13"] = datetime.datetime(2027, 3, 5); ws["A13"].number_format = "mm\\/dd\\/yyyy"
    ws["A14"] = "hidden text"; ws["A14"].number_format = ";;;"
    ws["A15"] = '=IF(1=1,"",5)'
    ws["A16"] = datetime.datetime(2027, 3, 5); ws["A16"].number_format = "[$-409]mmmm d, yyyy;@"
    ws["A17"] = datetime.datetime(2027, 3, 5, 9, 30); ws["A17"].number_format = "hh:mm"
    ws["B2"].border = Border(top=Side(style="thin"))
    ws["B3"].font = Font(bold=True)
    ws["C15"] = "MODEL OK"
    ws.conditional_formatting.add(
        "C15",
        CellIsRule(operator="equal", formula=['"MODEL OK"'],
                   fill=PatternFill("solid", fgColor="C6EFCE", bgColor="C6EFCE"),
                   font=Font(color="006100", bold=True)),
    )
    ws["D20"] = DataTableFormula(ref="D20:F22", dt2D=True, r1="A1", r2="A2")
    ws["E21"] = 42
    ws.row_breaks.append(Break(id=20))
    ws.row_dimensions[3].outlineLevel = 1; ws.row_dimensions[3].hidden = True
    ws.row_dimensions[4].outlineLevel = 1
    ws.column_dimensions["D"].outlineLevel = 2
    ws.column_dimensions["E"].hidden = True
    wb.defined_names["secret"] = DefinedName("secret", attr_text="Contents!$A$1", hidden=True)
    wb.defined_names["tax_rate"] = DefinedName("tax_rate", attr_text="Contents!$A$2")
    p = tmp_path / "_probe.xlsx"
    wb.save(p)
    return p


def _cells(path):
    wb1 = openpyxl.load_workbook(path)
    wb2 = openpyxl.load_workbook(path, data_only=True, read_only=True)
    res = extract_all_cell_data(wb1["Contents"], wb2["Contents"])
    out = {}
    for view in ("full", "data"):
        for row in csv.reader(io.StringIO(res[view])):
            for c in row:
                if c.startswith("["):
                    out.setdefault(view, {})[c[1:c.index("]")]] = c
    return out


def test_cell_extractor(tmp_path):
    p = _build(tmp_path)
    full = _cells(p)["full"]
    disp = {k: v.split("|")[0] for k, v in full.items()}
    assert disp["A1"] == "[A1]2027-01-01"
    assert disp["A2"] == "[A2]2027-01-01 00:00:00"
    assert disp["A3"] == "[A3]Jan-27"
    assert disp["A4"] == "[A4]-", "accounting zero reads as a dash, not -0"
    assert disp["A5"] == "[A5](12,346)"
    assert disp["A9"] == "[A9]12,346"
    assert disp["A6"] == "[A6]100 [FORMAT:;;;] [HIDDEN BY FORMAT]"
    assert disp["A14"] == "[A14]hidden text [FORMAT:;;;] [HIDDEN BY FORMAT]"
    assert disp["A10"] == "[A10]1 January 2027"
    assert disp["A11"] == "[A11]2:07 PM"
    assert disp["A12"] == "[A12]03-05-27"
    assert disp["A13"] == "[A13]03/05/2027"
    assert disp["A16"] == "[A16]March 5, 2027"
    assert disp["A17"] == "[A17]09:30"
    assert full["A7"].endswith(";wrap"), "wrap restored in the formatting view"
    assert full["A15"].startswith('[A15]|FORMULA:=IF(1=1,"",5)'), "formula cell keeps its ref with an empty display"
    assert "[DATA TABLE D20:F22" in full["D20"] and "FORMULA:{=TABLE(A1,A2)}" in full["D20"]
    assert full["E21"].startswith("[E21]42 [DATA TABLE D20:F22")
    data = _cells(p)["data"]
    assert data["A6"] == "[A6]100 [FORMAT:;;;] [HIDDEN BY FORMAT]", "marker survives the data view"
    assert "FORMAT:font" not in data["A7"]


def test_accounting_renderer_direct():
    assert _render_number_format(0, ACCT) == "-"
    assert _render_number_format(-12345.678, ACCT) == "(12,346)"
    assert _render_number_format(12345.678, ACCT) == "12,346"
    assert _render_number_format(1234.5, "#,##0.00") == "1,234.50", "existing formats unchanged"
    assert _render_number_format(0, "#,##0") == "0", "a forced digit still prints zero"


def test_properties_block(tmp_path):
    p = _build(tmp_path)
    wb = openpyxl.load_workbook(p)
    props = wp.extract_workbook_properties(wb, p)
    assert props["schema"] == 2
    text = wp.render_properties_text(props, origin={"original_filename": "MyModel.xlsm"})
    assert "original filename: MyModel.xlsm" in text
    assert "VBA: no" in text
    assert "secret (hidden) -> Contents!$A$1" in text
    assert "tax_rate -> Contents!$A$2" in text
    assert "hyperlinks: A8 -> #'Assumptions'!A1" in text
    assert "page breaks: rows 20; cols none" in text
    assert "hidden rows: 3 (grouped); hidden cols: E" in text
    assert "grouped rows: 3-4=L1; grouped cols: D=L2" in text
    assert 'C15 (cellIs equal "MODEL OK" -> fill rgb:00C6EFCE font rgb:00006100 bold)' in text
    # determinism: two extractions render identically
    assert text == wp.render_properties_text(wp.extract_workbook_properties(openpyxl.load_workbook(p), p),
                                             origin={"original_filename": "MyModel.xlsm"})


def test_vba_detection_via_zip(tmp_path):
    import zipfile
    p = _build(tmp_path)
    assert wp._zip_has_vba(p) is False
    q = tmp_path / "macro.xlsx"
    with zipfile.ZipFile(p) as src, zipfile.ZipFile(q, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/vbaProject.bin", b"\x00")
    assert wp._zip_has_vba(q) is True


def test_origin_sidecar_roundtrip(tmp_path):
    assert wp.load_origin(tmp_path) is None
    (tmp_path / wp.ORIGIN_FILENAME).write_text('{"original_filename": "x.xlsx"}', encoding="utf-8")
    assert wp.load_origin(tmp_path) == {"original_filename": "x.xlsx"}
