"""Unit tests for checks 93 (No hidden rows/columns) and 73 (Reasonable row heights) on
synthetic micro-workbooks (raw SpreadsheetML zips written here).

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_rowscols

Plain asserts; also collectable by pytest.  Temporary files go to detchecks/scratch/rowscols/.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from xml.sax.saxutils import escape, quoteattr

from detchecks.api import Engine
from detchecks.checks import c73 as M73
from detchecks.checks import c93 as M93
from detchecks.checks.c73 import C73, cell_need, col_px, face_width, mdw_px, text_em, wrap_lines
from detchecks.checks.c93 import C93, collapsed_members
from detchecks.errors import GradingError

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "rowscols")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WS = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

_TMP = None
_N = [0]


def tmp(name: str = "") -> str:
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_rowscols_", dir=SCRATCH)
    _N[0] += 1
    return os.path.join(_TMP, f"{_N[0]:03d}_{name or 'book'}.xlsx")


# ============================================================================ builder
class Styles:
    """styles.xml builder; Normal = Calibri 11 unless given."""

    def __init__(self, normal=("Calibri", 11)):
        self.fonts = [f'<font><sz val="{normal[1]}"/><name val="{normal[0]}"/></font>']
        self.xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']

    def xf(self, name="Calibri", size=11, bold=False, wrap=False, rotation=0, horizontal=None) -> int:
        self.fonts.append(f'<font>{"<b/>" if bold else ""}<sz val="{size}"/><name val="{name}"/></font>')
        fid = len(self.fonts) - 1
        al = []
        if wrap:
            al.append('wrapText="1"')
        if rotation:
            al.append(f'textRotation="{rotation}"')
        if horizontal:
            al.append(f'horizontal="{horizontal}"')
        alx = f'<alignment {" ".join(al)}/>' if al else ""
        self.xfs.append(f'<xf numFmtId="0" fontId="{fid}" fillId="0" borderId="0" xfId="0" applyFont="1" '
                        f'applyAlignment="1">{alx}</xf>')
        return len(self.xfs) - 1

    def xml(self) -> str:
        return (f'<styleSheet xmlns="{MAIN}"><fonts count="{len(self.fonts)}">{"".join(self.fonts)}</fonts>'
                '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf fontId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(self.xfs)}">{"".join(self.xfs)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def c(ref, v=None, s=0, f=None, t=None):
    """One <c>.  v: str (inline string), number, or formula cache (with f)."""
    sa = f' s="{s}"' if s else ""
    if f is not None:
        tt = t or ("str" if isinstance(v, str) else None)
        vv = "" if v is None else escape(str(v))
        return f'<c r="{ref}"{sa}{f" t=\"{tt}\"" if tt else ""}><f>{escape(f)}</f><v>{vv}</v></c>'
    if v is None:
        return f'<c r="{ref}"{sa}/>'
    if t == "s":
        return f'<c r="{ref}"{sa} t="s"><v>{v}</v></c>'
    if isinstance(v, str):
        return f'<c r="{ref}"{sa} t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
    return f'<c r="{ref}"{sa}><v>{v!r}</v></c>'


def ws(rows=None, *, cols="", fmt='<sheetFormatPr defaultRowHeight="15"/>', pr="", views="", tail="",
       no_r=False):
    """rows: {r: (attrs, [cells])}.  attrs is the raw attribute text of <row>."""
    body = []
    for r in sorted(rows or {}):
        attrs, cells = rows[r]
        ra = "" if no_r else f' r="{r}"'
        body.append(f"<row{ra}{(' ' + attrs) if attrs else ''}>{''.join(cells)}</row>")
    colx = f"<cols>{cols}</cols>" if cols else ""
    return f"{pr}{views}{fmt}{colx}<sheetData>{''.join(body)}</sheetData>{tail}"


def book(sheets, styles=None, *, states=None, sst=None, app=None, name=""):
    """sheets: [(name, inner xml)].  app: docProps/app.xml Application (writer label)."""
    styles = styles or Styles()
    states = states or {}
    path = tmp(name)
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    ov = [f'<Override PartName="/xl/workbook.xml" ContentType="{CT_XLSX}"/>']
    ov += [f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="{CT_WS}"/>' for i in range(len(sheets))]
    z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>' + "".join(ov) + "</Types>")
    rr = f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
    if app:
        rr += (f'<Relationship Id="rId2" Type="{REL}/extended-properties" Target="docProps/app.xml"/>')
        z.writestr("docProps/app.xml", f'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                                       f'<Application>{escape(app)}</Application></Properties>')
    z.writestr("_rels/.rels", f'<Relationships xmlns="{PKG_REL}">{rr}</Relationships>')
    wrels, sh = [], []
    for i, (nm, xml) in enumerate(sheets):
        st = f' state="{states[i]}"' if i in states else ""
        sh.append(f'<sheet name={quoteattr(nm)} sheetId="{i + 1}"{st} r:id="rId{i + 1}"/>')
        wrels.append(f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>')
        z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f'{DECL}<worksheet xmlns="{MAIN}" xmlns:r="{REL}">{xml}</worksheet>')
    wrels.append(f'<Relationship Id="rIdS" Type="{REL}/styles" Target="styles.xml"/>')
    z.writestr("xl/styles.xml", styles.xml())
    if sst is not None:
        wrels.append(f'<Relationship Id="rIdT" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">' + "".join(sst) + "</sst>")
    z.writestr("xl/workbook.xml", f'{DECL}<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>' + "".join(sh) + "</sheets></workbook>")
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG_REL}">' + "".join(wrels) + "</Relationships>")
    z.close()
    return path


def run(path, cls):
    return Engine(path, [cls()]).run()[cls.key]


def locs(v):
    return [m["location"] for m in v["mistakes"]]


def graded(kind, fn, *needles):
    """Patrick 2026-10-05 (every attempt graded): where the check used to raise, it decides by a default
    recorded in stats.defaults[kind]; needles that name a location ('S!A1') must appear in that record."""
    import json
    v = fn()
    d = (v["stats"].get("defaults") or {}).get(kind)
    assert d and d["count"] >= 1, (kind, v["stats"].get("defaults"))
    blob = json.dumps(d)
    for n in needles:
        if "!" in n:
            assert n.split("!")[-1] in blob, (n, d)
    return v


def raises(fn, *needles):
    try:
        fn()
    except GradingError as e:
        msg = str(e) + " " + " ".join(str(x) for x in e.failures.values())
        for n in needles:
            assert n in msg, (n, msg[:600])
        return e
    raise AssertionError(f"expected GradingError containing {needles}")


class patched:
    """Temporarily set module constants."""

    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def data_rows(lo, hi, attrs=None, text=True):
    """Rows lo..hi with one value each; attrs: {r: 'hidden="1"'}."""
    attrs = attrs or {}
    return {r: (attrs.get(r, ""), [c(f"A{r}", f"row {r}" if text else r)]) for r in range(lo, hi + 1)}


# ============================================================================ 93: pure function
def test_93_collapsed_members():
    lv = {5: 1, 6: 1, 7: 1}
    hid = {5, 6, 7}
    assert collapsed_members(lv, hid.__contains__, hid.__contains__) == {5, 6, 7}
    # partially hidden group: hiding, not grouping
    assert collapsed_members(lv, {6}.__contains__, {6}.__contains__) == set()
    # nested: outer 4..9 (level 1) expanded, inner 5..7 (level 2) collapsed
    lv2 = {4: 1, 5: 2, 6: 2, 7: 2, 8: 1, 9: 1}
    assert collapsed_members(lv2, hid.__contains__, hid.__contains__) == {5, 6, 7}
    # outer collapsed: everything hidden
    allh = set(lv2)
    assert collapsed_members(lv2, allh.__contains__, allh.__contains__) == allh
    # a level-1 row hidden next to an expanded inner group is not a collapse
    assert collapsed_members(lv2, {4}.__contains__, {4}.__contains__) == set()
    # zero-size member: run counts as invisible, but that member is not exempt
    assert collapsed_members(lv, hid.__contains__, {5, 7}.__contains__) == {5, 7}
    # require the collapsed mark: summary below (8) / above (4)
    assert collapsed_members(lv, hid.__contains__, hid.__contains__, set(), True, require_mark=True) == set()
    assert collapsed_members(lv, hid.__contains__, hid.__contains__, {8}, True, require_mark=True) == {5, 6, 7}
    assert collapsed_members(lv, hid.__contains__, hid.__contains__, {8}, False, require_mark=True) == set()
    assert collapsed_members(lv, hid.__contains__, hid.__contains__, {4}, False, require_mark=True) == {5, 6, 7}


# ============================================================================ 93: workbooks
def test_93_hidden_row_flag_and_zero_height():
    rows = data_rows(1, 10, {4: 'hidden="1"', 5: 'hidden="1"', 8: 'ht="0" customHeight="1"'})
    v = run(book([("Model", ws(rows))]), C93)
    assert v["decision"] == "fail"
    assert locs(v) == ["Model!4:5", "Model!8:8"], locs(v)
    assert "hidden=\"1\"" in v["mistakes"][0]["description"]
    assert "zero row height" in v["mistakes"][1]["description"]
    assert v["stats"]["hidden_rows"] == 2 and v["stats"]["zero_height_rows"] == 1
    # clean sheet passes
    assert run(book([("Model", ws(data_rows(1, 10)))]), C93)["decision"] == "pass"


def test_93_collapsed_group_passes_ungrouped_fails():
    # toy T3 Pass: rows 5:7 outlineLevel 1 hidden, summary row 8 collapsed
    g = data_rows(1, 10, {5: 'hidden="1" outlineLevel="1"', 6: 'hidden="1" outlineLevel="1"',
                          7: 'hidden="1" outlineLevel="1"', 8: 'collapsed="1"'})
    fmt = '<sheetFormatPr defaultRowHeight="15" outlineLevelRow="1"/>'
    v = run(book([("Model", ws(g, fmt=fmt))]), C93)
    assert v["decision"] == "pass", v
    assert v["stats"]["grouped_collapsed_rows_exempt"] == 3
    # toy T3 Fail: Ungroup leaves the rows hidden and the stray collapsed mark on row 8
    u = data_rows(1, 10, {5: 'hidden="1"', 6: 'hidden="1"', 7: 'hidden="1"', 8: 'collapsed="1"'})
    v = run(book([("Model", ws(u))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["Model!5:7"], locs(v)
    assert "collapsed mark on row 8" in v["mistakes"][0]["description"]
    # openpyxl-style group: hidden whole block, no collapsed mark -> still grouping (default)
    o = data_rows(1, 10, {5: 'hidden="1" outlineLevel="1"', 6: 'hidden="1" outlineLevel="1"',
                          7: 'hidden="1" outlineLevel="1"'})
    assert run(book([("Model", ws(o))]), C93)["decision"] == "pass"
    with patched(M93, REQUIRE_COLLAPSED_FLAG=True):
        v = run(book([("Model", ws(o))]), C93)
        assert v["decision"] == "fail" and locs(v) == ["Model!5:7"]
        assert "collapsed mark" in v["mistakes"][0]["description"]
        # Excel's own mark on the summary row below satisfies it
        assert run(book([("Model", ws(g, fmt=fmt))]), C93)["decision"] == "pass"
        # summaryBelow=0: the mark must be on the row ABOVE the group
        pr = '<sheetPr><outlinePr summaryBelow="0"/></sheetPr>'
        assert run(book([("Model", ws(g, fmt=fmt, pr=pr))]), C93)["decision"] == "fail"
        above = data_rows(1, 10, {4: 'collapsed="1"', 5: 'hidden="1" outlineLevel="1"',
                                  6: 'hidden="1" outlineLevel="1"', 7: 'hidden="1" outlineLevel="1"'})
        assert run(book([("Model", ws(above, fmt=fmt, pr=pr))]), C93)["decision"] == "pass"


def test_93_partial_and_nested_groups():
    # expanded group 5:7, only row 6 hidden -> hiding inside a group
    p = data_rows(1, 10, {5: 'outlineLevel="1"', 6: 'hidden="1" outlineLevel="1"', 7: 'outlineLevel="1"'})
    v = run(book([("S", ws(p))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!6:6"]
    assert "not collapsed as a whole" in v["mistakes"][0]["description"]
    # outer 4:9 expanded, inner 5:7 (level 2) collapsed -> pass
    n = data_rows(1, 12, {4: 'outlineLevel="1"', 5: 'hidden="1" outlineLevel="2"', 6: 'hidden="1" outlineLevel="2"',
                          7: 'hidden="1" outlineLevel="2"', 8: 'outlineLevel="1" collapsed="1"', 9: 'outlineLevel="1"'})
    assert run(book([("S", ws(n))]), C93)["decision"] == "pass"
    # 93-F1: a hidden="1" member with ht=0 inside a collapsed group is hidden inside a collapsed
    # group (handoff) -> exempt; with FLAGGED_ZERO_SIZE_EXEMPT off it fails, and says why (93-F2)
    z = data_rows(1, 10, {5: 'hidden="1" outlineLevel="1"', 6: 'hidden="1" outlineLevel="1" ht="0" customHeight="1"',
                          7: 'hidden="1" outlineLevel="1"', 8: 'collapsed="1"'})
    v = run(book([("S", ws(z))]), C93)
    assert v["decision"] == "pass", v["mistakes"]
    assert v["stats"]["grouped_collapsed_rows_exempt"] == 3
    with patched(M93, FLAGGED_ZERO_SIZE_EXEMPT=False):
        v = run(book([("S", ws(z))]), C93)
        assert v["decision"] == "fail" and locs(v) == ["S!6:6"], locs(v)
        assert v["stats"]["grouped_collapsed_rows_exempt"] == 2
        d = v["mistakes"][0]["description"]
        assert "zero height" in d and "collapsed outline group" in d and "not collapsed as a whole" not in d, d
    # a zero height WITHOUT the hidden flag inside a collapsed group is not what collapsing writes -> fails
    z2 = data_rows(1, 10, {5: 'hidden="1" outlineLevel="1"', 6: 'outlineLevel="1" ht="0" customHeight="1"',
                           7: 'hidden="1" outlineLevel="1"', 8: 'collapsed="1"'})
    v = run(book([("S", ws(z2))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!6:6"], locs(v)
    assert "zero row height" in v["mistakes"][0]["description"] and "inside an outline group" in v["mistakes"][0]["description"]
    assert v["stats"]["grouped_collapsed_rows_exempt"] == 2
    # one run of hidden rows, half grouped (partial group), half ungrouped: two mistakes, each with its own reason
    mix = data_rows(1, 12, {4: 'outlineLevel="1"', 5: 'hidden="1" outlineLevel="1"', 6: 'hidden="1" outlineLevel="1"',
                            7: 'hidden="1"', 8: 'hidden="1"'})
    v = run(book([("S", ws(mix))]), C93)
    assert locs(v) == ["S!5:6", "S!7:8"], locs(v)
    assert "not collapsed as a whole" in v["mistakes"][0]["description"]
    assert "outside any outline group" in v["mistakes"][1]["description"]
    # outline symbols switched off: still grouping by default; not with the option
    g = data_rows(1, 10, {5: 'hidden="1" outlineLevel="1"', 6: 'hidden="1" outlineLevel="1"', 8: 'collapsed="1"'})
    pr = '<sheetPr><outlinePr showOutlineSymbols="0"/></sheetPr>'
    assert run(book([("S", ws(g, pr=pr))]), C93)["decision"] == "pass"
    with patched(M93, GROUP_NEEDS_OUTLINE_SYMBOLS=True):
        v = run(book([("S", ws(g, pr=pr))]), C93)
        assert v["decision"] == "fail" and "outline symbols are switched off" in v["mistakes"][0]["description"]


def test_93_columns():
    rows = data_rows(1, 3)
    # toy T4: zero width without the hidden flag
    v = run(book([("S", ws(rows, cols='<col min="5" max="5" width="0" customWidth="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!E:E"]
    assert "zero column width" in v["mistakes"][0]["description"]
    # toy T1: hidden unused columns out to XFD (+ zero default width, every column covered)
    cols = ('<col min="1" max="71" width="9" customWidth="1"/><col min="72" max="129" width="12.8" hidden="1" customWidth="1"/>'
            '<col min="130" max="130" width="13" hidden="1"/><col min="131" max="16384" width="8.9" hidden="1"/>')
    v = run(book([("S", ws(rows, cols=cols, fmt='<sheetFormatPr defaultRowHeight="15" defaultColWidth="0"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!BT:XFD"], locs(v)
    assert "16,313 columns" in v["mistakes"][0]["description"] and "XFD" in v["mistakes"][0]["description"]
    # zero default width with every column covered by a visible <col>: nothing hidden
    cols2 = '<col min="1" max="16384" width="9" customWidth="1"/>'
    assert run(book([("S", ws(rows, cols=cols2, fmt='<sheetFormatPr defaultRowHeight="15" defaultColWidth="0"/>'))]),
               C93)["decision"] == "pass"
    # zero default width with uncovered columns: they are hidden
    v = run(book([("S", ws(rows, cols='<col min="1" max="3" width="9"/>',
                           fmt='<sheetFormatPr defaultRowHeight="15" defaultColWidth="0"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!D:XFD"], locs(v)
    # collapsed column group C:D (summary E collapsed) passes; summaryRight=0 + mark required fails
    cg = ('<col min="3" max="4" width="9" hidden="1" outlineLevel="1"/>'
          '<col min="5" max="5" width="9" collapsed="1"/>')
    assert run(book([("S", ws(rows, cols=cg))]), C93)["decision"] == "pass"
    with patched(M93, REQUIRE_COLLAPSED_FLAG=True):
        assert run(book([("S", ws(rows, cols=cg))]), C93)["decision"] == "pass"
        pr = '<sheetPr><outlinePr summaryRight="0"/></sheetPr>'
        assert run(book([("S", ws(rows, cols=cg, pr=pr))]), C93)["decision"] == "fail"
    # 93-F1: Excel writes width="0" hidden="1" for hidden columns (22 T4/T6 toys); inside a collapsed
    # column group they are exempt
    cz = ('<col min="3" max="4" width="0" hidden="1" customWidth="1" outlineLevel="1"/>'
          '<col min="5" max="5" width="9" collapsed="1"/>')
    v = run(book([("S", ws(rows, cols=cz))]), C93)
    assert v["decision"] == "pass" and v["stats"]["grouped_collapsed_cols_exempt"] == 2, v["mistakes"]
    # ... but the same columns without a group fail
    v = run(book([("S", ws(rows, cols='<col min="3" max="4" width="0" hidden="1" customWidth="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!C:D"]
    # hidden column on a case tab (toy T6), second sheet
    v = run(book([("Model", ws(rows)), ("Assumption", ws(rows, cols='<col min="7" max="7" width="17.9" hidden="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["Assumption!G:G"]


def test_93_sheet_defaults_hidden_sheets_and_variants():
    # zeroHeight: every row without its own height is hidden -> one sheet-level mistake
    rows = {1: ('ht="15" customHeight="1"', [c("A1", "x")]), 2: ("", [c("A2", "y")])}
    v = run(book([("S", ws(rows, fmt='<sheetFormatPr defaultRowHeight="15" zeroHeight="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S"], locs(v)
    assert "zeroHeight" in v["mistakes"][0]["description"]
    assert v["stats"]["rows_hidden_by_zero_default"] == 1048576 - 1
    # a hidden row on a hidden sheet counts (whole workbook)
    v = run(book([("A", ws(data_rows(1, 3))), ("B", ws(data_rows(1, 3, {2: 'hidden="1"'})))], states={1: "hidden"}), C93)
    assert v["decision"] == "fail" and locs(v) == ["B!2:2"] and "(hidden sheet)" in v["mistakes"][0]["description"]
    # rows without r= attributes: the 3rd row is hidden
    v = run(book([("S", ws({1: ("", [c("A1", 1)]), 2: ("", [c("A2", 2)]), 3: ('hidden="1"', [c("A3", 3)])}, no_r=True))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!3:3"], locs(v)
    # filtered-out rows count as hidden; the description says why they are hidden
    tail = '<autoFilter ref="A1:A10"><filterColumn colId="0"><filters><filter val="row 1"/></filters></filterColumn></autoFilter>'
    v = run(book([("S", ws(data_rows(1, 10, {4: 'hidden="1"'}), tail=tail))]), C93)
    assert v["decision"] == "fail" and "AutoFilter" in v["mistakes"][0]["description"]
    # near-zero (but not zero) height: not failed, listed in stats
    v = run(book([("S", ws(data_rows(1, 3, {2: 'ht="0.5" customHeight="1"'})))]), C93)
    assert v["decision"] == "pass" and v["stats"]["per_sheet"][0]["near_zero_rows"] == [(2, 0.5)]
    # empty hidden rows are not exempt
    v = run(book([("S", ws({1: ("", [c("A1", 1)]), 50: ('hidden="1"', [])}))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!50:50"]


def test_93_mistake_cap():
    attrs = {r: 'hidden="1"' for r in range(2, 62, 2)}         # 30 separate hidden rows
    v = run(book([("S", ws(data_rows(1, 61, attrs)))]), C93)
    assert v["decision"] == "fail"
    assert v["stats"]["n_mistakes"] == 30 and len(v["mistakes"]) == 25 and v["stats"]["mistakes_truncated"]


# ============================================================================ 73: pure functions
def test_73_text_metrics_and_wrap():
    assert abs(text_em("0123456789") - 5.56) < 1e-9
    assert abs(text_em("é") - text_em("e")) < 1e-9
    assert face_width("Arial", False) == 1.0 and face_width("Calibri", False) < 1.0 and face_width("Verdana", False) > 1.1
    assert face_width("Unknown Face", False) == 1.0
    assert mdw_px("Calibri", 11) == 7 and mdw_px("Arial", 10) == 7 and mdw_px("Aptos Narrow", 11) == 7
    assert col_px(8.43 + 5 / 7, 7) == 64 and col_px(0, 7) == 0
    pxem = 10 * 4 / 3                                                # Arial 10 at 96 dpi
    # toy T3 C54: 147 characters in a 33.9-character column -> 4 lines (Excel AutoFit: 4 lines)
    t3 = ("Note: tower asset value capitalises tower NOI (site rental revenue less straight-line rent, "
          "site rental costs and SG&A allocation) at the cap rate.")
    assert wrap_lines(t3, pxem, col_px(33.88671875, 7) - 5) == 4
    assert wrap_lines("short", pxem, 200) == 1
    assert wrap_lines("a\nb\n\nc", pxem, 200) == 4                      # typed breaks, empty paragraph
    assert wrap_lines("x" * 100, pxem, 100) >= 7                        # a long word breaks by characters
    # need: unwrapped = one line whatever the text; wrapped counts lines; rotated = projected width
    assert cell_need("a\nb\nc", 10, 1.0, False, 0, 100) == (13.0, 1)
    assert cell_need("a\nb\nc", 10, 1.0, True, 0, 100) == (39.0, 3)
    need, _ = cell_need("Revenue 2025 FY", 10, 1.0, False, 90, 50)
    assert 60 < need < 90, need
    assert cell_need("ABC", 10, 1.0, False, 255, 50)[0] == 39.0       # vertical stacked letters


# ============================================================================ 73: workbooks
def _instructions_title(st, size=48):
    """Case-style Instructions row 2: 48 pt title in merged B2:K2 at 60.75 pt."""
    s = st.xf("ROBOTECH GP", size, bold=True)
    cells = [c("B2", "Valuation Task", s)] + [c(f"{chr(67 + i)}2", None, s) for i in range(9)]
    return ws({1: ("", [c("A1", None)]), 2: ('ht="60.75" customHeight="1"', cells)},
              tail='<mergeCells count="1"><mergeCell ref="B2:K2"/></mergeCells>')


def test_73_toy_traps():
    st = Styles(("Aptos Narrow", 11))
    # Instructions title row: 60.75 pt for a 48 pt title -> need 62.4 -> fine
    v = run(book([("Instructions", _instructions_title(st))], st), C73)
    assert v["decision"] == "pass", v
    ok = v["stats"]["per_sheet"][0]["tall_rows_justified"][0]
    assert ok["row"] == 2 and ok["need"] == 62.4
    # the same row after a rewrite that dropped the title font to 10 pt -> fails (no exemption)
    st2 = Styles(("Aptos Narrow", 11))
    v = run(book([("Instructions", _instructions_title(st2, 10))], st2), C73)
    assert v["decision"] == "fail" and locs(v) == ["Instructions!2:2"]
    # toy T1: single-line data row at 120 pt
    st = Styles(("Aptos Narrow", 11))
    s = st.xf("Arial", 10)
    rows = {r: ("", [c(f"A{r}", "x", s)]) for r in range(1, 12)}
    rows[10] = ('ht="120" customHeight="1"', [c("A10", "5003", s), c("B10", "Manufacturing Overhead", s), c("C10", -3220, s)])
    v = run(book([("Budget", ws(rows))], st), C73)
    assert v["decision"] == "fail" and locs(v) == ["Budget!10:10"]
    assert "8.39x" in v["mistakes"][0]["description"]          # ratio shown with 2 decimals (73-F6)
    # toy T3: 4 wrapped lines at 55.2 (under the cap) pass; at 180 fail; at 100 (< 2x) pass
    t3 = ("Note: tower asset value capitalises tower NOI (site rental revenue less straight-line rent, "
          "site rental costs and SG&A allocation) at the cap rate.")
    for ht, want in ((55.2, "pass"), (180, "fail"), (100, "pass")):
        st = Styles(("Aptos Narrow", 11))
        sw = st.xf("Arial", 10, wrap=True)
        x = ws({54: (f'ht="{ht}" customHeight="1"', [c("C54", t3, sw)])}, cols='<col min="3" max="3" width="33.88671875" customWidth="1"/>')
        v = run(book([("Solution Model", x)], st), C73)
        assert v["decision"] == want, (ht, v["summary"], v["mistakes"])
    # toy T4: 18 pt title at 26.4 passes; at 110 fails
    for ht, want in ((26.4, "pass"), (110, "fail")):
        st = Styles(("Aptos Narrow", 11))
        s18 = st.xf("Arial", 18, bold=True)
        v = run(book([("Solution Model", ws({1: (f'ht="{ht}" customHeight="1"', [c("A1", "Valuation Task: Comparables", s18)])}))], st), C73)
        assert v["decision"] == want


def test_73_cap_hidden_rows_and_defaults():
    # exactly 60 pt passes (not over 60); 61 pt with no content fails (61 > 2 x 14.3)
    v = run(book([("S", ws({1: ('ht="60" customHeight="1"', []), 3: ('ht="61" customHeight="1"', [])}))]), C73)
    assert v["decision"] == "fail" and locs(v) == ["S!3:3"]
    # hidden 200 pt row is ignored; zero-height row too
    v = run(book([("S", ws({1: ('ht="200" hidden="1"', [c("A1", "x")]), 2: ('ht="0"', [])}))]), C73)
    assert v["decision"] == "pass"
    # consecutive failing rows are one mistake
    rows = {r: ('ht="95" customHeight="1"', [c(f"A{r}", "x")]) for r in range(27, 35)}
    v = run(book([("S", ws(rows))]), C73)
    assert locs(v) == ["S!27:34"] and "8 rows" in v["mistakes"][0]["description"]
    # a sheet default row height of 80 pt: every unsized row is that tall -> sheet-level mistake
    v = run(book([("S", ws({1: ('ht="15"', [c("A1", "x")])}, fmt='<sheetFormatPr defaultRowHeight="80" customHeight="1"/>'))]), C73)
    assert v["decision"] == "fail" and locs(v) == ["S"], locs(v)
    # hidden sheets are graded too
    v = run(book([("A", ws({})), ("B", ws({4: ('ht="90" customHeight="1"', [c("A4", "x")])}))], states={1: "hidden"}), C73)
    assert locs(v) == ["B!4:4"] and "(hidden sheet)" in v["mistakes"][0]["description"]


def test_73_fonts_rich_text_rotation_and_breaks():
    # rich text: a 36 pt run needs 46.8 pt -> an 80 pt row is fine
    sst = ['<si><r><t>small </t></r><r><rPr><sz val="36"/><rFont val="Arial"/></rPr><t>BIG</t></r></si>']
    v = run(book([("S", ws({2: ('ht="80" customHeight="1"', [c("A2", 0, t="s")])}))], sst=sst), C73)
    assert v["decision"] == "pass", v["mistakes"]
    # rotated header (90 deg) at 80 pt: its width is the need
    st = Styles()
    sr = st.xf("Arial", 10, rotation=90)
    v = run(book([("S", ws({1: ('ht="80" customHeight="1"', [c("A1", "Revenue growth 2025", sr)])}))], st), C73)
    assert v["decision"] == "pass", v["mistakes"]
    # typed line breaks count only with wrap on
    st = Styles()
    sn, sw = st.xf("Arial", 10), st.xf("Arial", 10, wrap=True)
    txt = "line one\nline two\nline three\nline four"
    v = run(book([("S", ws({1: ('ht="70" customHeight="1"', [c("A1", txt, sn)]),
                            2: ('ht="70" customHeight="1"', [c("A2", txt, sw)])}, cols='<col min="1" max="1" width="40"/>'))], st), C73)
    assert locs(v) == ["S!1:1"], locs(v)
    with patched(M73, LINE_BREAKS_NEED_WRAP=False):
        v = run(book([("S", ws({1: ('ht="70" customHeight="1"', [c("A1", txt, sn)])}, cols='<col min="1" max="1" width="40"/>'))], st), C73)
        assert v["decision"] == "pass"
    # justify alignment wraps like wrapText
    st = Styles()
    sj = st.xf("Arial", 10, horizontal="justify")
    long = "word " * 60
    v = run(book([("S", ws({1: ('ht="90" customHeight="1"', [c("A1", long, sj)])}, cols='<col min="1" max="1" width="20"/>'))], st), C73)
    assert v["decision"] == "pass", v["mistakes"]


def test_73_merges_and_hidden_columns():
    long = ("This explanatory note is long enough to need several lines in one narrow column but only "
            "two lines across the merged block.")
    # anchor of a horizontal merge uses the merged width: 2 lines -> 70 pt fails;
    # unmerged in its narrow column: many lines -> 70 pt fine
    st = Styles()
    sw = st.xf("Arial", 10, wrap=True)
    cols = '<col min="1" max="6" width="12" customWidth="1"/>'
    merged = ws({3: ('ht="70" customHeight="1"', [c("A3", long, sw)])}, cols=cols,
                tail='<mergeCells count="1"><mergeCell ref="A3:F3"/></mergeCells>')
    v = run(book([("S", merged)], st), C73)
    assert v["decision"] == "fail", v["stats"]["per_sheet"]
    single = ws({3: ('ht="70" customHeight="1"', [c("A3", long, sw)])}, cols=cols)
    assert run(book([("S", single)], st), C73)["decision"] == "pass"
    # covered cells of a merge are ignored even when they hold text
    cov = ws({3: ('ht="70" customHeight="1"', [c("A3", "x", sw), c("B3", long, sw)])}, cols=cols,
             tail='<mergeCells count="1"><mergeCell ref="A3:F3"/></mergeCells>')
    assert run(book([("S", cov)], st), C73)["decision"] == "fail"
    # vertical merge: anchor A5 (15 pt row) + row 6 at 80 pt; the anchor (read in a second pass)
    # needs ~90 pt -> row 6 needs ~75 pt -> fine; a short anchor -> row 6 fails
    para = "Explanation " * 40
    for text, want in ((para, "pass"), ("short", "fail")):
        st = Styles()
        sw = st.xf("Arial", 10, wrap=True)
        x = ws({5: ('ht="15" customHeight="1"', [c("A5", text, sw)]), 6: ('ht="80" customHeight="1"', [c("A6", None, sw)])},
               cols='<col min="1" max="1" width="30" customWidth="1"/>',
               tail='<mergeCells count="1"><mergeCell ref="A5:A6"/></mergeCells>')
        v = run(book([("S", x)], st), C73)
        assert v["decision"] == want, (want, v["mistakes"], v["stats"]["per_sheet"])
        assert v["stats"]["second_pass_sheets"] == ["S"]
    # text in a hidden column needs nothing
    st = Styles()
    sw = st.xf("Arial", 10, wrap=True)
    x = ws({2: ('ht="100" customHeight="1"', [c("B2", para, sw)])}, cols='<col min="2" max="2" width="20" hidden="1"/>')
    assert run(book([("S", x)], st), C73)["decision"] == "fail"


def test_73_formula_values_only_where_needed():
    para = "Explanation " * 30
    app = "Microsoft Excel Compatible / Openpyxl 3.1.5"           # openpyxl label: caches untrusted
    st = Styles()
    sw, sn = st.xf("Arial", 10, wrap=True), st.xf("Arial", 10)
    cols = '<col min="1" max="2" width="30" customWidth="1"/>'
    # unwrapped formula cell: one line whatever its value -> decided without reading it
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", para, sn, f='"x"&"y"')])}, cols=cols)
    v = run(book([("S", x)], st, app=app), C73)
    assert v["decision"] == "fail" and v["stats"]["formula_values_read"] == 0
    # wrapped formula cell in a tall row: its value is needed; untrusted -> GradingError (no fallback)
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", para, sw, f='REPT("Explanation ",30)')])}, cols=cols)
    graded("untrusted_value", lambda: run(book([("S", x)], st, app=app), C73), "needs the value of S!A2", "openpyxl")
    # ... but not when a constant in the same row already justifies the height
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", para, sw, f='REPT("Explanation ",30)'), c("B2", para, sw)])}, cols=cols)
    v = run(book([("S", x)], st, app=app), C73)
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 0
    # trusted cache (no writer label): the value is read and justifies the height
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", para, sw, f='REPT("Explanation ",30)')])}, cols=cols)
    v = run(book([("S", x)], st), C73)
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 1
    # rows under the cap never read values
    x = ws({2: ('ht="40" customHeight="1"', [c("A2", para, sw, f='REPT("Explanation ",30)')])}, cols=cols)
    assert run(book([("S", x)], st, app=app), C73)["decision"] == "pass"


def test_73_values_after_anchors_untrusted_only_if_needed():
    app = "Microsoft Excel Compatible / Openpyxl 3.1.5"           # openpyxl label: caches untrusted
    long = " ".join(["word"] * 120)
    cols = '<col min="1" max="1" width="12" customWidth="1"/><col min="2" max="2" width="12" customWidth="1"/>'
    # 73-F1: row 6 (80 pt) is justified by the merge A5:A6 anchored in row 5 (15 pt, read in a second
    # pass); its wrapped formula B6 is not needed, so its untrusted value is never read
    st = Styles()
    sw = st.xf("Arial", 10, wrap=True)
    x = ws({5: ('ht="15" customHeight="1"', [c("A5", long, sw)]),
            6: ('ht="80" customHeight="1"', [c("A6", None, sw), c("B6", "ok", sw, f='"o"&"k"')])},
           cols=cols, tail='<mergeCells count="1"><mergeCell ref="A5:A6"/></mergeCells>')
    for label in (app, "Microsoft Excel"):
        v = run(book([("S", x)], st, app=label), C73)
        assert v["decision"] == "pass", (label, v["mistakes"])
        assert v["stats"]["formula_values_read"] == 0 and v["stats"]["second_pass_sheets"] == ["S"], v["stats"]
    # sanity finding: a file that already fails without values does not raise for a row that
    # would need an UNTRUSTED value; that row is left undecided and listed
    st = Styles()
    sw, sn = st.xf("Arial", 10, wrap=True), st.xf("Arial", 10)
    x = ws({2: ('ht="61" customHeight="1"', [c("A2", "Title", sn)]),
            46: ('ht="76" customHeight="1"', [c("B46", "short", sw, f='"sh"&"ort"')])}, cols=cols)
    v = run(book([("S", x)], st, app=app), C73)
    assert v["decision"] == "fail" and locs(v) == ["S!2:2"], (v["mistakes"], v["stats"])
    assert v["stats"]["formula_values_read"] == 0 and v["stats"]["rows_undecided"] == 1
    assert v["stats"]["undecided_rows"][0]["row"] == 46 and v["stats"]["undecided_rows"][0]["cells_needing_values"] == ["B46"]
    assert "undecided" in v["summary"]
    with patched(M73, UNTRUSTED_ONLY_IF_VERDICT_NEEDS=False):
        graded("untrusted_value", lambda: run(book([("S", x)], st, app=app), C73), "needs the value of S!B46", "openpyxl")
    # trusted values are read before untrusted ones: row 3's trusted cache settles the verdict (fail),
    # so row 2's formula without a cache (untrusted) is never needed
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", None, sw, f='REPT("Explanation ",30)')]),
            3: ('ht="90" customHeight="1"', [c("A3", "short", sw, f='"sh"&"ort"')])}, cols=cols)
    v = run(book([("S", x)], st), C73)
    assert v["decision"] == "fail" and locs(v) == ["S!3:3"], v["mistakes"]
    assert v["stats"]["formula_values_read"] == 1 and v["stats"]["rows_undecided"] == 1
    # trusted values are read even when the file already fails, so the mistake list stays complete
    x = ws({2: ('ht="61" customHeight="1"', [c("A2", "Title", sn)]),
            3: ('ht="90" customHeight="1"', [c("A3", "short", sw, f='"sh"&"ort"')]),
            4: ('ht="90" customHeight="1"', [c("A4", long, sw, f='REPT("Explanation ",30)')])}, cols=cols)
    v = run(book([("S", x)], st), C73)
    assert locs(v) == ["S!2:3"], locs(v)
    assert v["stats"]["formula_values_read"] == 2 and v["stats"]["rows_undecided"] == 0
    # ... and when no row fails, the untrusted value is really needed -> GradingError
    x = ws({2: ('ht="90" customHeight="1"', [c("A2", None, sw, f='REPT("Explanation ",30)')])}, cols=cols)
    graded("untrusted_value", lambda: run(book([("S", x)], st), C73), "needs the value of S!A2")


def test_73_rich_runs_indent_cap_and_labels():
    # 73-F2: a 48 pt cell whose every rich run is 10 pt shows 10 pt text -> the 60.75 pt row fails
    def title(cell_pt, runs_xml):
        st = Styles(("Aptos Narrow", 11))
        s = st.xf("Arial", cell_pt, bold=True)
        cells = [c("B2", 0, s, t="s")] + [c(f"{chr(67 + i)}2", None, s) for i in range(9)]
        x = ws({2: ('ht="60.75" customHeight="1"', cells)}, tail='<mergeCells count="1"><mergeCell ref="B2:K2"/></mergeCells>')
        return run(book([("Instructions", x)], st, sst=[f"<si>{runs_xml}</si>"]), C73)
    small = '<r><rPr><b/><sz val="10"/><rFont val="Arial"/></rPr><t>Valuation</t></r><r><rPr><sz val="10"/></rPr><t> Task</t></r>'
    v = title(48, small)
    assert v["decision"] == "fail" and locs(v) == ["Instructions!2:2"], v["stats"]
    # runs without <rPr>, or <rPr> without a size, show the cell's 48 pt -> fine
    assert title(48, '<r><t>Valuation</t></r><r><t xml:space="preserve"> Task</t></r>')["decision"] == "pass"
    assert title(48, '<r><rPr><b/></rPr><t>Valuation Task</t></r>')["decision"] == "pass"
    # a 48 pt run in a 10 pt cell -> fine (unchanged)
    assert title(10, '<r><rPr><sz val="48"/></rPr><t>Valuation Task</t></r>')["decision"] == "pass"
    # 73-F4: the alignment indent narrows the wrap width (Calibri 11 Normal: 3 spaces = 9 px a level)
    text = "Revenue grows with volume and price; costs follow the plan for"
    for indent, want in ((0, "fail"), (3, "pass")):
        st = Styles()
        st.fonts.append('<font><sz val="10"/><name val="Arial"/></font>')
        st.xfs.append(f'<xf numFmtId="0" fontId="{len(st.fonts) - 1}" fillId="0" borderId="0" xfId="0" applyFont="1" '
                      f'applyAlignment="1"><alignment wrapText="1" horizontal="left" indent="{indent}"/></xf>')
        x = ws({5: ('ht="61" customHeight="1"', [c("B5", text, len(st.xfs) - 1)])},
               cols='<col min="2" max="2" width="30" customWidth="1"/>')
        v = run(book([("S", x)], st), C73)
        assert v["decision"] == want, (indent, v["stats"]["per_sheet"])
    # 73-F5: one-line cells are not counted against the buffer cap; wrapped text cells are
    with patched(M73, MAX_BUFFERED_CELLS=100):
        rows = {r: ('ht="80" customHeight="1"', [c(f"{chr(65 + k)}{r}", k) for k in range(26)]) for r in range(1, 51)}
        v = run(book([("S", ws(rows))]), C73)
        assert v["decision"] == "fail" and locs(v) == ["S!1:50"], locs(v)
        st = Styles()
        sw = st.xf("Arial", 10, wrap=True)
        rows = {r: ('ht="80" customHeight="1"', [c(f"{chr(65 + k)}{r}", "some text", sw) for k in range(26)])
                for r in range(1, 6)}
        raises(lambda: run(book([("S", ws(rows))], st), C73), "wrapped or rotated text cells")
    # one-line cells: a covered one-line cell is skipped, the anchor of a vertical merge gives its share
    st = Styles()
    s36 = st.xf("Arial", 36)
    x = ws({3: ('ht="62" customHeight="1"', [c("A3", "x"), c("B3", "BIG", s36)])},
           tail='<mergeCells count="1"><mergeCell ref="A3:B3"/></mergeCells>')
    assert run(book([("S", x)], st), C73)["decision"] == "fail"           # B3 (36 pt) is covered by A3:B3
    x = ws({3: ('ht="62" customHeight="1"', [c("A3", "x"), c("B3", "BIG", s36)])})
    assert run(book([("S", x)], st), C73)["decision"] == "pass"           # B3 shows: 46.8 pt
    # 73-F6: ratio with 2 decimals (2.04x, not 2.0x); a sheet default of 80 pt WITH customHeight:
    # only the sheet-level mistake, not the rows without a height of their own again
    t3 = ("Note: tower asset value capitalises tower NOI (site rental revenue less straight-line rent, "
          "site rental costs and SG&A allocation) at the cap rate.")
    st = Styles(("Aptos Narrow", 11))
    sw = st.xf("Arial", 10, wrap=True)
    x = ws({54: ('ht="106" customHeight="1"', [c("C54", t3, sw)])}, cols='<col min="3" max="3" width="33.88671875" customWidth="1"/>')
    v = run(book([("Solution Model", x)], st), C73)
    assert "2.04x the need" in v["mistakes"][0]["description"], v["mistakes"]
    v = run(book([("S", ws(data_rows(1, 5), fmt='<sheetFormatPr defaultRowHeight="80" customHeight="1"/>'))]), C73)
    assert locs(v) == ["S"], locs(v)
    assert v["stats"]["per_sheet"][0]["default_height_rows_written"] == 5
    # rows with a height of their own on such a sheet are still graded
    rows = data_rows(1, 5)
    rows[3] = ('ht="120" customHeight="1"', [c("A3", "x")])
    v = run(book([("S", ws(rows, fmt='<sheetFormatPr defaultRowHeight="80" customHeight="1"/>'))]), C73)
    assert locs(v) == ["S!3:3", "S"], locs(v)
    assert "manually set height" in v["mistakes"][0]["description"]


def test_73_excel_2026_10_03_non_custom_heights_ignored():
    """Q6: heights without customHeight are not honoured (Excel auto-fits): a row ht of 60/90 pt
    without customHeight and an 80 pt sheetFormatPr default without customHeight never fail;
    the same heights with customHeight="1" do."""
    rows = {3: ("", [c("A3", "x")]), 5: ('ht="90"', [c("A5", "x")]), 7: ('ht="90" customHeight="1"', [c("A7", "x")])}
    v = run(book([("S", ws(rows, fmt='<sheetFormatPr defaultRowHeight="80"/>'))]), C73)
    assert locs(v) == ["S!7:7"], locs(v)
    assert v["stats"]["non_custom_row_heights_ignored"] == 1, v["stats"]
    assert v["stats"]["non_custom_default_heights_ignored"] == {"S": 80.0}, v["stats"]
    v = run(book([("S", ws(rows, fmt='<sheetFormatPr defaultRowHeight="80" customHeight="1"/>'))]), C73)
    assert locs(v) == ["S!7:7", "S"], locs(v)                    # the custom default is honoured
    v = run(book([("S", ws({5: ('ht="135"', [c("A5", "a long single-line note")])}))]), C73)
    assert v["decision"] == "pass", locs(v)                       # GPT-6 style non-custom tall row (1505)


def test_73_instructions_title_with_lost_font_fails():
    """Ruling 2026-10-03 (A4): an Instructions title row kept at 60.75 pt (custom) whose 48 pt font
    the delivering tool dropped (10 pt now) FAILS; with its 48 pt font it passes."""
    st = Styles(("Calibri", 11))
    s10, s48 = st.xf("Calibri", 10), st.xf("Calibri", 48)
    for s_, want in ((s10, "fail"), (s48, "pass")):
        x = ws({2: ('ht="60.75" customHeight="1"', [c("B2", "Valuation Task: Comparables", s_)])})
        v = run(book([("Instructions", x)], st), C73)
        assert v["decision"] == want, (want, v["mistakes"])


def test_73_vertical_merge_whole_block():
    """Second review 73-R2-1 / sanity Rec 73-6 (2026-10-04): a merge over several rows is judged
    as one block; each row is credited with the merge's need in proportion to its height.  A 48 pt
    title merged A5:A6 over rows of 40 + 62 pt is a 102 pt block for 62.4 pt of text (1.63x), so
    row 6 passes; the per-row share (62.4 - 40 = 22.4, 2.77x) failed it."""
    merge = '<mergeCells count="1"><mergeCell ref="A5:A6"/></mergeCells>'
    st = Styles()
    s48 = st.xf("Arial", 48)

    def title_rows(h5, h6):
        return ws({5: (f'ht="{h5}" customHeight="1"', [c("A5", "Title", s48)]),
                   6: (f'ht="{h6}" customHeight="1"', [c("A6", None, s48)])}, tail=merge)

    v = run(book([("S", title_rows(40, 62))], st), C73)
    assert v["decision"] == "pass", (v["mistakes"], v["stats"]["per_sheet"])
    ok = v["stats"]["per_sheet"][0]["tall_rows_justified"][0]
    assert ok["row"] == 6 and ok["need"] == 37.9 and ok["ratio"] == 1.63, ok
    assert "A5: one line of 48 pt text in merged A5:A6 (62.4 pt for the 102.0 pt block of rows 5:6; this row's share 37.9 pt)" == ok["why"], ok["why"]
    assert v["stats"]["second_pass_sheets"] == ["S"] and v["stats"]["options"]["merge_rows_whole_block"] is True
    # the old per-row share is still available behind the option (and still fails row 6 at 22.4 pt)
    with patched(M73, MERGE_ROWS_WHOLE_BLOCK=False):
        v = run(book([("S", title_rows(40, 62))], st), C73)
        assert v["decision"] == "fail" and locs(v) == ["S!6:6"], v["mistakes"]
        assert v["stats"]["per_sheet"][0]["failing_rows"][0]["need"] == 22.4, v["stats"]["per_sheet"]
    # a block that is itself excessive (240 pt for 62.4 pt of text, 3.85x) fails row 6 either way
    for flag in (True, False):
        with patched(M73, MERGE_ROWS_WHOLE_BLOCK=flag):
            v = run(book([("S", title_rows(40, 200))], st), C73)
            assert v["decision"] == "fail" and locs(v) == ["S!6:6"], (flag, v["mistakes"])
    # both rows over the cap: a 122 pt block (61 + 61, 1.96x) passes both rows; a 126 pt block (62 + 64,
    # 2.02x) fails both, as one run
    assert run(book([("S", title_rows(61, 61))], st), C73)["decision"] == "pass"
    v = run(book([("S", title_rows(62, 64))], st), C73)
    assert locs(v) == ["S!5:6"], v["mistakes"]
    assert "2.02x the need" in v["mistakes"][0]["description"], v["mistakes"]
    # the block changes only the need, not the 60 pt cap: 55 + 55 pt (a 110 pt block, 1.76x) and even
    # 60 + 60 pt for a short 10 pt word (9.2x) are not examined, since no row is over 60 pt
    s10 = st.xf("Arial", 10)
    for h, word in ((55, "Title"), (60, "x")):
        x = ws({5: (f'ht="{h}" customHeight="1"', [c("A5", word, s10 if word == "x" else s48)]),
                6: (f'ht="{h}" customHeight="1"', [c("A6", None, s48)])}, tail=merge)
        v = run(book([("S", x)], st), C73)
        assert v["decision"] == "pass" and v["stats"]["rows_over_cap"] == 0, (h, v["stats"])
    # a wrapped anchor in the candidate row: 100 pt + 15 pt holding 7 lines (91 pt) of text passes
    # (share 91 x 100 / 115 = 79 pt); an equal split (45.5) would have failed it
    st = Styles()
    sw = st.xf("Arial", 10, wrap=True)
    para = "Explanation " * 14                       # 2 words a line in a 30-wide Calibri-11 column
    x = ws({5: ('ht="100" customHeight="1"', [c("A5", para, sw)]), 6: ('ht="15" customHeight="1"', [c("A6", None, sw)])},
           cols='<col min="1" max="1" width="30" customWidth="1"/>', tail=merge)
    v = run(book([("S", x)], st), C73)
    assert v["decision"] == "pass", v["mistakes"]
    ok = v["stats"]["per_sheet"][0]["tall_rows_justified"][0]
    assert ok["row"] == 5 and 75 < ok["need"] < 85 and "115.0 pt block" in ok["why"], ok
    # a hidden anchor row counts 0 pt: row 6 carries the whole need (as before)
    x = ws({5: ('ht="15" customHeight="1" hidden="1"', [c("A5", para, sw)]), 6: ('ht="80" customHeight="1"', [c("A6", None, sw)])},
           cols='<col min="1" max="1" width="30" customWidth="1"/>', tail=merge)
    v = run(book([("S", x)], st), C73)
    assert v["decision"] == "pass" and "80.0 pt block" in v["stats"]["per_sheet"][0]["tall_rows_justified"][0]["why"], v["stats"]["per_sheet"]
    # a wrapped FORMULA anchor read in finish(): the trusted value is laid out with the same block share
    x = ws({5: ('ht="100" customHeight="1"', [c("A5", para, sw, f='REPT("Explanation ",14)')]), 6: ('ht="15" customHeight="1"', [c("A6", None, sw)])},
           cols='<col min="1" max="1" width="30" customWidth="1"/>', tail=merge)
    v = run(book([("S", x)], st), C73)
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 1, (v["mistakes"], v["stats"])
    assert "115.0 pt block" in v["stats"]["per_sheet"][0]["tall_rows_justified"][0]["why"]


def test_73_overlapping_cols_later_entry_wins():
    """Overlapping <col> entries: the later entry wins (the core reader's one rule, SheetHead.col_info; 73.md
    question 12).  A:J 60 followed by B:B 5: C is 60 wide, so the note needs 2 lines and a 70 pt row fails.
    Before the fix col_info returned the sheet default (8.43) for C - many lines, and the row passed."""
    long = ("This explanatory note is long enough to need several lines in one narrow column but only "
            "two lines across the merged block.")
    st = Styles()
    sw = st.xf("Arial", 10, wrap=True)
    cols = '<col min="1" max="10" width="60" customWidth="1"/><col min="2" max="2" width="5" customWidth="1"/>'
    v = run(book([("S", ws({3: ('ht="70" customHeight="1"', [c("C3", long, sw)])}, cols=cols))], st), C73)
    assert v["decision"] == "fail" and locs(v) == ["S!3:3"], (v["mistakes"], v["stats"]["per_sheet"])
    # in B, the later 5-wide entry wins: many lines, the 70 pt row is needed
    v = run(book([("S", ws({3: ('ht="70" customHeight="1"', [c("B3", long, sw)])}, cols=cols))], st), C73)
    assert v["decision"] == "pass", v["mistakes"]


def test_93_overlapping_cols_later_entry_wins():
    """Overlapping <col> entries through the shared reading (SheetHead.col_segments): the later entry
    - by min, then file order - wins on the columns it covers, as a whole, so a later VISIBLE entry
    clears an earlier hidden one (93.md said so; until 2026-10-04 the loop only ever added reasons).
    Before: C:XFD hidden + D:D visible failed as one run C:XFD; E hidden + E visible (same min, file
    order) failed at E."""
    rows = data_rows(1, 3)
    cols = '<col min="3" max="16384" width="9" hidden="1"/><col min="4" max="4" width="12" customWidth="1"/>'
    v = run(book([("S", ws(rows, cols=cols))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!C:C", "S!E:XFD"], locs(v)
    assert v["stats"]["hidden_cols"] == 16384 - 3, v["stats"]
    # the file lists the wider entry later (the reader sorts by min: D:D still wins on D)
    cols_rev = '<col min="4" max="4" width="12" customWidth="1"/><col min="3" max="16384" width="9" hidden="1"/>'
    assert locs(run(book([("S", ws(rows, cols=cols_rev))]), C93)) == ["S!C:C", "S!E:XFD"]
    # same min: file order decides - a hidden entry overridden by a later visible one hides nothing
    same = '<col min="5" max="5" width="9" hidden="1"/><col min="5" max="5" width="9" customWidth="1"/>'
    v = run(book([("S", ws(rows, cols=same))]), C93)
    assert v["decision"] == "pass", v["mistakes"]
    # ... and the other way round it hides E
    v = run(book([("S", ws(rows, cols='<col min="5" max="5" width="9"/><col min="5" max="5" width="9" hidden="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!E:E"]
    # a later zero-width entry inside a visible range still hides its columns (later wins)
    v = run(book([("S", ws(rows, cols='<col min="1" max="10" width="9"/><col min="3" max="3" width="0" customWidth="1"/>'))]), C93)
    assert v["decision"] == "fail" and locs(v) == ["S!C:C"] and "zero column width" in v["mistakes"][0]["description"]


TESTS = [test_93_collapsed_members, test_93_hidden_row_flag_and_zero_height,
         test_93_collapsed_group_passes_ungrouped_fails, test_93_partial_and_nested_groups, test_93_columns,
         test_93_sheet_defaults_hidden_sheets_and_variants, test_93_mistake_cap,
         test_73_text_metrics_and_wrap, test_73_toy_traps, test_73_cap_hidden_rows_and_defaults,
         test_73_fonts_rich_text_rotation_and_breaks, test_73_merges_and_hidden_columns,
         test_73_formula_values_only_where_needed, test_73_values_after_anchors_untrusted_only_if_needed,
         test_73_rich_runs_indent_cap_and_labels,
         test_73_excel_2026_10_03_non_custom_heights_ignored, test_73_instructions_title_with_lost_font_fails,
         test_73_vertical_merge_whole_block, test_73_overlapping_cols_later_entry_wins,
         test_93_overlapping_cols_later_entry_wins]


def main() -> int:
    failed = 0
    try:
        for t in TESTS:
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        if _TMP and os.path.isdir(_TMP):
            shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
