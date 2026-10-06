"""Unit tests for check 65 (Negatives in parentheses) on synthetic micro-workbooks (raw
SpreadsheetML zips; an openpyxl-labelled app.xml where cache trust matters).

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_65

Plain asserts; also collectable by pytest.  Temporary files go to detchecks/scratch/negs/.
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
from detchecks.checks import REGISTRY, c65
from detchecks.checks._cfeval import is_unevaluable
from detchecks.checks.c65 import (BLANK, C65, DATETIME, HIDDEN, MINUS, MIXED, NEG, NONNEG, PARENS, TEXT, UNCERTAIN,
                                  UNKNOWN, UNSIGNED, ZERO, classify, fails, fires, fixed_pass, strip_label_literals)
from detchecks.core.numfmt import resolve_format
from detchecks.core.sheet import CfRule
from detchecks.errors import GradingError

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "negs")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WS = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"
RUBRIC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "prompts", "rubrics", "rubric_9.json")
APP_OPX = "Microsoft Excel Compatible / Openpyxl 3.1.5"
DATABAR = ('<conditionalFormatting sqref="{sq}"><cfRule type="dataBar" priority="1"><dataBar showValue="0">'
           '<cfvo type="min"/><cfvo type="max"/><color rgb="FF638EC6"/></dataBar></cfRule></conditionalFormatting>')

_TMP = None
_N = [0]


def tmp(name: str = "") -> str:
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_negs_", dir=SCRATCH)
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
    if t == "d":
        return f'<c {attrs} t="d"><v>{escape(v)}</v></c>'
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
    caches written here are trusted); app=APP_OPX labels the file openpyxl (caches untrusted)."""
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


def run(path, value_path=None):
    return Engine(path, [C65()], value_path=value_path).run()[C65.key]


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


def one(code_or_id, value=-5, t=None, f=None, **kw):
    """Workbook with one cell A1 under a format; returns the verdict."""
    st = Styles()
    s = st.xf(code_or_id)
    return run(book([("S", sheet([c("A1", s, value, t=t, f=f)]))], st, **kw))


class _switch:
    """Temporarily set a module-level policy switch of c65."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(c65, k)
            setattr(c65, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(c65, k, v)


# ============================================================================ pure functions
def test_classify_formats():
    parens = ['#,##0;(#,##0)', '#,##0.00;\\(#,##0.00\\)', '0;"("0")"', '#,##0;[Red](#,##0)', '#,##0;[Red]\\(#,##0\\)',
              '#,##0_);(#,##0)', '_(* #,##0_);_(* \\(#,##0\\);_(* "-"??_);_(@_)',
              '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)', '"$"#,##0.00_);[Red]\\("$"#,##0.00\\)',
              '0.00%;(0.00%)', '0.0%;(0.0%);"-"', '0.00E+00;(0.00E+00);"-"', 'General;(General)',
              'General;[Red](General)', '0;(General)', '[<0](#,##0);#,##0', '0;(0);0;@', '(0);(0)',
              '[$€-407]#,##0;\\([$€-407]#,##0\\)', '#,##0;(#,##0);\\-']
    minus = ['General', '0', '#,##0', '#,##0.00', '0%', '0.00%', '"$"#,##0.00', '0.00E+00', '@', '# ?/?', '0.0x',
             '#,##0;-#,##0', '#,##0;[Red]-#,##0', '[Blue]0;[Red]-0', '_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-',
             '0;-0;0', '']
    unsigned = ['#,##0;[Red]#,##0', '0;0', 'General;General', '[>=1E11]"big";#,##0', '[>0]0;0', '0;(0']
    mixed = ['#,##0;-(#,##0)', '#,##0.00;(#,##0.00)-', '0;"-"(0)', '0;(0)"-"']
    for x in parens:
        assert classify(-1006.22, x)[0] == PARENS, (x, classify(-1006.22, x))
    for x in minus:
        assert classify(-1006.22, x)[0] == MINUS, (x, classify(-1006.22, x))
    for x in unsigned:
        assert classify(-1006.22, x)[0] == UNSIGNED, (x, classify(-1006.22, x))
    for x in mixed:
        assert classify(-5.0, x)[0] == MIXED, (x, classify(-5.0, x))
    assert classify(-5.0, '[<-1000](0);0')[0] == UNSIGNED and classify(-1006.22, '[<-1000](0);0')[0] == PARENS
    assert classify(-5.0, '#,##0;;')[0] == BLANK
    assert classify(-5.0, ';;;')[0] == BLANK
    for x in ('#,##0;"neg"', '#,##0;\\-', '0;"n/a"'):
        assert classify(-5.0, x)[0] == TEXT, x
    for x in ('d-mmm-yy', 'm/d/yyyy', 'h:mm', '[h]:mm:ss', 'mmm yyyy', 'mm:ss'):
        assert classify(-5.0, x)[0] == DATETIME, x
    # rounding to zero on display is not a displayed negative
    assert classify(-0.3, '#,##0')[0] == ZERO and classify(-0.004, '0.00')[0] == ZERO
    assert classify(-0.004, '0.00;(0.00)')[0] == ZERO
    assert classify(-0.004, '0.000')[0] == MINUS                # '-0.004'
    assert classify(-1006.22, '0.0,,"M"')[0] == ZERO and classify(-2.5e6, '0.0,,"M"')[0] == MINUS   # '-0.0M' / '-2.5M'
    # the threshold: leftovers above -1e-6 are not negatives
    assert classify(-1e-13, 'General')[0] == NONNEG and classify(-9.9e-7, 'General')[0] == NONNEG
    assert classify(-1.34e-6, 'General')[0] == MINUS            # '-1.34E-06' (attempt 1985)
    assert classify(5.0, '#,##0')[0] == NONNEG and classify(0.0, 'General')[0] == NONNEG
    # percent scaling, scientific
    assert classify(-0.05, '0.00%')[1] == '-5.00%' and classify(-0.05, '0.00%;(0.00%)')[1] == '(5.00%)'
    assert classify(-0.000381, '0.00E+00;(0.00E+00);"-"')[1] == '(3.81E-04)'
    # the policy table
    assert fails(MINUS) and fails(UNSIGNED) and fails(MIXED)
    assert not fails(PARENS) and not fails(TEXT) and not fails(BLANK) and not fails(HIDDEN)
    assert not fails(DATETIME) and not fails(ZERO) and not fails(NONNEG)
    assert fixed_pass(PARENS) and fixed_pass(DATETIME) and fixed_pass(BLANK) and fixed_pass(TEXT)
    assert not fixed_pass(MINUS) and not fixed_pass(ZERO) and not fixed_pass(UNCERTAIN)


def test_builtin_ids():
    for i in (5, 6, 7, 8, 37, 38, 39, 40, 41, 42, 43, 44):
        assert classify(-5.0, resolve_format(i))[0] == PARENS, i
    for i in (0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 48, 49):
        assert classify(-5.0, resolve_format(i))[0] == MINUS, i
    for i in (14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47):
        assert classify(-5.0, resolve_format(i))[0] == DATETIME, i
    # in a workbook: no <numFmt> entry, the built-in table decides
    for i in (5, 8, 37, 40, 42, 44):
        assert one(i)["decision"] == "pass", i
    for i in (0, 3, 4, 9, 10, 48, 49):
        assert one(i)["decision"] == "fail", i
    for i in (14, 22, 45):
        assert one(i)["decision"] == "pass", i


def test_cf_rule_evaluation():
    R = lambda **kw: CfRule(**kw)   # noqa: E731
    # concrete values
    assert fires(R(type="cellIs", operator="lessThan", formulas=["0"]), -5.0) is True
    assert fires(R(type="cellIs", operator="lessThan", formulas=["0"]), 5.0) is False
    assert fires(R(type="cellIs", operator="between", formulas=["-10", "-1"]), -5.0) is True
    assert fires(R(type="cellIs", operator="between", formulas=["-10", "-1"]), -50.0) is False
    assert fires(R(type="cellIs", operator="equal", formulas=['"x"']), -5.0) is False
    assert fires(R(type="cellIs", operator="lessThan", formulas=["A1"]), -5.0) is UNKNOWN     # position unknown
    assert fires(R(type="containsText", text="5"), -5.0) is True
    assert fires(R(type="expression", formulas=["B2<0"]), -5.0, (2, 2, 2, 2)) is True
    assert fires(R(type="expression", formulas=["0>B2"]), -5.0, (3, 2, 2, 2)) is True      # B2 shifted to row 3 = the cell
    # another cell with no value source given (env None): cannot be evaluated - read through one in test_cfeval.py
    assert is_unevaluable(fires(R(type="expression", formulas=["0>C2"]), -5.0, (2, 2, 2, 2)))
    assert is_unevaluable(fires(R(type="expression", formulas=["$B$2<0"]), -5.0, (3, 2, 2, 2)))
    assert fires(R(type="expression", formulas=["TRUE"]), -5.0) is True
    assert is_unevaluable(fires(R(type="top10"), -5.0))          # Patrick 2026-10-05: taken as off
    assert fires(R(type="dataBar"), -5.0) is True
    # an unknown negative (NEG): fires for every negative / for none / depends on which
    assert fires(R(type="cellIs", operator="lessThan", formulas=["0"]), NEG) is True
    assert fires(R(type="cellIs", operator="lessThan", formulas=["100"]), NEG) is True
    assert fires(R(type="cellIs", operator="lessThan", formulas=["-100"]), NEG) is UNKNOWN
    assert fires(R(type="cellIs", operator="greaterThan", formulas=["0"]), NEG) is False
    assert fires(R(type="cellIs", operator="greaterThanOrEqual", formulas=["0"]), NEG) is False
    assert fires(R(type="cellIs", operator="greaterThan", formulas=["-1"]), NEG) is UNKNOWN
    assert fires(R(type="cellIs", operator="equal", formulas=["0"]), NEG) is False
    assert fires(R(type="cellIs", operator="equal", formulas=["-7"]), NEG) is UNKNOWN
    assert fires(R(type="cellIs", operator="notEqual", formulas=["0"]), NEG) is True
    assert fires(R(type="cellIs", operator="between", formulas=["0", "100"]), NEG) is False
    assert fires(R(type="cellIs", operator="between", formulas=["-100", "0"]), NEG) is UNKNOWN
    assert fires(R(type="cellIs", operator="notBetween", formulas=["0", "100"]), NEG) is True
    assert fires(R(type="cellIs", operator="lessThan", formulas=['"x"']), NEG) is True     # numbers < text
    assert fires(R(type="containsText", text="-"), NEG) is UNKNOWN
    assert fires(R(type="expression", formulas=["B2<0"]), NEG, (2, 2, 2, 2)) is True
    assert fires(R(type="expression", formulas=["B2<-1000"]), NEG, (2, 2, 2, 2)) is UNKNOWN
    assert fires(R(type="containsBlanks"), NEG) is False and fires(R(type="notContainsErrors"), NEG) is True


def test_rubric_key():
    with open(RUBRIC) as fh:
        rub = json.load(fh)
    keys = [f"{cat}/{it['name']}" for cat, items in rub.items() for it in items]
    assert keys[65 - 1] == C65.key, (keys[65 - 1], C65.key)
    assert REGISTRY[65] is C65


# ============================================================================ workbook level
def test_general_negative_constant_and_formula_fail():
    st = Styles()
    cells = [c("A1", 0, -5), c("A2", 0, -123.4, f="B1-B2"), c("B1", 0, 7), c("B2", 0, 130.4)]
    v = run(book([("Model", sheet(cells))], st))
    assert v["decision"] == "fail" and locs(v) == ["Model!A1:A2"], v["mistakes"]
    d = v["mistakes"][0]["description"]
    assert "minus sign" in d and "'-5'" in d and "General" in d, d
    assert v["stats"]["negative_cells_failing"] == 2


def test_each_format_shape():
    assert one('#,##0;(#,##0)')["decision"] == "pass"
    assert one('#,##0.00;\\(#,##0.00\\)')["decision"] == "pass"           # escaped parentheses (toy T3)
    assert one('0;"("0")"')["decision"] == "pass"                          # quoted parentheses
    assert one('#,##0;[Red]\\(#,##0\\)')["decision"] == "pass"             # red AND parentheses (toy T3 Pass)
    v = one('#,##0;[Red]#,##0', -1006.22)                                  # toy T3 Fail: red, sign dropped
    assert v["decision"] == "fail" and "without a sign or parentheses" in v["mistakes"][0]["description"]
    assert "'1,006'" in v["mistakes"][0]["description"], v["mistakes"]
    assert one('#,##0')["decision"] == "fail"                              # automatic minus
    assert one('#,##0;-#,##0')["decision"] == "fail"
    assert one('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-')["decision"] == "fail"   # UK accounting
    v = one('#,##0;-(#,##0)')
    assert v["decision"] == "fail" and "minus sign as well as parentheses" in v["mistakes"][0]["description"]
    assert one('#,##0.00;(#,##0.00)-')["decision"] == "fail"
    assert one('@')["decision"] == "fail"                                  # a number under '@' shows -5
    assert one('General;General')["decision"] == "fail"                    # unsigned 5
    assert one('General;(General)')["decision"] == "pass"
    assert one('[<0](#,##0);#,##0')["decision"] == "pass"                  # conditional section with parentheses
    assert one('[>=1E11]"big";#,##0', -1234.5)["decision"] == "fail"       # fallback section: unsigned (Excel 2026-10-03)
    assert one('0;(0);0;@')["decision"] == "pass"
    assert one('#,##0;;')["decision"] == "pass"                            # empty negative section: nothing shown
    assert one('#,##0;"neg"')["decision"] == "pass"                        # literal text, no number
    assert one('_(* #,##0_);_(* \\(#,##0\\);_(* "-"??_);_(@_)')["decision"] == "pass"   # '_)' padding is not a parenthesis
    assert one('#,##0_);(#,##0)')["decision"] == "pass"
    assert one('')["decision"] == "fail"                                   # empty code = General (Excel 2026-10-03)


def test_percent_scientific_fraction():
    assert one('0.00%', -0.05)["decision"] == "fail"
    assert one('0.00%;(0.00%)', -0.05)["decision"] == "pass"
    assert one('0.0%;(0.0%);"-"', -0.05)["decision"] == "pass"
    assert one('0.00E+00', -0.000381)["decision"] == "fail"
    assert one('0.00E+00;(0.00E+00);"-"', -0.000381)["decision"] == "pass"
    assert one('# ?/?', -1.5)["decision"] == "fail"


def test_rounding_to_zero_and_threshold():
    assert one('#,##0', -0.3)["decision"] == "pass"                        # '-0': rounds to zero on display
    assert one('0.00', -0.004)["decision"] == "pass"                       # '-0.00'
    assert one('0.000', -0.004)["decision"] == "fail"                      # '-0.004'
    assert one('General', -1.1e-13)["decision"] == "pass"                  # leftover below the threshold
    assert one('General', -1.34e-6)["decision"] == "fail"                  # '-1.34E-06' (attempt 1985)
    assert one('General', -1e-6)["decision"] == "fail"                     # <= -1e-6 is a negative
    assert one('General', 5)["decision"] == "pass" and one('General', 0)["decision"] == "pass"
    with _switch(ROUNDS_TO_ZERO_COUNTS=True):
        v = one('0.00', -0.004)
        assert v["decision"] == "fail" and "round to zero" in v["mistakes"][0]["description"], v["mistakes"]


def test_not_numeric_negatives():
    st = Styles()
    cells = [c("A1", 0, "-5"), c("A2", 0, "-5", f='"-"&B1'), c("A3", 0, False), c("A4", 0, "#N/A", t="e"),
             c("A5", 0, "-5", t="s"), c("A6", 0, "2020-01-01", t="d"), c("A7", 0, None), c("B1", 0, 5),
             c("A8", 0, False, f="B1<0"), c("A9", 0, "", f='IF(B1>0,"","x")')]
    v = run(book([("S", sheet(cells))], st, sst=["-5"]))
    assert v["decision"] == "pass", v["mistakes"]
    assert v["stats"]["negative_cells_by_display"] == {}


def test_dates_not_judged():
    assert one('d-mmm-yy', -5)["decision"] == "pass"                      # Excel shows #####
    assert one('h:mm', -0.5)["decision"] == "pass"
    assert one('[h]:mm', -5)["decision"] == "pass"
    with _switch(DATE_TIME_IN_SCOPE=True):
        assert one('d-mmm-yy', -5)["decision"] == "fail"


def test_shared_child_and_spill_member():
    """Toy T1 trap: negatives in spill children (no <f>, cached values); shared-formula children."""
    st = Styles()
    p = st.xf('0.00%;\\(0.00%\\)')
    cells = [c("A1", p, -0.05, f="B1*-1", si=0), c("A2", p, -0.04, f="", si=0), c("A3", 0, -0.03, f="", si=0),
             c("A4", p, -0.02, f="", si=0), c("B1", 0, 0.05)]
    v = run(book([("S", sheet(cells))], st))
    assert locs(v) == ["S!A3"], v["mistakes"]
    assert v["stats"]["negative_cells_by_display"] == {"base_parens": 3, "judged_minus": 1}
    spill = (f'<sheetData><row r="1"><c r="C1" s="{p}" cm="1"><f t="array" ref="C1:C3">-SEQUENCE(3,1,1,1)/100</f>'
             f'<v>-0.01</v></c></row><row r="2"><c r="C2" s="{p}"><v>-0.02</v></c></row>'
             f'<row r="3"><c r="C3"><v>-0.03</v></c></row></sheetData>')
    v = run(book([("S", spill)], st))
    assert locs(v) == ["S!C3"], v["mistakes"]
    assert v["stats"]["negative_cells_by_display"] == {"base_parens": 2, "judged_minus": 1}


def test_counters_and_hidden_content_fail():
    """A period index -1 in General (toy T3 golden) and negatives on hidden sheets / rows / columns."""
    st = Styles()
    v = run(book([("S", sheet([c("G1", 0, -1), c("H1", 0, 0), c("I1", 0, 1)]))], st))
    assert locs(v) == ["S!G1"]
    v = run(book([("Vis", sheet([c("A1", 0, 5)])), ("Hid", sheet([c("A1", 0, -5)]))], st, states={1: "hidden"}))
    assert locs(v) == ["Hid!A1"] and "hidden sheet" in v["mistakes"][0]["description"]
    assert v["stats"]["hidden_sheets_with_failures"] == ["Hid"]
    v = run(book([("S", sheet([c("A3", 0, -5)], rows_attr={3: ' hidden="1"'}))], st))
    assert locs(v) == ["S!A3"]
    v = run(book([("S", sheet([c("B1", 0, -5)], head='<cols><col min="2" max="2" hidden="1" width="0"/></cols>'))], st))
    assert locs(v) == ["S!B1"]


def test_merged_non_anchor():
    st = Styles()
    tail = '<mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
    v = run(book([("S", sheet([c("A1", 0, "Label"), c("B1", 0, -5), c("C1", 0, -5), c("A2", 0, -5)], tail=tail))], st))
    assert locs(v) == ["S!A2"] and v["stats"]["negative_cells_by_display"]["merged_non_anchor"] == 2
    v = run(book([("S", sheet([c("A1", 0, -5)], tail=tail))], st))
    assert locs(v) == ["S!A1"]


def test_conditional_format_number_formats():
    # base '#,##0' (minus), CF (<0) applies '#,##0;(#,##0)': Excel shows parentheses -> pass (main and x14)
    st = Styles()
    g = st.xf("#,##0")
    dx = st.dxf("#,##0;(#,##0)")
    cells = [c("A1", g, -5), c("A2", g, -7, f="B2*-1"), c("B2", 0, 7)]
    tail = cf_main("A1:A5", ("cellIs", dx, "lessThan", ["0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "pass", v["mistakes"]
    assert v["stats"]["negative_cells_by_display"] == {"judged_parens": 2}
    v = run(book([("S", sheet(cells, tail=cf_x14("A1:A5", "cellIs", "lessThan", "0", "#,##0;(#,##0)")))], st))
    assert v["decision"] == "pass", v["mistakes"]
    # a ONE-section CF format '(#,##0)' gets Excel's automatic minus: '-(5)' -> fail
    st1 = Styles()
    g1 = st1.xf("#,##0")
    d1 = st1.dxf("(#,##0)")
    v = run(book([("S", sheet([c("A1", g1, -5)], tail=cf_main("A1:A5", ("cellIs", d1, "lessThan", ["0"], ""))))], st1))
    assert v["decision"] == "fail" and "'-(5)'" in v["mistakes"][0]["description"], v["mistakes"]
    # a rule that does not fire on the value leaves the base format: fail
    tail = cf_main("A1:A5", ("cellIs", dx, "lessThan", ["-100"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A2"], v["mistakes"]
    # expression comparing the cell itself (relative to the range's top-left cell)
    tail = cf_main("A1:A5", ("expression", dx, None, ["A1<0"], ""))
    assert run(book([("S", sheet(cells, tail=tail))], st))["decision"] == "pass"
    # stopIfTrue on a higher-priority rule without a format blocks the parenthesis rule
    tail = ('<conditionalFormatting sqref="A1:A5">'
            f'<cfRule type="cellIs" priority="1" operator="lessThan" stopIfTrue="1"><formula>0</formula></cfRule>'
            f'<cfRule type="cellIs" dxfId="{dx}" priority="2" operator="lessThan"><formula>0</formula></cfRule>'
            '</conditionalFormatting>')
    assert run(book([("S", sheet(cells, tail=tail))], st))["decision"] == "fail"
    # a rule reading another cell is evaluated through the value source (Patrick 2026-10-05; GradingError until
    # then): B9 empty -> 0 < 0 is false -> the base format's minus shows; B9 = 1 (a switch) -> parentheses
    tail = cf_main("A1:A5", ("expression", dx, None, ["$B$9<0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A2"] and v["stats"]["cf_assumptions"]["cells"] == 0, v["stats"]
    tail = cf_main("A1:A5", ("expression", dx, None, ["$B$9=1"], ""))
    assert run(book([("S", sheet(cells + [c("B9", 0, 1)], tail=tail))], st))["decision"] == "pass"
    # a rule this check cannot evaluate is OFF: the base format decides, the assumption is recorded (decisive)
    tail = cf_main("A1:A5", ("expression", dx, None, ["SUM($B$1:$B$9)<0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    a = v["stats"]["cf_assumptions"]
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A2"] and a["decisive_cells"] == 2 and "SUM" in a["examples"][0], a
    # ... not decisive when the CF format also shows a minus
    st2 = Styles()
    g2 = st2.xf("#,##0")
    dm = st2.dxf("0.0")
    tail = cf_main("A1:A5", ("expression", dm, None, ["SUM($B$1:$B$9)<0"], ""))
    v = run(book([("S", sheet([c("A1", g2, -5)], tail=tail))], st2))
    assert v["decision"] == "fail" and v["stats"]["cf_assumptions"]["decisive_cells"] == 0, v["stats"]["cf_assumptions"]


def test_conditional_format_turns_parens_into_minus_second_pass():
    """A CF number format with a minus over a parenthesis base: those cells were not read in the
    first pass, so the sheet gets a second pass."""
    st = Styles()
    p = st.xf("#,##0;(#,##0)")
    dm = st.dxf("0.0")
    cells = [c("A1", p, -5), c("A2", p, -7, f="B2*-1"), c("A9", p, -9), c("B2", 0, 7)]
    tail = cf_main("A1:A5", ("cellIs", dm, "lessThan", ["0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A2"], v["mistakes"]
    assert "conditional-format number format '0.0'" in v["mistakes"][0]["description"]
    assert v["stats"]["cf_second_pass_sheets"] == ["S"]
    # the CF format only fires on positives: no second pass needed, everything passes
    tail = cf_main("A1:A5", ("cellIs", dm, "greaterThan", ["0"], ""))
    v = run(book([("S", sheet(cells, tail=tail))], st))
    assert v["decision"] == "pass" and v["stats"]["cf_second_pass_sheets"] == []


def test_data_bar_hiding_value():
    st = Styles()
    v = run(book([("S", sheet([c("A1", 0, -5), c("A2", 0, -5)], tail=DATABAR.format(sq="A1:A1")))], st))
    assert locs(v) == ["S!A2"] and v["stats"]["negative_cells_by_display"]["judged_hidden"] == 1
    with _switch(HIDDEN_NEGATIVE_PASSES=False):
        v = run(book([("S", sheet([c("A1", 0, -5)], tail=DATABAR.format(sq="A1:A1")))], st))
        assert v["decision"] == "fail" and "hidden by a data bar" in v["mistakes"][0]["description"]


def test_values_read_only_where_needed():
    """openpyxl-labelled file: formula caches untrusted.  A formula under a parenthesis format
    needs no value (passes); one under General needs its value -> GradingError (no fallback)."""
    st = Styles()
    p = st.xf('#,##0;(#,##0)')
    v = run(book([("S", sheet([c("A1", p, -5, f="B1*-1"), c("B1", 0, 5), c("C1", p, None, f="B1")]))], st, app=APP_OPX))
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 0
    e = graded("untrusted_value", lambda: run(book([("S", sheet([c("A1", 0, -5, f="B1*-1"), c("B1", 0, 5)]))], st, app=APP_OPX)),
               "untrusted", "S!A1", "'General'", "'-1234567.891'")
    # a conditional format needs the value too (which section applies depends on it)
    k = st.xf('[<-1000](#,##0);#,##0')
    graded("untrusted_value", lambda: run(book([("S", sheet([c("A1", k, -5, f="B1*-1"), c("B1", 0, 5)]))], st, app=APP_OPX)),
           "untrusted", "conditional sections")
    # a positive cached value is still untrusted: the check cannot know it is positive
    graded("untrusted_value", lambda: run(book([("S", sheet([c("A1", 0, 5, f="B1"), c("B1", 0, 5)]))], st, app=APP_OPX)), "untrusted")
    # constants are always readable
    assert locs(run(book([("S", sheet([c("A1", 0, -5)]))], st, app=APP_OPX))) == ["S!A1"]
    # a trusted writer (no docProps): the cache is read
    assert locs(run(book([("S", sheet([c("A1", 0, -5, f="B1*-1"), c("B1", 0, 5)]))], st))) == ["S!A1"]


def test_untrusted_values_needed_only_after_the_tail():
    """An untrusted formula under a minus format needs its value only if a negative there could
    still fail once the sheet tail is known."""
    st = Styles()
    dx = st.dxf("#,##0;(#,##0)")
    f0 = [c("A1", 0, -5, f="B1*-1"), c("B1", 0, 5)]
    # (a) a CF rule (<0 -> parentheses) over General formulas: any negative shows parentheses
    v = run(book([("S", sheet(f0, tail=cf_main("A1:A5", ("cellIs", dx, "lessThan", ["0"], ""))))], st, app=APP_OPX))
    assert v["decision"] == "pass" and v["stats"]["untrusted_values_not_needed"] == 1, v
    # (b) a data bar hiding the value
    v = run(book([("S", sheet(f0, tail=DATABAR.format(sq="A1:A3")))], st, app=APP_OPX))
    assert v["decision"] == "pass", v
    # (c) a formula in a non-anchor merged cell is not displayed
    tail = '<mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
    cells = [c("A1", 0, "Label"), c("B1", 0, -5, f="D1*-1"), c("D1", 0, 5)]
    assert run(book([("S", sheet(cells, tail=tail))], st, app=APP_OPX))["decision"] == "pass"
    # controls: the tail does not rescue the cell -> GradingError naming it
    graded("untrusted_value", lambda: run(book([("S", sheet(f0 + [c("A3", 0, -5, f="B1*-1")], tail=tail))], st, app=APP_OPX)),
           "untrusted", "S!A1")
    tail_lt = cf_main("A1:A5", ("cellIs", dx, "lessThan", ["-100"], ""))      # fires only on some negatives
    graded("untrusted_value", lambda: run(book([("S", sheet(f0, tail=tail_lt))], st, app=APP_OPX)), "untrusted", "S!A1")
    graded("untrusted_value", lambda: run(book([("S", sheet(f0))], st, app=APP_OPX)), "untrusted", "S!A1")


def test_untrusted_values_beyond_buffer_second_pass():
    st = Styles()
    tail = '<mergeCells count="4">' + "".join(f'<mergeCell ref="A{r}:B{r}"/>' for r in range(1, 5)) + "</mergeCells>"
    rescued = [c(f"B{r}", 0, -5, f="C1*-1") for r in range(1, 5)] + [c("C1", 0, 5)]
    with _switch(MAX_DEFERRED=2):
        v = run(book([("S", sheet(rescued, tail=tail))], st, app=APP_OPX))
        assert v["decision"] == "pass" and v["stats"]["deferred_second_pass_sheets"] == ["S"], v
        bad = rescued + [c("D4", 0, -5, f="C1*-1")]
        graded("untrusted_value", lambda: run(book([("S", sheet(bad, tail=tail))], st, app=APP_OPX)), "untrusted", "S!D4")


def test_unreadable_formats_raise_only_when_needed():
    st = Styles()
    bad = st.xf(65)                     # numFmtId 65: no <numFmt> and no built-in meaning
    assert run(book([("S", sheet([c("A1", bad, "text"), c("A2", bad, 5)]))], st))["decision"] == "pass"
    # Patrick 2026-10-06 (attempt 2777, measured in Excel): an unreadable format is General, so the
    # negative shows as -5 and fails; recorded (it raised until then)
    v = graded("unreadable_number_format", lambda: run(book([("S", sheet([c("A1", bad, -5)]))], st)), "S!A1")
    assert v["decision"] == "fail" and locs(v) == ["S!A1"], v


def test_array_members_missing_from_the_file():
    """Members of an array range with no <c> (only the anchor written) are shown by Excel under
    the row / column style; their values are unknown -> GradingError when a negative there would
    not pass; no error when every negative there would."""
    st = Styles()
    p = st.xf('#,##0;(#,##0)')
    anchor = '<c r="A1"><f t="array" ref="A1:C1">B9:D9*1</f><v>5</v></c>'
    graded("unwritten_array_member", lambda: run(book([("S", f'<sheetData><row r="1">{anchor}</row></sheetData>')], st)),
           "S!A1:C1", "2 member cell(s) not written in the file", "'General'")
    cols = f'<cols><col min="2" max="3" width="9" style="{p}"/></cols>'
    v = run(book([("S", f'{cols}<sheetData><row r="1">{anchor}</row></sheetData>')], st))
    assert v["decision"] == "pass" and v["stats"]["array_members_not_in_file"] == 2, v
    row = f'<row r="1" s="{p}" customFormat="1">'
    v = run(book([("S", f'<sheetData>{row}{anchor}</row></sheetData>')], st))
    assert v["decision"] == "pass", v
    full = (f'<sheetData><row r="1">{anchor}<c r="B1"><v>1</v></c><c r="C1"><v>2</v></c></row></sheetData>')
    v = run(book([("S", full)], st))
    assert v["decision"] == "pass" and v["stats"]["array_members_not_in_file"] == 0, v
    dyn = '<c r="A1" cm="1"><f t="array" ref="A1:A3">SEQUENCE(3)</f><v>1</v></c>'
    graded("unwritten_array_member", lambda: run(book([("S", f'<sheetData><row r="1">{dyn}</row></sheetData>')], st)),
           "S!A1:A3", "2 member cell(s)")


def test_formula_results_typed_text_or_error_follow_the_trusted_value():
    """Second review R2-1: the type the delivered file stores for a formula result (an agent
    tool's t="str" empty cache, a t="e" #NAME? placeholder, t="b") says nothing about what
    Excel shows; the trusted value (here a recalculation copy) decides.  Genuine text /
    boolean / error results are skipped; an untrusted needed value raises."""
    st = Styles()
    p = st.xf('#,##0;(#,##0)')
    typed = [c("A1", 0, "#NAME?", t="e", f="-_xlfn.XLOOKUP(1,B1:B2,C1:C2)"), c("A2", 0, "", f="-C1"),
             c("A3", 0, False, f="C1<0"), c("A4", p, "", f="-C1"), c("B1", 0, 1), c("C1", 0, 5)]
    assert all(x in "".join(typed) for x in ('t="e"', 't="str"', 't="b"')), typed
    delivered = book([("S", sheet(typed))], st, app=APP_OPX, name="typed_delivered")
    copy = book([("S", sheet([c("A1", 0, -5, f="-XLOOKUP(1,B1:B2,C1:C2)"), c("A2", 0, -5, f="-C1"),
                              c("A3", 0, -7, f="C1<0"), c("A4", p, -5, f="-C1"), c("B1", 0, 1), c("C1", 0, 5)]))],
                st, name="typed_copy")
    v = run(delivered, value_path=copy)
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A3"], v["mistakes"]
    assert v["stats"]["formula_values_read"] == 3, v["stats"]
    assert v["stats"]["negative_cells_by_display"] == {"judged_minus": 3, "base_parens": 1}, v["stats"]
    # the copy holds genuine non-numbers: nothing to judge
    copy2 = book([("S", sheet([c("A1", 0, "n/a", f='"n/a"'), c("A2", 0, "#N/A", t="e", f="NA()"),
                               c("A3", 0, True, f="C1>0"), c("A4", p, "", f='""'), c("B1", 0, 1), c("C1", 0, 5)]))],
                 st, name="typed_copy_text")
    v = run(delivered, value_path=copy2)
    assert v["decision"] == "pass" and v["stats"]["negative_cells_by_display"] == {}, v
    # no copy: the values are needed and untrusted -> GradingError naming the first such cell
    graded("untrusted_value", lambda: run(delivered), "untrusted", "S!A1", "'General'")
    # ... but not under a parenthesis format (no value needed)
    v = run(book([("S", sheet([c("A1", p, "", f="-C1"), c("A2", p, "#NAME?", t="e", f="-C1"), c("C1", 0, 5)]))],
                 st, app=APP_OPX, name="typed_parens"))
    assert v["decision"] == "pass" and v["stats"]["formula_values_read"] == 0, v
    # trusted writer: genuine text / error / boolean results under General pass
    v = run(book([("S", sheet([c("A1", 0, "n/a", f='"n/a"'), c("A2", 0, "#N/A", t="e", f="NA()"),
                               c("A3", 0, True, f="1>0"), c("A4", 0, "", f='""')]))], st, name="typed_trusted"))
    assert v["decision"] == "pass" and v["stats"]["negative_cells_by_display"] == {}, v
    # second pass: a CF minus format over a parenthesis base; the typed cell's copy value is read
    dm = st.dxf("0.0")
    tail = cf_main("A1:A5", ("cellIs", dm, "lessThan", ["0"], ""))
    d2 = book([("S", sheet([c("A1", p, -5), c("A2", p, "", f="B2*-1"), c("B2", 0, 7)], tail=tail))], st,
              app=APP_OPX, name="typed_cf_delivered")
    c2 = book([("S", sheet([c("A1", p, -5), c("A2", p, -7, f="B2*-1"), c("B2", 0, 7)], tail=tail))], st,
              name="typed_cf_copy")
    v = run(d2, value_path=c2)
    assert v["decision"] == "fail" and locs(v) == ["S!A1:A2"] and v["stats"]["cf_second_pass_sheets"] == ["S"], v
    graded("untrusted_value", lambda: run(d2), "untrusted", "S!A2")


def test_label_literals_are_not_signs():
    """Second review R2-3 / R2-4: literal text with letters or digits ('FY24-25 ', ' – est.',
    'T-1: ') is a label; its dashes and digits are neither the number's sign nor its digits.
    Punctuation-only literals ('"-"') stay part of the number."""
    for code in ('0.0;(0.0)" – est."', '"T-1: "0;"T-1: "(0)', '"FY24-25 "#,##0;"FY24-25 "(#,##0)',
                 '#,##0;(#,##0)" k-USD"', '#,##0;\\k(#,##0)', '0;"a;b"(0)'):
        assert classify(-5.0, code)[0] == PARENS, (code, classify(-5.0, code))
        assert one(code)["decision"] == "pass", code
    assert classify(-5.0, '0.0;(0.0)" – est."')[1] == '(5.0) – est.'           # the text shown stays the full display
    assert classify(-5.0, '"FY24-25 "#,##0;"FY24-25 "(#,##0)')[1] == 'FY24-25 (5)'
    for code in ('0;(0)"-"', '0;"-"(0)', '#,##0;-(#,##0)'):
        assert classify(-5.0, code)[0] == MIXED and one(code)["decision"] == "fail", code
    assert classify(-5.0, '0;"Loss: "-0')[0] == MINUS and one('0;"Loss: "-0')["decision"] == "fail"
    assert classify(-5.0, '"FY24-25 "#,##0')[0] == MINUS                          # '-FY24-25 5' (automatic minus)
    assert classify(-0.3, '"FY24-25 "#,##0')[0] == ZERO                           # the label's digits are not the number's
    assert classify(-5.0, '0;"T-1"')[0] == TEXT and one('0;"T-1"')["decision"] == "pass"   # no number shown
    assert classify(-5.0, '#,##0;"neg"')[0] == TEXT and classify(-5.0, '#,##0;\\-')[0] == TEXT
    v = one('"FY "0;"FY "-0')
    assert v["decision"] == "fail" and "'FY -5'" in v["mistakes"][0]["description"], v["mistakes"]
    # a label keeps its parentheses (only its letters, digits and dashes go): they may wrap the number
    for code, shown in (('0;"Net ("0")"', 'Net (5)'), ('#,##0" bn";"("#,##0" bn)"', '(5 bn)'),
                        ('0;"(USD "0")"', '(USD 5)')):
        assert classify(-5.0, code) == (PARENS, shown), (code, classify(-5.0, code))
        assert one(code)["decision"] == "pass", code
    # corpus: the GPT-6 multiples format (1413, 1730, 1805) keeps its ')' inside the label '"x)"'
    mult = '0.0\\x;\\(0.0"x)";\\-'
    assert classify(-1.62437, mult) == (PARENS, '(1.6x)'), classify(-1.62437, mult)
    assert one(mult, -1.62437)["decision"] == "pass" and one(mult, -1.62437, f="-1.62437")["decision"] == "pass"
    assert classify(-1.62437, '0.0\\x;-0.0"x"')[0] == MINUS and classify(-1.62437, '0.0\\x;0.0"x"')[0] == UNSIGNED
    # ... but only parentheses that enclose the number count
    for code, cls in (('0;"(est) "0', UNSIGNED), ('0;"(a) "0" (b)"', UNSIGNED), ('0;0" (bn)"', UNSIGNED),
                      ('0;-0" (bn)"', MINUS), ('0;"(bn) "-0', MINUS), ('0;(0', UNSIGNED)):
        assert classify(-5.0, code)[0] == cls and one(code)["decision"] == "fail", (code, classify(-5.0, code))
    assert strip_label_literals('"T-1: "(0)') == '": "(0)'
    assert strip_label_literals('(0.0)" – est."') == '(0.0)"  ."'
    assert strip_label_literals('"("0" bn)"') == '"("0" )"'
    assert strip_label_literals('"T-1"0') == "0"
    assert strip_label_literals('"-"(0)_)') == '"-"(0)_)'
    assert strip_label_literals('[Red]\\k\\(0\\)') == '[Red]\\(0\\)'
    assert strip_label_literals('_("$"* \\(#,##0\\);"a;b"') == '_("$"* \\(#,##0\\);";"'
    assert strip_label_literals('"unterminated') == '"unterminated'


def test_grouping_cap_and_order():
    st = Styles()
    cells = [c(f"{col}{r}", 0, -r * 10 - k) for r in range(1, 4) for k, col in enumerate("BCD")] + [c("A1", 0, -1)]
    cells += [c(f"F{r}", 0, -r) for r in range(1, 61, 2)]           # 30 separate cells
    v = run(book([("S", sheet(cells))], st))
    assert v["decision"] == "fail" and v["stats"]["n_mistakes"] == 32 and v["stats"]["mistakes_truncated"]
    assert len(v["mistakes"]) == 25 and v["stats"]["negative_cells_failing"] == 40
    assert locs(v)[:3] == ["S!A1:D1", "S!F1", "S!B2:D3"], locs(v)[:3]     # row runs, then stacked
    assert "e.g. '-1'" in v["mistakes"][0]["description"], v["mistakes"][0]
    assert "(first 25 of 32 listed)" in v["summary"]
    # distinct formats make distinct mistakes
    st = Styles()
    u = st.xf("#,##0;[Red]#,##0")
    v = run(book([("S", sheet([c("A1", 0, -5), c("B1", u, -5)]))], st))
    assert locs(v) == ["S!A1", "S!B1"] and v["stats"]["n_mistakes"] == 2


TESTS = [test_classify_formats, test_builtin_ids, test_cf_rule_evaluation, test_rubric_key,
         test_general_negative_constant_and_formula_fail, test_each_format_shape, test_percent_scientific_fraction,
         test_rounding_to_zero_and_threshold, test_not_numeric_negatives, test_dates_not_judged,
         test_shared_child_and_spill_member, test_counters_and_hidden_content_fail, test_merged_non_anchor,
         test_conditional_format_number_formats, test_conditional_format_turns_parens_into_minus_second_pass,
         test_data_bar_hiding_value, test_values_read_only_where_needed,
         test_untrusted_values_needed_only_after_the_tail, test_untrusted_values_beyond_buffer_second_pass,
         test_unreadable_formats_raise_only_when_needed, test_array_members_missing_from_the_file,
         test_formula_results_typed_text_or_error_follow_the_trusted_value, test_label_literals_are_not_signs,
         test_grouping_cap_and_order]


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
