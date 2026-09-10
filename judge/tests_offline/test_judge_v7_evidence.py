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
Tier 2 (2026-09-10, schema 3 / caches _v5):
  - spill / array ranges tagged on anchor and children; a bare `=` child
    never renders as an empty formula
  - `[Red]` / `[$$-409]` number formats rendered; conditions still defer
  - theme-palette colours resolved (utils/theme_palette.py) into the same
    textcolor/bgcolor tokens; default text slot stays untokenised
  - blue-family colour names; outputs outside the band unchanged
  - styled empty cells in the used range counted per sheet
  - hidden defined names folded into the footnote
  - active cell per sheet from the active pane's selection

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
from openpyxl.styles.colors import Color  # noqa: E402
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula  # noqa: E402
from openpyxl.worksheet.pagebreak import Break  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402

from utils import theme_palette as tp  # noqa: E402
from utils import workbook_properties as wp  # noqa: E402
from utils.excel_utils import (  # noqa: E402
    _render_number_format,
    _rgb_to_color_name,
    extract_all_cell_data,
)

ACCT = '_(* #,##0_);_(* \\(#,##0\\);_(* "-"??_);_(@_)'
RED = "#,##0;[Red](#,##0);-"


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
    # --- tier 2 fixtures ---
    ws["G1"] = 0; ws["G1"].number_format = RED
    ws["G2"] = -1234.5; ws["G2"].number_format = RED
    ws["G3"] = 1234.5; ws["G3"].number_format = RED
    ws["G4"] = 1234.5; ws["G4"].number_format = "[$$-409]#,##0.00"
    ws["G5"] = 150; ws["G5"].number_format = '[>100]"big";"small"'
    ws["H1"] = "input"; ws["H1"].font = Font(color=Color(theme=4))            # accent1 -> 4472C4
    ws["H2"] = "band"; ws["H2"].fill = PatternFill("solid", fgColor=Color(theme=4, tint=0.6))
    ws["H3"] = "plain"; ws["H3"].font = Font(color=Color(theme=1))            # default text slot
    ws["H4"].fill = PatternFill("solid", fgColor=Color(theme=4, tint=0.8))    # theme fill, no value
    ws["H5"].font = Font(color=Color(theme=4))                                # theme font, no value
    ws["J1"] = ArrayFormula("J1:J3", "=SEQUENCE(3)")
    ws["J2"] = 2
    ws["J3"] = "="                                                            # <f ca="1"/> child
    ws["K7"].border = Border(left=Side(style="thin"))                         # styled empty OUTSIDE the used range (no value past J)
    ws["L1"] = "2024"; ws["L2"] = 2024; ws["L3"] = "1,234.50"; ws["L4"] = "(1,234)"
    ws["L5"] = "45%"; ws["L6"] = "Q1 2024"; ws["L7"] = "1.0"; ws["L8"] = '=TEXT(L2,"0")'
    ws["L9"] = "$1,000"; ws["L10"] = " 12 "; ws["L11"] = "2024-01-01"; ws["L12"] = "007"
    ws["L13"] = " "                                                            # whitespace-only text: blank, NOT hidden by format
    ws.freeze_panes = "B2"
    ws.sheet_view.pane.activePane = "bottomRight"
    for sel in ws.sheet_view.selection:
        if sel.pane == "bottomRight":
            sel.activeCell = "F5"; sel.sqref = "F5"
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
    assert "HIDDEN BY FORMAT" not in _cells(p)["full"].get("L13", ""), "a whitespace-only cell is not hidden by format"
    grid = list(csv.reader(io.StringIO(extract_all_cell_data(
        openpyxl.load_workbook(p)["Contents"], openpyxl.load_workbook(p, data_only=True, read_only=True)["Contents"])["full"])))
    assert grid[12][11] == " ", "whitespace-only text keeps the pre-v7 encoding (served as itself, no ref, no tag)"
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
    assert props["schema"] == 3
    text = wp.render_properties_text(props, origin={"original_filename": "MyModel.xlsm"})
    assert "original filename: MyModel.xlsm" in text
    assert "VBA: no" in text
    assert "secret" not in text, "hidden names leave the listed set (tier 2 item 6)"
    assert "tax_rate -> Contents!$A$2" in text
    assert "[+1 hidden names not listed]" in text
    assert "hyperlinks: A8 -> #'Assumptions'!A1" in text
    assert "page breaks: rows 20; cols none" in text
    assert "hidden rows: 3 (grouped); hidden cols: E" in text
    assert "grouped rows: 3-4=L1; grouped cols: D=L2" in text
    assert 'C15 (cellIs equal "MODEL OK" -> fill rgb:00C6EFCE font rgb:00006100 bold)' in text
    # tier 2: active cell from the active pane, styled empties, spill count
    contents = text.split("  1. Contents")[1].split("  2. Assumptions")[0]
    assert "active cell: F5" in contents
    styled = contents.split("styled empty cells in used range:")[1].splitlines()[0]
    # border (B2), bold (B3), painted theme fill (H4), border (K7 — inside the
    # used range because column L holds values). H5 (theme font only) is not
    # visible formatting and is not counted.
    assert styled.strip() == "4 (e.g. B2, B3, H4, K7)", styled
    assert "H5" not in styled
    assert "1 spill/array ranges" in contents
    assumptions = text.split("  2. Assumptions")[1]
    assert "active cell: A1" in assumptions
    assert "styled empty cells in used range: none" in assumptions
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


# ---------------------------------------------------------------------------
# Tier 2 (2026-09-10)
# ---------------------------------------------------------------------------


def test_bracket_formats_render():
    assert _render_number_format(0, RED) == "-"
    assert _render_number_format(-1234.5, RED) == "(1,235)"
    assert _render_number_format(1234.5, RED) == "1,235"
    assert _render_number_format(1234.5, "[$$-409]#,##0.00") == "$1,234.50"
    assert _render_number_format(1234.5, "[$€-x-euro2] #,##0.00") == "€ 1,234.50"
    assert _render_number_format(1234.5, "[Color 10]#,##0.0") == "1,234.5"
    assert _render_number_format(-5, '#,##0.00\\ "€";[Red]\\-#,##0.00\\ "€"') == "-5.00 €"
    assert _render_number_format(150, '[>100]"big";"small"') is None, "real conditions still defer"
    assert _render_number_format(0.5, "[h]:mm") is None, "elapsed-time codes still defer"
    assert _render_number_format(0.5, "[h]") is None


def test_theme_palette_math():
    assert tp.DEFAULT_PALETTE[4] == "4472C4" and tp.DEFAULT_PALETTE[1] == "000000"
    assert tp.apply_tint("4472C4", 0.4) == "8FAADC"
    assert tp.apply_tint("4472C4", 0.6) == "B4C7E7"
    assert tp.apply_tint("4472C4", -0.25) == "2F5597"
    assert tp.apply_tint("FFFFFF", -0.1499984740745262) == "D9D9D9"
    xml = (
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements>'
        '<a:clrScheme name="x"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="112233"/></a:dk2>'
        '<a:lt2><a:srgbClr val="EEEEEE"/></a:lt2><a:accent1><a:srgbClr val="123456"/></a:accent1>'
        '</a:clrScheme></a:themeElements></a:theme>'
    )
    pal = tp.parse_theme(xml)
    assert pal[0] == "FFFFFF" and pal[1] == "000000", "index 0 is lt1, index 1 is dk1 (swapped pairs)"
    assert pal[3] == "112233" and pal[4] == "123456"
    assert pal[5] == tp.DEFAULT_PALETTE[5], "missing slots fall back to the Office default"
    assert tp.parse_theme(b"<junk") == tp.DEFAULT_PALETTE, "a broken theme part never raises"
    assert tp.resolve(pal, 99) is None and tp.resolve(pal, "x") is None


def test_blue_family_names():
    assert _rgb_to_color_name("4472C4").startswith("blue ")
    assert _rgb_to_color_name("8FAADC").startswith("light_blue ")
    assert _rgb_to_color_name("DDEBF7").startswith("light_blue ")
    assert _rgb_to_color_name("1F3864").startswith("dark_blue ")
    for hx in ("4472C4", "8FAADC", "DDEBF7", "B4C7E7", "2F5597", "44546A"):
        assert "blue" in _rgb_to_color_name(hx), hx
    # outside the blue band: byte-identical to the pre-tier-2 names
    assert _rgb_to_color_name("FFFFE699") == "pale_yellow (FFE699)"
    assert _rgb_to_color_name("FF0000") == "bright_red (FF0000)"
    assert _rgb_to_color_name("00B0F0") == "bright_cyan (00B0F0)"
    assert _rgb_to_color_name("C6EFCE") == "olive (C6EFCE)"
    # light yellows at the 60-degree boundary (canary 887, check 47)
    assert _rgb_to_color_name("FFFFCC") == "light_yellow (FFFFCC)"
    assert _rgb_to_color_name("FFFFFFCC") == "light_yellow (FFFFCC)"
    assert _rgb_to_color_name("FFFFE0") == "light_yellow (FFFFE0)"
    assert _rgb_to_color_name("999966") == "olive (999966)", "a true muted olive keeps its name"
    assert _rgb_to_color_name("FFF2CC") == "beige (FFF2CC)", "Office gold 80% tint unchanged"
    assert _rgb_to_color_name("FFFF00") == "bright_yellow (FFFF00)"
    assert _rgb_to_color_name("A5A5A5") == "gray (A5A5A5)"


def test_theme_colours_and_spills_in_extractor(tmp_path):
    p = _build(tmp_path)
    wb1 = openpyxl.load_workbook(p)
    wb2 = openpyxl.load_workbook(p, data_only=True, read_only=True)
    pal = tp.load_palette(wb1)
    res = extract_all_cell_data(wb1["Contents"], wb2["Contents"], palette=pal)
    full = {}
    for row in csv.reader(io.StringIO(res["full"])):
        for c in row:
            if c.startswith("["):
                full[c[1:c.index("]")]] = c
    disp = {k: v.split("|")[0] for k, v in full.items()}
    # item 2 through the extractor
    assert disp["G1"] == "[G1]-" and disp["G2"] == "[G2](1,235)" and disp["G3"] == "[G3]1,235"
    assert disp["G4"] == "[G4]$1,234.50"
    assert disp["G5"] == "[G5]150", "a real condition defers to the legacy form (unchanged behaviour)"
    # item 3: theme colours tokenised like RGB ones. openpyxl writes its own
    # (Office 2007) theme part on save, so expectations come from the palette
    # the file actually carries, not from the Office 2013 defaults.
    accent1 = pal[4]
    assert f"textcolor:{_rgb_to_color_name(accent1)}" in full["H1"]
    assert "blue" in _rgb_to_color_name(accent1)
    assert f"bgcolor:{_rgb_to_color_name(tp.apply_tint(accent1, 0.6))}" in full["H2"]
    assert "textcolor" not in full["H3"], "default text slot (theme 1, no tint) stays untokenised"
    # A value-less styled cell has always been emitted as a ref-less
    # `|FORMAT:...` field (pre-existing encoding), so locate H4/H5 by position.
    grid = list(csv.reader(io.StringIO(res["full"])))
    h4, h5 = grid[3][7], grid[4][7]
    assert h4.startswith("|FORMAT:") and f"bgcolor:{_rgb_to_color_name(tp.apply_tint(accent1, 0.8))}" in h4, \
        "a painted theme fill on an empty cell is served"
    assert h5 == "", "a theme font on an empty cell is not"
    # item 1: spill anchor + children
    assert full["J1"].startswith("[J1]|FORMULA:=SEQUENCE(3)") or "[SPILL J1:J3]|FORMULA:=SEQUENCE(3)" in full["J1"]
    assert "[SPILL J1:J3]" in full["J1"]
    assert disp["J2"] == "[J2]2 [SPILLED FROM J1]"
    assert full["J3"].startswith("[J3]") and "SPILLED FROM J1" in full["J3"] and "FORMULA:=" not in full["J3"], \
        "a bare '=' child renders as the tag only, never as an empty formula"
    # data view keeps the tags (they sit in the display part) but not the style tokens
    data = {}
    for row in csv.reader(io.StringIO(res["data"])):
        for c in row:
            if c.startswith("["):
                data[c[1:c.index("]")]] = c
    assert "[SPILLED FROM J1]" in data["J2"] and "textcolor" not in data["H1"]
    # without a palette the extractor behaves exactly as before (tests that build bare cells)
    res0 = extract_all_cell_data(wb1["Contents"], wb2["Contents"])
    assert "textcolor:blue" not in res0["full"] and "[SPILL J1:J3]" in res0["full"]


def test_properties_theme_hex_and_determinism(tmp_path):
    p = _build(tmp_path)
    wb = openpyxl.load_workbook(p)
    wb["Assumptions"].sheet_properties.tabColor = Color(theme=4, tint=0.4)
    q = tmp_path / "_tab.xlsx"
    wb.save(q)
    wbq = openpyxl.load_workbook(q)
    props = wp.extract_workbook_properties(wbq, q)
    text = wp.render_properties_text(props)
    expected_hex = tp.apply_tint(tp.load_palette(wbq)[4], 0.4)
    assert f"tab color: theme:4 tint +0.40 ({expected_hex})" in text
    assert text == wp.render_properties_text(wp.extract_workbook_properties(openpyxl.load_workbook(q), q))


def test_numbers_stored_as_text_marker(tmp_path):
    p = _build(tmp_path)
    full = _cells(p)["full"]
    disp = {k: v.split("|")[0] for k, v in full.items()}
    assert disp["L1"] == "[L1]2024 [TEXT]"
    assert disp["L2"] == "[L2]2024", "a real number is never marked"
    assert disp["L3"] == "[L3]1,234.50 [TEXT]"
    assert disp["L4"] == "[L4](1,234) [TEXT]"
    assert disp["L5"] == "[L5]45% [TEXT]"
    assert disp["L9"] == "[L9]$1,000 [TEXT]"
    assert disp["L10"] == "[L10] 12  [TEXT]"
    assert disp["L12"] == "[L12]007 [TEXT]", "leading-zero codes are text that looks numeric — marked, judge decides"
    assert disp["L7"] == "[L7]1.0 [TEXT]", "version labels are marked too; the marker is a fact, not a verdict"
    assert disp["L6"] == "[L6]Q1 2024", "labels with letters are not marked"
    assert disp["L11"] == "[L11]2024-01-01", "date-like text is not marked"
    assert "[TEXT]" not in full["L8"], "a formula returning text is not marked"
    assert _cells(p)["data"]["L1"] == "[L1]2024 [TEXT]", "marker survives the data view"
