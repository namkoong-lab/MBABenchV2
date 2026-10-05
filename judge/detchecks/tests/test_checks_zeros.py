"""Unit tests for check 66 (Zeros as dashes) on synthetic micro-workbooks (raw SpreadsheetML
zips; an openpyxl-labelled app.xml where cache trust matters).

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_zeros

Plain asserts; also collectable by pytest.  Temporary files go to detchecks/scratch/zeros/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile
from xml.sax.saxutils import escape, quoteattr

from detchecks.api import Engine
from detchecks.checks import REGISTRY, c66
from detchecks.checks._cfeval import is_unevaluable
from detchecks.checks.c66 import (BLANK, C66, DASH, DATETIME, DIGIT, HIDDEN, NONZERO, TEXT, UNCERTAIN, UNKNOWN,
                                  classify, fails, fires, literal)
from detchecks.core import numfmt as N
from detchecks.core.numfmt import resolve_format
from detchecks.core.sheet import CfRule
from detchecks.errors import GradingError

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "zeros")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WS = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"
RUBRIC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "prompts", "rubrics", "rubric_9.json")

_TMP = None
_N = [0]


def tmp(name: str = "") -> str:
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_zeros_", dir=SCRATCH)
    _N[0] += 1
    return os.path.join(_TMP, f"{_N[0]:03d}_{name or 'book'}.xlsx")


# ============================================================================ raw workbook builder
class Styles:
    """styles.xml builder: xf(numfmt) / dxf(numfmt) return their index.  numfmt: a code (custom
    <numFmt>) or an int id written without a <numFmt> entry (built-in)."""

    def __init__(self):
        self.numfmts: dict = {}
        self.xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
        self.dxfs: list = []

    def numfmt(self, code: str) -> int:
        for k, v in self.numfmts.items():
            if v == code:
                return k
        k = 164 + len(self.numfmts)
        self.numfmts[k] = code
        return k

    def xf(self, numfmt) -> int:
        nf = self.numfmt(numfmt) if isinstance(numfmt, str) else numfmt
        self.xfs.append(f'<xf numFmtId="{nf}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>')
        return len(self.xfs) - 1

    def dxf(self, numfmt: str) -> int:
        self.dxfs.append(f'<dxf><numFmt numFmtId="{300 + len(self.dxfs)}" formatCode={quoteattr(numfmt)}/></dxf>')
        return len(self.dxfs) - 1

    def xml(self) -> str:
        nf = "".join(f'<numFmt numFmtId="{k}" formatCode={quoteattr(v)}/>' for k, v in self.numfmts.items())
        return (f'<styleSheet xmlns="{MAIN}">'
                + (f'<numFmts count="{len(self.numfmts)}">{nf}</numFmts>' if nf else "")
                + '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
                + '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                  '<fill><patternFill patternType="gray125"/></fill></fills>'
                + '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
                + f'<cellXfs count="{len(self.xfs)}">{"".join(self.xfs)}</cellXfs>'
                + '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                + f'<dxfs count="{len(self.dxfs)}">{"".join(self.dxfs)}</dxfs></styleSheet>')


def c(ref, s=0, v=None, t=None, f=None, si=None):
    """One <c>.  v: number / str (inline string unless t given) / bool; f: formula text
    (f='' with si: a shared-formula child)."""
    attrs = f'r="{ref}"' + (f' s="{s}"' if s else "")
    if f is not None:
        tt = t or ("str" if isinstance(v, str) else "b" if isinstance(v, bool) else None)
        vv = "" if v is None else ("1" if v is True else "0" if v is False else escape(str(v)))
        fx = (f'<f t="shared" si="{si}"/>' if f == "" else f'<f t="shared" ref="{ref}:{ref}" si="{si}">{escape(f)}</f>') \
            if si is not None else f"<f>{escape(f)}</f>"
        return f'<c {attrs}{f" t=\"{tt}\"" if tt else ""}>{fx}<v>{vv}</v></c>'
    if v is None:
        return f"<c {attrs}/>"
    if t == "e":
        return f'<c {attrs} t="e"><v>{escape(v)}</v></c>'
    if t == "s":
        return f'<c {attrs} t="s"><v>{v}</v></c>'
    if isinstance(v, bool):
        return f'<c {attrs} t="b"><v>{int(v)}</v></c>'
    if isinstance(v, str):
        return f'<c {attrs} t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
    return f"<c {attrs}><v>{v!r}</v></c>"


def sheet(cells, *, head="", tail="", rows_attr=None):
    rows: dict = {}
    for x in cells:
        ref = re.search(r'r="([A-Z]+)(\d+)"', x)
        rows.setdefault(int(ref.group(2)), []).append((ref.group(1), x))
    rows_attr = rows_attr or {}
    for r in rows_attr:
        rows.setdefault(r, [])

    def colkey(col):
        n = 0
        for ch in col:
            n = n * 26 + ord(ch) - 64
        return n
    body = "".join(f'<row r="{r}"{rows_attr.get(r, "")}>'
                   + "".join(x for _k, x in sorted(rows[r], key=lambda y: colkey(y[0]))) + "</row>"
                   for r in sorted(rows))
    return f"{head}<sheetData>{body}</sheetData>{tail}"


def book(sheets, styles: Styles, *, sst=None, states=None, app=None, name=""):
    """sheets: [(name, inner_xml)].  No docProps unless app is given (writer 'unknown': formula
    caches written here are trusted); app='Microsoft Excel Compatible / Openpyxl 3.1.5' labels the
    file openpyxl (caches untrusted)."""
    path = tmp(name)
    states = states or {}
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    ov = [f'<Override PartName="/xl/workbook.xml" ContentType="{CT_XLSX}"/>']
    ov += [f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="{CT_WS}"/>' for i in range(len(sheets))]
    z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>' + "".join(ov) + "</Types>")
    z.writestr("_rels/.rels", f'<Relationships xmlns="{PKG_REL}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    if app is not None:
        z.writestr("docProps/app.xml", '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                   f"<Application>{escape(app)}</Application></Properties>")
    wrels, sh = [], []
    for i, (nm, xml) in enumerate(sheets):
        st = f' state="{states[i]}"' if i in states else ""
        sh.append(f'<sheet name={quoteattr(nm)} sheetId="{i + 1}"{st} r:id="rId{i + 1}"/>')
        wrels.append(f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>')
        z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                   f'{DECL}<worksheet xmlns="{MAIN}" xmlns:r="{REL}" xmlns:x14="{X14}" xmlns:xm="{XM}">{xml}</worksheet>')
    wrels.append(f'<Relationship Id="rIdS" Type="{REL}/styles" Target="styles.xml"/>')
    z.writestr("xl/styles.xml", styles.xml())
    if sst is not None:
        wrels.append(f'<Relationship Id="rIdT" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{escape(x)}</t></si>" for x in sst) + "</sst>")
    z.writestr("xl/workbook.xml", f'{DECL}<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>' + "".join(sh) + "</sheets></workbook>")
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG_REL}">' + "".join(wrels) + "</Relationships>")
    z.close()
    return path


def cf_main(sqref, *rules):
    """rules: (type, dxf_id, operator, [formulas], extra_attrs)."""
    out = []
    for k, (typ, dxf, op, formulas, extra) in enumerate(rules):
        fx = "".join(f"<formula>{escape(x)}</formula>" for x in formulas)
        out.append(f'<cfRule type="{typ}"' + (f' dxfId="{dxf}"' if dxf is not None else "")
                   + f' priority="{k + 1}"' + (f' operator="{op}"' if op else "") + f"{extra}>{fx}</cfRule>")
    return f'<conditionalFormatting sqref="{sqref}">' + "".join(out) + "</conditionalFormatting>"


def cf_x14(sqref, typ, op, formula, numfmt):
    return (f'<extLst><ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}"><x14:conditionalFormattings>'
            f'<x14:conditionalFormatting><x14:cfRule type="{typ}" priority="1"' + (f' operator="{op}"' if op else "")
            + f' id="{{00000000-0000-0000-0000-000000000001}}"><xm:f>{escape(formula)}</xm:f>'
            f'<x14:dxf><numFmt numFmtId="300" formatCode={quoteattr(numfmt)}/></x14:dxf></x14:cfRule>'
            f'<xm:sqref>{sqref}</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext></extLst>')


def run(path):
    return Engine(path, [C66()]).run()[C66.key]


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
        msg = str(e) + " " + " ".join(str(v) for v in getattr(e, "failures", {}).values())
        for n in needles:
            assert n in msg, (n, msg[:800])
        return e
    raise AssertionError(f"expected GradingError containing {needles}")


def locs(v):
    return [m["location"] for m in v["mistakes"]]


def one(code_or_id, value=0, t=None, f=None, **kw):
    """Workbook with one cell A1 under a format; returns the verdict."""
    st = Styles()
    s = st.xf(code_or_id)
    return run(book([("S", sheet([c("A1", s, value, t=t, f=f)]))], st, **kw))


# ============================================================================ pure functions
def test_classify_formats():
    dash = ['#,##0;(#,##0);\\-', '#,##0.00;\\(#,##0.00\\);\\-', '0;(0);"-"', '#,##0;-#,##0;"–"', '0;0;—',
            '_(* #,##0_);_(* (#,##0);_(* "-"??_);_(@_)', '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)',
            '[>=0.005]#,##0.00;[<=-0.005]\\(#,##0.00\\);\\-', '[=0]"-";#,##0', '0.00E+00;(0.00E+00);"-"',
            '0.0%;(0.0%);"-"', '#,##0;(#,##0);* -']
    digit = ['General', '0', '#,##0.00', '#,##0;(#,##0)', '0%', '0.0%', '"$"#,##0.00', '0.00E+00', '@',
             '#,##0;[Red](#,##0)', '[>=1E11]"big";#,##0', '0;-0;0', '0.0x', '# ?/?']
    blank = [';;;', '#,##0;(#,##0);', '0;-0;;@', '#,###']
    for x in dash:
        assert classify(0.0, x)[0] == DASH, (x, classify(0.0, x))
    for x in digit:
        assert classify(0.0, x)[0] == DIGIT, (x, classify(0.0, x))
    for x in blank:
        assert classify(0.0, x)[0] == BLANK, (x, classify(0.0, x))
    assert classify(0.0, '0;-0;"nil"')[0] == TEXT
    for x in ('d-mmm-yy', 'm/d/yyyy', 'h:mm', '[h]:mm:ss', 'mmm yyyy'):
        assert classify(0.0, x)[0] == DATETIME, x
    assert classify(0.0, "")[0] == DIGIT                     # empty code = General (Excel 2026-10-03)
    # non-zero values: a residue is not a zero; it only "displays as zero"
    assert classify(2.33e-10, '[>=0.005]#,##0.00;[<=-0.005]\\(#,##0.00\\);\\-')[0] == NONZERO
    assert classify(1e-12, '#,##0.00')[0] == DIGIT          # displays 0.00 (counted only if DISPLAYED_ZERO_COUNTS)
    assert classify(1e-12, 'General')[0] == NONZERO          # '1E-12'
    assert classify(5.0, '#,##0')[0] == NONZERO
    # the policy table
    assert fails(DIGIT) and fails(TEXT) and not fails(DASH) and not fails(BLANK) and not fails(DATETIME)
    assert not fails(NONZERO) and not fails(HIDDEN)


def test_builtin_ids():
    for i in (41, 42, 43, 44):
        assert classify(0.0, resolve_format(i))[0] == DASH, i
    for i in (0, 1, 2, 3, 4, 9, 10, 11, 37, 38, 39, 40, 48, 49):
        assert classify(0.0, resolve_format(i))[0] == DIGIT, i
    # in a workbook: no <numFmt> entry, the built-in table decides (66 T3 trap)
    for i in (43, 44):
        assert one(i)["decision"] == "pass", i
    for i in (40, 37, 3, 9):
        assert one(i)["decision"] == "fail", i


def test_cf_rule_evaluation():
    R = lambda **kw: CfRule(**kw)   # noqa: E731
    assert fires(R(type="cellIs", operator="equal", formulas=["0"]), 0.0) is True
    assert fires(R(type="cellIs", operator="notEqual", formulas=["0"]), 0.0) is False
    assert fires(R(type="cellIs", operator="between", formulas=["-0.005", "0.005"]), 0.0) is True
    assert fires(R(type="cellIs", operator="between", formulas=["1", "-1"]), 0.0) is True     # min/max
    assert fires(R(type="cellIs", operator="greaterThan", formulas=["100"]), 0.0) is False
    assert fires(R(type="cellIs", operator="equal", formulas=['"0"']), 0.0) is False        # text is not the number
    # another cell, no value source given (env None): cannot be evaluated - read through one in test_cfeval.py
    assert is_unevaluable(fires(R(type="cellIs", operator="equal", formulas=["$B$1"]), 0.0))
    assert fires(R(type="containsBlanks"), 0.0) is False
    assert fires(R(type="notContainsErrors"), 0.0) is True
    assert fires(R(type="containsText", text="0"), 0.0) is True
    assert fires(R(type="expression", formulas=["TRUE"]), 0.0) is True
    assert fires(R(type="expression", formulas=["0"]), 0.0) is False
    # self comparisons, relative to the first range's top-left cell (anchor)
    assert fires(R(type="expression", formulas=["B2=0"]), 0.0, (5, 3, 2, 2)) is True        # shifted to C5
    assert fires(R(type="expression", formulas=["ABS(B2)<0.001"]), 0.0, (2, 2, 2, 2)) is True
    assert fires(R(type="expression", formulas=["ROUND($B2,2)=0"]), 0.0, (2, 2, 2, 2)) is True
    assert fires(R(type="expression", formulas=["0<>B2"]), 0.0, (2, 2, 2, 2)) is False
    assert is_unevaluable(fires(R(type="expression", formulas=["$A$1=0"]), 0.0, (2, 2, 2, 2)))    # another cell
    assert is_unevaluable(fires(R(type="expression", formulas=["B2=C2"]), 0.0, (2, 2, 2, 2)))
    assert fires(R(type="expression", formulas=["B2=0"]), 0.0, None) is UNKNOWN          # position unknown
    assert is_unevaluable(fires(R(type="top10", rank=10), 0.0))    # Patrick 2026-10-05: taken as off
    assert fires(R(type="dataBar"), 0.0) is True
    assert literal('"a""b"') == 'a"b' and literal("1E-3") == 0.001 and literal("x") is UNKNOWN


def test_rubric_key():
    with open(RUBRIC) as fh:
        rub = json.load(fh)
    keys = [f"{cat}/{it['name']}" for cat, items in rub.items() for it in items]
    assert keys[66 - 1] == C66.key, (keys[66 - 1], C66.key)
    assert REGISTRY[66] is C66


# ============================================================================ workbook level
def test_general_zero_constant_and_formula_fail():
    st = Styles()
    cells = [c("A1", 0, 0), c("A2", 0, 0, f="B1-B1"), c("B1", 0, 7)]
    v = run(book([("Model", sheet(cells))], st))
    assert v["decision"] == "fail" and locs(v) == ["Model!A1:A2"], v["mistakes"]
    assert "'0'" in v["mistakes"][0]["description"] and "General" in v["mistakes"][0]["description"]
    assert v["stats"]["zero_cells_failing"] == 2


def test_shared_child_and_spill_member_zero():
    """66 T2 trap: the failing zero is a shared-formula child with no formula text; T1: spill members."""
    st = Styles()
    d = st.xf('#,##0.00;\\(#,##0.00\\);\\-')
    cells = [c("A1", d, 0, f="B1*0", si=0), c("A2", d, 0, f="", si=0), c("A3", 0, 0, f="", si=0),
             c("A4", d, 0, f="", si=0)]
    v = run(book([("S", sheet(cells))], st))
    assert locs(v) == ["S!A3"], v["mistakes"]
    spill = (f'<sheetData><row r="1"><c r="C1" s="{d}" cm="1"><f t="array" ref="C1:C3">SEQUENCE(3,1,0,0)</f>'
             f'<v>0</v></c></row><row r="2"><c r="C2" s="{d}"><v>0</v></c></row>'
             f'<row r="3"><c r="C3"><v>0</v></c></row></sheetData>')
    v = run(book([("S", spill)], st))
    assert locs(v) == ["S!C3"], v["mistakes"]


def test_dash_blank_text_datetime_percent():
    assert one('#,##0;(#,##0);\\-')["decision"] == "pass"
    assert one('_(* #,##0_);_(* (#,##0);_(* "-"??_);_(@_)')["decision"] == "pass"
    assert one('[=0]"-";#,##0')["decision"] == "pass"
    assert one('[>=1E11]"big";#,##0')["decision"] == "fail"       # 2nd section shows 0
    assert one('#,##0;(#,##0)')["decision"] == "fail"             # 2 sections: 0 uses the 1st
    assert one(';;;')["decision"] == "pass"                       # blank: check 94's business
    assert one('#,##0;(#,##0);')["decision"] == "pass"
    v = one('0;-0;"nil"')
    assert v["decision"] == "fail" and "'nil'" in v["mistakes"][0]["description"]
    assert one('0%')["decision"] == "fail"                         # zero rate: plain rule
    assert one('0.0%;(0.0%);"-"')["decision"] == "pass"
    assert one('0.00E+00;(0.00E+00);"-"')["decision"] == "pass"
    v = one('d-mmm-yy')
    assert v["decision"] == "pass" and v["stats"]["zero_cells_by_display"].get("base_datetime") == 1
    assert one('h:mm')["decision"] == "pass" and one('[h]:mm')["decision"] == "pass"
    assert one('@')["decision"] == "fail"                          # a number under '@' shows General '0'


def test_not_numeric_zeros():
    st = Styles()
    cells = [c("A1", 0, False), c("A2", 0, False, f="1>2"), c("A3", 0, "0"), c("A4", 0, 0, t="s"),
             c("A5", 0, "#DIV/0!", t="e"), c("A6", 0, "", f='IF(1,"","")'), c("A7"), c("A8", 0, "0", f='"0"'),
             c("A9", 0, "2024-01-01T00:00:00", t="d")]
    v = run(book([("S", sheet(cells))], st, sst=["0"]))
    assert v["decision"] == "pass", v["mistakes"]


def test_exact_zero_only():
    """Residues are not zeros (DISPLAYED_ZERO_COUNTS False); -0 is a zero."""
    st = Styles()
    g = st.xf('#,##0.00')
    cells = [c("A1", g, 1e-12), c("A2", g, -2e-13), c("A3", g, 0.0004), c("A4", 0, 3.6e-12)]
    assert run(book([("S", sheet(cells))], st))["decision"] == "pass"
    v = run(book([("S", '<sheetData><row r="1"><c r="A1"><v>-0</v></c></row></sheetData>')], Styles()))
    assert v["decision"] == "fail"


def test_counters_and_switches_fail():
    """Patrick 2026-10-02: no exemption for period counters or 1/0 switches."""
    st = Styles()
    sw = st.xf("0")
    cells = [c("A1", 0, "Period #"), c("B1", 0, 0), c("C1", 0, 1, f="B1+1"), c("D1", 0, 2, f="C1+1"),
             c("A2", 0, "Include? (1 = yes)"), c("B2", sw, 0)]
    v = run(book([("Model", sheet(cells))], st))
    assert sorted(locs(v)) == ["Model!B1", "Model!B2"], v["mistakes"]


def test_hidden_sheet_row_column_count():
    st = Styles()
    cells = [c("A1", 0, 0)]
    v = run(book([("Vis", sheet([c("A1", 0, 5)])), ("Hid", sheet(cells))], st, states={1: "hidden"}))
    assert locs(v) == ["Hid!A1"] and "hidden sheet" in v["mistakes"][0]["description"]
    assert v["stats"]["hidden_sheets_with_failures"] == ["Hid"]
    v = run(book([("S", sheet([c("A3", 0, 0)], rows_attr={3: ' hidden="1"'},
                                head='<cols><col min="2" max="2" hidden="1" width="0"/></cols>'))], st))
    assert locs(v) == ["S!A3"]
    v = run(book([("S", sheet([c("B1", 0, 0)], head='<cols><col min="2" max="2" hidden="1" width="0"/></cols>'))], st))
    assert locs(v) == ["S!B1"]


def test_show_zeros_off_and_merges():
    st = Styles()
    head = '<sheetViews><sheetView showZeros="0" workbookViewId="0"/></sheetViews>'
    v = run(book([("S", sheet([c("A1", 0, 0)], head=head)), ("T", sheet([c("A1", 0, 0)]))], st))
    assert locs(v) == ["T!A1"] and v["stats"]["show_zeros_off_sheets"] == ["S"]
    # a value hidden under a merge (not the anchor) is not displayed; the anchor is judged
    tail = '<mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
    v = run(book([("S", sheet([c("A1", 0, "Label"), c("B1", 0, 0), c("C1", 0, 0), c("A2", 0, 0)], tail=tail))], st))
    assert locs(v) == ["S!A2"] and v["stats"]["zero_cells_by_display"]["merged_non_anchor"] == 2
    v = run(book([("S", sheet([c("A1", 0, 0)], tail=tail))], st))
    assert locs(v) == ["S!A1"]


def test_conditional_format_number_formats():
    # base digit format, CF (=0) applies a dash: Excel shows a dash -> pass (main and x14)
    st = Styles()
    g = st.xf("#,##0.00")
    dx = st.dxf('"-"')
    cells = [c("A1", g, 0), c("A2", g, 0, f="B2*0"), c("B2", 0, 3)]
    tail = cf_main("A1:A5", ("cellIs", dx, "equal", ["0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "pass", v["mistakes"]
    v = run(book([("S", sheet(cells, tail=cf_x14("A1:A5", "cellIs", "equal", "0", '"-"')))], st))
    assert v["decision"] == "pass", v["mistakes"]
    # self-comparison expression with ABS(): evaluated
    tail = cf_main("A1:A5", ("expression", dx, None, ["ABS(A1)<0.0001"], ""))
    assert run(book([("S", sheet(cells, tail=tail))], st))["decision"] == "pass"
    # a rule that does not fire on 0 leaves the digit
    tail = cf_main("A1:A5", ("cellIs", dx, "greaterThan", ["100"], ""))
    assert locs(run(book([("S", sheet(cells, tail=tail))], st))) == ["S!A1:A2"]
    # a rule reading another cell is evaluated through the value source (Patrick 2026-10-05; until then
    # GradingError): Z1 empty -> off -> the digits show; Z1 = 1 -> the dash shows
    tail = cf_main("A1:A5", ("expression", dx, None, ["$Z$1=1"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert locs(v) == ["S!A1:A2"] and v["stats"]["cf_assumptions"]["cells"] == 0, v["stats"]
    assert run(book([("S", sheet(cells + [c("Z1", 0, 1)], tail=tail))], st))["decision"] == "pass"
    # a rule that cannot be evaluated is OFF (the cell's own format decides) and recorded as decisive
    tail = cf_main("A1:A5", ("expression", dx, None, ["COUNTIF($Z$1:$Z$9,1)>0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    a = v["stats"]["cf_assumptions"]
    assert locs(v) == ["S!A1:A2"] and a["cells"] == 2 and a["decisive_cells"] == 2, a
    assert "COUNTIF" in a["examples"][0] and a["unevaluable_rules"] == 1, a
    # ...not decisive when the CF format also shows a digit
    st2 = Styles()
    g2 = st2.xf("#,##0.00")
    dx2 = st2.dxf("0.0")
    tail = cf_main("A1:A5", ("expression", dx2, None, ["COUNTIF($Z$1:$Z$9,1)>0"], ""))
    v = run(book([("S", sheet([c("A1", g2, 0)], tail=tail))], st2))
    assert locs(v) == ["S!A1"] and v["stats"]["cf_assumptions"]["decisive_cells"] == 0, v["stats"]["cf_assumptions"]


def test_conditional_format_turns_dash_into_digit_second_pass():
    st = Styles()
    d = st.xf('#,##0;(#,##0);\\-')
    dx = st.dxf("0.00")
    cells = [c("A1", d, 0), c("A2", d, 0, f="B2*0"), c("A3", d, 5), c("B2", 0, 3), c("C1", d, 0)]
    tail = cf_main("A1:A5", ("cellIs", dx, "equal", ["0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert locs(v) == ["S!A1:A2"], v["mistakes"]
    assert "conditional-format number format '0.00'" in v["mistakes"][0]["description"]
    assert v["stats"]["cf_second_pass_sheets"] == ["S"]
    # a stopIfTrue rule above it stops the digit format
    st = Styles()
    d = st.xf('#,##0;(#,##0);\\-')
    dx_none = st.dxf('#,##0;(#,##0);\\-')
    dx = st.dxf("0.00")
    tail = cf_main("A1:A5", ("cellIs", dx_none, "equal", ["0"], ' stopIfTrue="1"'), ("cellIs", dx, "equal", ["0"], ""))
    assert run(book([("S", sheet([c("A1", d, 0)], tail=tail))], st))["decision"] == "pass"


def test_data_bar_hiding_value():
    st = Styles()
    tail = ('<conditionalFormatting sqref="A1:A3"><cfRule type="dataBar" priority="1"><dataBar showValue="0">'
            '<cfvo type="min"/><cfvo type="max"/><color rgb="FF638EC6"/></dataBar></cfRule></conditionalFormatting>')
    assert run(book([("S", sheet([c("A1", 0, 0)], tail=tail))], st))["decision"] == "pass"
    tail = tail.replace('showValue="0"', 'showValue="1"')
    assert run(book([("S", sheet([c("A1", 0, 0)], tail=tail))], st))["decision"] == "fail"


def test_values_read_only_where_needed():
    """openpyxl-labelled file: formula caches untrusted.  A formula under a dash format needs no
    value (passes); one under General needs its value -> GradingError (no fallback)."""
    app = "Microsoft Excel Compatible / Openpyxl 3.1.5"
    st = Styles()
    d = st.xf('#,##0;(#,##0);\\-')
    v = run(book([("S", sheet([c("A1", d, 0, f="B1*0"), c("B1", 0, 4), c("C1", d, None, f="B1")]))], st, app=app))
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 0
    graded("untrusted_value", lambda: run(book([("S", sheet([c("A1", 0, 0, f="B1*0"), c("B1", 0, 4)]))], st, app=app)),
           "untrusted", "S!A1")
    # constants are always readable
    assert locs(run(book([("S", sheet([c("A1", 0, 0)]))], st, app=app))) == ["S!A1"]


def test_unreadable_formats_raise_only_when_needed():
    st = Styles()
    bad = st.xf(65)                     # numFmtId 65: no <numFmt> and no built-in meaning
    assert run(book([("S", sheet([c("A1", bad, "text"), c("A2", bad, 5)]))], st))["decision"] == "pass"
    raises(lambda: run(book([("S", sheet([c("A1", bad, 0)]))], st)), "number format cannot be read")
    st = Styles()
    e = st.xf("")                       # empty formatCode = General (Excel 2026-10-03): a '0', no raise
    assert locs(run(book([("S", sheet([c("A1", e, 0)]))], st))) == ["S!A1"]


def test_grouping_cap_and_order():
    st = Styles()
    cells = [c(f"{col}{r}", 0, 0) for r in range(1, 4) for col in "BCD"] + [c("A1", 0, 0)]
    cells += [c(f"F{r}", 0, 0) for r in range(1, 61, 2)]           # 30 separate cells
    v = run(book([("S", sheet(cells))], st))
    assert v["decision"] == "fail" and v["stats"]["n_mistakes"] == 32 and v["stats"]["mistakes_truncated"]
    assert len(v["mistakes"]) == 25 and v["stats"]["zero_cells_failing"] == 40
    assert locs(v)[:3] == ["S!A1:D1", "S!F1", "S!B2:D3"], locs(v)[:3]     # row runs, then stacked


# ============================================================================ review fixes (2026-10-03)
APP_OPX = "Microsoft Excel Compatible / Openpyxl 3.1.5"
HEAD_SZ = '<sheetViews><sheetView showZeros="0" workbookViewId="0"/></sheetViews>'
DATABAR = ('<conditionalFormatting sqref="{sq}"><cfRule type="dataBar" priority="1"><dataBar showValue="0">'
           '<cfvo type="min"/><cfvo type="max"/><color rgb="FF638EC6"/></dataBar></cfRule></conditionalFormatting>')


class _switch:
    """Temporarily set a module-level policy switch of c66."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(c66, k)
            setattr(c66, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(c66, k, v)


def test_strict_dash_display():
    """66-R3: a zero section that merely CONTAINS a dash is text, not a dash (STRICT_DASH)."""
    # '0;-0;"-x"': a unit label the numbers do not carry is text (66-S1 moved '0.0"x";(0.0"x");"-x"', whose
    # positive section prints the 'x' too, to the dash list below: see test_review2_decorated_dash)
    for x in ('0;-0;"n-a"', '0;-0;"zero-rated"', '0;-0;"N/A - nil"', '#,##0;(#,##0);"nil"*-', '0;-0;"—none—"',
              '0;-0;"-x"', '0;-0;"-/-"'):
        assert classify(0.0, x)[0] == TEXT, (x, classify(0.0, x))
    for x in ('0;-0;"-"', '0;-0;" - "', '0;-0;"--"', '0;-0;"(-)"', '0;-0;"[-]"', '#,##0;(#,##0);* -', '#,##0;(#,##0);*-',
              '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)',
              '_-[$CHF]\\ * #,##0.00_-;-[$CHF]\\ * #,##0.00_-;_-[$CHF]\\ * "-"??_-;_-@_-',
              '_-* #,##0.00\\ "€"_-;\\-* #,##0.00\\ "€"_-;_-* "-"??\\ "€"_-;_-@_-', '#,##0;[Red]-#,##0;\\–', '0;0;—',
              '0;0;"−"', '£#,##0;(£#,##0);£-', '0.0"x";(0.0"x");"-x"'):
        assert classify(0.0, x)[0] == DASH, (x, classify(0.0, x))
    v = one('0;-0;"n-a"')
    assert v["decision"] == "fail" and "'n-a'" in v["mistakes"][0]["description"], v["mistakes"]
    with _switch(STRICT_DASH=False):
        assert classify(0.0, '0;-0;"n-a"')[0] == DASH and one('0;-0;"n-a"')["decision"] == "pass"


def test_untrusted_values_needed_only_after_the_tail():
    """66-R1: in an openpyxl-labelled file (caches untrusted) a formula under a digit format needs
    its value only if a zero there would still fail once the sheet tail is known."""
    st = Styles()
    g = st.xf("#,##0.00")
    dx = st.dxf('"-"')
    f0 = [c("A1", 0, 0, f="B1*0"), c("B1", 0, 4)]
    # (a) showZeros=0: every zero is hidden -> no value needed
    v = run(book([("S", sheet(f0, head=HEAD_SZ))], st, app=APP_OPX))
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 0, v
    # (b) a CF rule (=0 -> '-') over General formulas: any zero shows a dash
    v = run(book([("S", sheet(f0, tail=cf_main("A1:A5", ("cellIs", dx, "equal", ["0"], ""))))], st, app=APP_OPX))
    assert v["decision"] == "pass" and v["stats"]["untrusted_values_not_needed"] == 1, v
    # (c) a data bar hiding the value
    v = run(book([("S", sheet(f0, tail=DATABAR.format(sq="A1:A3")))], st, app=APP_OPX))
    assert v["decision"] == "pass", v
    # (d) a formula in a non-anchor merged cell is not displayed
    tail = '<mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
    cells = [c("A1", 0, "Label"), c("B1", g, 0, f="D1*0"), c("D1", 0, 4)]
    assert run(book([("S", sheet(cells, tail=tail))], st, app=APP_OPX))["decision"] == "pass"
    # controls: the tail does not rescue the cell -> GradingError naming it (no fallback)
    graded("untrusted_value", lambda: run(book([("S", sheet(f0 + [c("A3", g, 0, f="B1*0")], tail=tail))], st, app=APP_OPX)),
           "untrusted", "S!A1")
    tail_gt = cf_main("A1:A5", ("cellIs", dx, "greaterThan", ["100"], ""))
    graded("untrusted_value", lambda: run(book([("S", sheet(f0, tail=tail_gt))], st, app=APP_OPX)), "untrusted", "S!A1", "'General'")
    graded("untrusted_value", lambda: run(book([("S", sheet(f0))], st, app=APP_OPX)), "untrusted", "S!A1")
    # a dash format still needs no value at all
    d = st.xf('#,##0;(#,##0);\\-')
    v = run(book([("S", sheet([c("A1", d, 0, f="B1*0"), c("B1", 0, 4)]))], st, app=APP_OPX))
    assert v["decision"] == "pass" and v["stats"]["untrusted_values_not_needed"] == 0


def test_untrusted_values_beyond_buffer_second_pass():
    """More untrusted cells than MAX_DEFERRED: the rest are re-checked in a second pass."""
    st = Styles()
    tail = '<mergeCells count="4">' + "".join(f'<mergeCell ref="A{r}:B{r}"/>' for r in range(1, 5)) + "</mergeCells>"
    rescued = [c(f"B{r}", 0, 0, f="C1*0") for r in range(1, 5)] + [c("C1", 0, 4)]
    with _switch(MAX_DEFERRED=2):
        v = run(book([("S", sheet(rescued, tail=tail))], st, app=APP_OPX))
        assert v["decision"] == "pass" and v["stats"]["deferred_second_pass_sheets"] == ["S"], v
        bad = rescued + [c("D4", 0, 0, f="C1*0")]
        graded("untrusted_value", lambda: run(book([("S", sheet(bad, tail=tail))], st, app=APP_OPX)), "untrusted", "S!D4")
        bad = rescued + [c("D1", 0, 0, f="C1*0")]      # within the buffer: raised at sheet end
        graded("untrusted_value", lambda: run(book([("S", sheet(bad, tail=tail))], st, app=APP_OPX)), "untrusted", "S!D1")


def test_hidden_zeros_are_their_own_class():
    """66-R2: zeros hidden by showZeros=0 or a hidden-value data bar are HIDDEN (check 94 does not
    charge them), distinct from a format blank (BLANK, charged by 94).  Both pass by default."""
    st = Styles()
    g = st.xf("#,##0.00")
    blank = st.xf(";;;")
    sz = sheet([c("A1", g, 0), c("A2", 0, 0), c("A3", blank, 0)], head=HEAD_SZ)
    db = sheet([c("A1", g, 0)], tail=DATABAR.format(sq="A1:A3"))
    v = run(book([("S", sz)], st))
    assert v["decision"] == "pass" and v["stats"]["zero_cells_by_display"] == {"base_blank": 1, "base_hidden": 2}, v
    v = run(book([("S", db)], st))
    assert v["decision"] == "pass" and v["stats"]["zero_cells_by_display"] == {"judged_hidden": 1}, v
    with _switch(HIDDEN_ZERO_PASSES=False):
        v = run(book([("S", sz)], st))
        assert locs(v) == ["S!A1", "S!A2"] and "hide-zeros option" in v["mistakes"][0]["description"], v["mistakes"]
        assert locs(run(book([("S", db)], st))) == ["S!A1"]
        # a format blank stays BLANK (BLANK_ZERO_PASSES) on a normal sheet
        assert run(book([("S", sheet([c("A1", blank, 0)]))], st))["decision"] == "pass"
    with _switch(BLANK_ZERO_PASSES=False):
        v = run(book([("S", sheet([c("A1", blank, 0)]))], st))
        assert locs(v) == ["S!A1"] and "display nothing" in v["mistakes"][0]["description"], v["mistakes"]


def test_merge_index():
    """66-R4: one bisect per cell (was a scan of every merge in the column); overlapping merges
    (an invalid file) fall back to a scan."""
    class Tail:
        def __init__(self, refs):
            from detchecks.core.refs import parse_range
            self.merges = []
            for ref in refs:
                r1, c1, r2, c2 = parse_range(ref)
                m = type("M", (), {})()
                m.r1, m.c1, m.r2, m.c2, m.is_single_cell = r1, c1, r2, c2, (r1 == r2 and c1 == c2)
                self.merges.append(m)
    idx = C66._merge_index(Tail(["A1:A3", "A5:B6", "B8:C8", "D4"]))
    hid = C66._hidden_by_merge
    assert [hid(idx, r, 1) for r in range(1, 8)] == [False, True, True, False, False, True, False]
    assert hid(idx, 6, 2) and hid(idx, 5, 2) and not hid(idx, 8, 2) and hid(idx, 8, 3) and not hid(idx, 4, 4)
    idx = C66._merge_index(Tail(["A1:A10", "A3:B4"]))           # overlapping
    assert not idx[1][2] and hid(idx, 3, 1) and hid(idx, 4, 2) and not hid(idx, 1, 1) and not hid(idx, 11, 1)
    st = Styles()
    tail = '<mergeCells count="2"><mergeCell ref="A1:A3"/><mergeCell ref="A5:B6"/></mergeCells>'
    cells = [c(f"A{r}", 0, 0) for r in range(1, 8)] + [c("B6", 0, 0)]
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert locs(v) == ["S!A1", "S!A4:A5", "S!A7"] and v["stats"]["zero_cells_by_display"]["merged_non_anchor"] == 4, \
        (locs(v), v["stats"]["zero_cells_by_display"])


def test_array_members_missing_from_the_file():
    """Sanity finding: members of an array range with no <c> (only the anchor written) are shown
    by Excel under the row / column style; their values are unknown -> GradingError when a zero
    there would not pass; no error when it would."""
    st = Styles()
    d = st.xf('#,##0;(#,##0);\\-')
    anchor = f'<c r="A1"><f t="array" ref="A1:C1">B9:D9*1</f><v>5</v></c>'
    graded("unwritten_array_member", lambda: run(book([("S", f'<sheetData><row r="1">{anchor}</row></sheetData>')], st)),
           "S!A1:C1", "2 member cell(s) not written in the file", "'General'")
    # the members' column style shows a zero as a dash -> no value needed
    cols = f'<cols><col min="2" max="3" width="9" style="{d}"/></cols>'
    v = run(book([("S", f'{cols}<sheetData><row r="1">{anchor}</row></sheetData>')], st))
    assert v["decision"] == "pass" and v["stats"]["array_members_not_in_file"] == 2, v
    # a custom row style wins over the column style
    row = f'<row r="1" s="{d}" customFormat="1">'
    v = run(book([("S", f'<sheetData>{row}{anchor}</row></sheetData>')], st))
    assert v["decision"] == "pass", v
    # two rows: row 2 has no custom style and the columns are General -> its members may show '0'
    anchor2 = anchor.replace('ref="A1:C1"', 'ref="A1:C2"')
    graded("unwritten_array_member", lambda: run(book([("S", f'<sheetData>{row}{anchor2}</row><row r="2"/></sheetData>')], st)),
           "S!A1:C2", "5 member cell(s)")
    # all members written: nothing missing
    full = (f'<sheetData><row r="1">{anchor}<c r="B1"><v>1</v></c><c r="C1"><v>2</v></c></row></sheetData>')
    v = run(book([("S", full)], st))
    assert v["decision"] == "pass" and v["stats"]["array_members_not_in_file"] == 0, v
    # a hide-zeros sheet hides any zero there
    v = run(book([("S", f'{HEAD_SZ}<sheetData><row r="1">{anchor}</row></sheetData>')], st))
    assert v["decision"] == "pass", v
    # a dynamic-array anchor (cm=) with its spill members missing
    dyn = '<c r="A1" cm="1"><f t="array" ref="A1:A3">SEQUENCE(3)</f><v>1</v></c>'
    graded("unwritten_array_member", lambda: run(book([("S", f'<sheetData><row r="1">{dyn}</row></sheetData>')], st)),
           "S!A1:A3", "2 member cell(s)")


def test_excel_2026_10_03_show_zeros_off_zero_section():
    """Q5: with showZeros=0 a format with an explicit zero section still prints it: '0.00;-0.00;0.00'
    shows 0.00 (fail), '0;-0;"nil"' shows nil (fail, non-dash text), '#,##0;(#,##0);"-"' a dash
    (pass); General and single-section formats hide the zero (pass); a conditional format is
    unmeasured -> GradingError when it decides."""
    st = Styles()
    three, nil, dash, one = st.xf("0.00;-0.00;0.00"), st.xf('0;-0;"nil"'), st.xf('#,##0;(#,##0);"-"'), st.xf("0.00")
    zero_txt = st.xf('General;General;"zero"')
    cells = [c("A1", three, 0), c("A2", nil, 0), c("A3", dash, 0), c("A4", one, 0), c("A5", 0, 0), c("A6", zero_txt, 0)]
    v = run(book([("S", sheet(cells, head=HEAD_SZ))], st))
    assert locs(v) == ["S!A1", "S!A2", "S!A6"] or sorted(locs(v)) == ["S!A1", "S!A2", "S!A6"], locs(v)
    cond = st.xf('[>0]0;[<0]-0;0')
    graded("unverified_number_format", lambda: run(book([("S", sheet([c("A1", cond, 0)], head=HEAD_SZ))], st)), "showZeros=0", "S!A1")
    assert run(book([("S", sheet([c("A1", cond, 0)]))], st))["decision"] == "fail"      # normal sheet: '0'


def test_excel_2026_10_03_contains_text_wildcards():
    """Q3: containsText uses SEARCH wildcards; a rule with text '0*' fires on the zero (General
    text '0') and its number format (a dash) applies."""
    st = Styles()
    dx = st.dxf('"-"')
    rule = cf_main("A1:A3", ("containsText", dx, "containsText", ['NOT(ISERROR(SEARCH("?",A1)))'], ' text="?"'))
    v = run(book([("S", sheet([c("A1", 0, 0)], tail=rule))], st))
    assert v["decision"] == "pass", locs(v)                                  # '?' matches the one character '0'
    rule = cf_main("A1:A3", ("containsText", dx, "containsText", ['NOT(ISERROR(SEARCH("??",A1)))'], ' text="??"'))
    v = run(book([("S", sheet([c("A1", 0, 0)], tail=rule))], st))
    assert locs(v) == ["S!A1"], locs(v)                                      # '??' needs two characters


# ============================================================================ second review (2026-10-04)
def test_review2_decorated_dash():
    """66-S1: a zero section whose dash sits beside the format's own number decorations - a QUOTED
    currency code (Excel's sv-SE ' -   kr', de-CH 'CHF -', pt-BR 'R$ -', en-ZA 'R -', pl 'zł', cs 'Kč',
    'USD -') or a unit label the positive section prints too ('-x', '-%', '- bps') - is a dash, exactly
    like the same display through '€', '$' or a [$CHF] tag.  A label the numbers do not carry stays text."""
    kr = '_-* #,##0.00\\ "kr"_-;\\-* #,##0.00\\ "kr"_-;_-* "-"??\\ "kr"_-;_-@_-'
    chf_lit = '_ "CHF"\\ * #,##0.00_ ;_ "CHF"\\ * \\-#,##0.00_ ;_ "CHF"\\ * "-"??_ ;_ @_ '
    chf_tag = '_-[$CHF]\\ * #,##0.00_-;-[$CHF]\\ * #,##0.00_-;_-[$CHF]\\ * "-"??_-;_-@_-'
    dash = [kr, chf_lit, chf_tag, '"R$ "#,##0.00;"R$ "\\-#,##0.00;"R$ "\\-', '"USD "#,##0;"USD "(#,##0);"USD "-',
            '"R "#,##0;"R "-#,##0;"R "-', '#,##0\\ "zł";-#,##0\\ "zł";"-"\\ "zł"', '#,##0 "Kč";-#,##0 "Kč";"-" "Kč"',
            '0.0"x";(0.0"x");"-x"', '0.0%;(0.0%);"-"%', '#,##0" bps";(#,##0" bps");"-"" bps"',
            '#,##0.00\\ "€";\\-#,##0.00\\ "€";"-"\\ "€"', '"Total "General;"Total "-General;"Total "-']
    for x in dash:
        assert classify(0.0, x)[0] == DASH, (x, classify(0.0, x))
    # the same display must get the same verdict whatever the authoring of the code
    assert classify(0.0, chf_lit)[1] == classify(0.0, chf_tag)[1] == " CHF -   "
    assert one(chf_lit)["decision"] == one(chf_tag)["decision"] == one(kr)["decision"] == "pass"
    assert one('0.0"x";(0.0"x");"-x"')["decision"] == "pass" and one('#,##0" bps";(#,##0" bps");"-"" bps"')["decision"] == "pass"
    # a label the positive section does not print, a word, a literal-only positive section, a literal with a
    # dash or digit: not decorations -> text (fail)
    text = ['0;-0;"-x"', '0;-0;"USD -"', '0"na";-0"na";"n-a"', '"nil";"nil";"nil"-', '"FY-"0;"FY-"-0;"FY-"-',
            '0;-0;"nil"', '0;-0;"n-a"', '#,##0;(#,##0);"nil"*-']
    for x in text:
        assert classify(0.0, x)[0] == TEXT, (x, classify(0.0, x))
    v = one('0;-0;"-x"')
    assert v["decision"] == "fail" and "'-x'" in v["mistakes"][0]["description"], v["mistakes"]
    # the decorations themselves: positive-section literals / '%' without digits or dashes, longest first
    assert c66.decorations('"R$ "#,##0.00;"R$ "\\-#,##0.00;"R$ "\\-') == ("R$",)
    assert c66.decorations('0.0%;(0.0%);"-"%') == ("%",) and c66.decorations(kr) == ("kr",)
    assert c66.decorations('0;-0;"-x"') == () and c66.decorations('"nil";"nil";"nil"-') == ()
    assert c66.decorations('"FY-"0;"FY-"-0;"FY-"-') == () and c66.decorations("General") == ()
    # digits stay digits whatever the decoration; non-zero values are never dashes
    assert classify(0.0, '0.0"x"')[0] == DIGIT and classify(0.0, '0" bps"')[0] == DIGIT
    assert classify(2.5, '0.0"x";(0.0"x");"-x"')[0] == NONZERO
    with _switch(STRICT_DASH=False):
        assert classify(0.0, kr)[0] == DASH


def test_review2_show_zeros_off_under_cf_format():
    """66-S2: Patrick's 2026-10-03 measurement of showZeros=0 covers BASE formats only.  A zero whose
    display is decided by a conditional format's number format on such a sheet is unmeasured: until
    2026-10-05 a digit / text CF format raised GradingError; now (Patrick 2026-10-05, conditional formats
    never stop a grading) the cell is graded by its own format and the assumption recorded.  A dash /
    blank CF format passes either way."""
    st = Styles()
    dx_digit, dx_dash, dx_nil = st.dxf("0.00"), st.dxf('"-"'), st.dxf('0;-0;"nil"')
    three = st.xf("0.00;-0.00;0.00")
    eq0 = lambda dx: cf_main("A1:A3", ("cellIs", dx, "equal", ["0"], ""))      # noqa: E731
    # General constant 0 (hidden by the sheet option on its own) under a firing CF '0.00': its own format
    # hides it -> pass, recorded as a decisive assumption
    for dx in (dx_digit, dx_nil):
        v = run(book([("S", sheet([c("A1", 0, 0)], head=HEAD_SZ, tail=eq0(dx)))], st))
        a = v["stats"]["cf_assumptions"]
        assert v["decision"] == "pass" and a["cells"] == 1 and a["decisive_cells"] == 1, a
        assert "S!A1" in a["examples"][0] and "graded by its own format" in a["examples"][0], a
    # the same CF rule on a normal sheet is decided: fail under the CF format
    v = run(book([("S", sheet([c("A1", 0, 0)], tail=eq0(dx_digit)))], st))
    assert locs(v) == ["S!A1"] and "conditional-format number format '0.00'" in v["mistakes"][0]["description"]
    # a CF dash / a non-firing CF digit rule: pass (dash or hidden either way; no value needed)
    assert run(book([("S", sheet([c("A1", 0, 0)], head=HEAD_SZ, tail=eq0(dx_dash)))], st))["decision"] == "pass"
    gt = cf_main("A1:A3", ("cellIs", dx_digit, "greaterThan", ["100"], ""))
    v = run(book([("S", sheet([c("A1", 0, 0)], head=HEAD_SZ, tail=gt))], st))
    assert v["decision"] == "pass" and v["stats"]["zero_cells_by_display"] == {"base_hidden": 1}, v["stats"]
    # a base format with an explicit zero section (measured: printed) under a CF dash: pass; under a CF
    # digit: unmeasured -> graded by its own format, which prints '0.00' -> fail (GradingError until 2026-10-05)
    assert run(book([("S", sheet([c("A1", three, 0)], head=HEAD_SZ, tail=eq0(dx_dash)))], st))["decision"] == "pass"
    v = run(book([("S", sheet([c("A1", three, 0)], head=HEAD_SZ, tail=eq0(dx_digit)))], st))
    assert locs(v) == ["S!A1"] and "number format '0.00;-0.00;0.00'" in v["mistakes"][0]["description"], v["mistakes"]
    assert locs(run(book([("S", sheet([c("A1", three, 0)], head=HEAD_SZ))], st))) == ["S!A1"]
    # an untrusted formula there is no longer needed: whatever its value, the CF display gives way to its own
    # format, which hides a zero (it was refused until 2026-10-05)
    f0 = [c("A1", 0, 0, f="B1*0"), c("B1", 0, 4)]
    assert run(book([("S", sheet(f0, head=HEAD_SZ, tail=eq0(dx_digit)))], st, app=APP_OPX))["decision"] == "pass"
    assert run(book([("S", sheet(f0, head=HEAD_SZ, tail=eq0(dx_dash)))], st, app=APP_OPX))["decision"] == "pass"
    # ... but with a base format that prints the zero, the value is needed and refused (not a CF matter)
    f3 = [c("A1", three, 0, f="B1*0"), c("B1", 0, 4)]
    graded("untrusted_value", lambda: run(book([("S", sheet(f3, head=HEAD_SZ, tail=eq0(dx_digit)))], st, app=APP_OPX)), "S!A1")
    # a non-zero value under the CF format is not a zero: pass
    assert run(book([("S", sheet([c("A1", 0, 5)], head=HEAD_SZ, tail=eq0(dx_digit)))], st))["decision"] == "pass"


def test_review2_mixed_date_codes():
    """66-S3: an unquoted unit word ('0 bps', '0 days', '#,##0 d') makes the engine read a date format
    and the check used to leave the zero unjudged as a date; Excel's display of such a code is
    unmeasured -> UNCERTAIN / GradingError for a zero; a non-zero under it is not judged; genuine
    date / time formats (fractional seconds included) stay DATETIME; quoted units stay numeric.
    Since 2026-10-04 the format engine marks such codes unverified (numfmt.mixed_date_letters, every
    render certain=False) and 66's own workaround (c66.mixed_date_section) is gone: the same codes are
    asserted on the core instead."""
    for x in ("0 bps", "0 days", "0 yrs", "0 mths", "#,##0 d", "0.0 d", "? d"):
        assert classify(0.0, x)[0] == UNCERTAIN, (x, classify(0.0, x))
        assert not N.parse_format(x).verified and not N.render(0.0, x).certain, x
    for x in ("h:mm:ss.000", "[h]:mm:ss.00", "mm:ss.0", "d-mmm-yy", 'dd-mmm-yyyy" SUP"', '"FY"yyyy"E PV"',
              "mm/dd/yy\\E", "[h]:mm", "m/d/yyyy h:mm AM/PM"):
        assert classify(0.0, x)[0] == DATETIME, (x, classify(0.0, x))
        assert N.parse_format(x).verified, x
    for x in ('0" bps"', '0 "days"', "0.0x", '#,##0" d"'):
        assert classify(0.0, x)[0] == DIGIT, x
    assert N.parse_format("General").verified
    assert not hasattr(c66, "mixed_date_section")
    graded("unverified_number_format", lambda: one("0 bps"), "S!A1", "'0 bps'", "not verified")
    assert one("0 bps", value=25)["decision"] == "pass"                 # not a zero: never judged
    assert one("0 bps", value="x")["decision"] == "pass"                # text: not numeric
    assert one("h:mm:ss.000")["stats"]["zero_cells_by_display"] == {"base_datetime": 1}
    assert locs(one('0" bps"')) == ["S!A1"]


TESTS = [test_classify_formats, test_builtin_ids, test_cf_rule_evaluation, test_rubric_key,
         test_general_zero_constant_and_formula_fail, test_shared_child_and_spill_member_zero,
         test_dash_blank_text_datetime_percent, test_not_numeric_zeros, test_exact_zero_only,
         test_counters_and_switches_fail, test_hidden_sheet_row_column_count, test_show_zeros_off_and_merges,
         test_conditional_format_number_formats, test_conditional_format_turns_dash_into_digit_second_pass,
         test_data_bar_hiding_value, test_values_read_only_where_needed, test_unreadable_formats_raise_only_when_needed,
         test_grouping_cap_and_order, test_strict_dash_display, test_untrusted_values_needed_only_after_the_tail,
         test_untrusted_values_beyond_buffer_second_pass, test_hidden_zeros_are_their_own_class, test_merge_index,
         test_array_members_missing_from_the_file,
         test_excel_2026_10_03_show_zeros_off_zero_section, test_excel_2026_10_03_contains_text_wildcards,
         test_review2_decorated_dash, test_review2_show_zeros_off_under_cf_format, test_review2_mixed_date_codes]


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
