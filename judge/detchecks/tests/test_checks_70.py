"""Unit tests for Reasonable column widths (70) on synthetic micro-workbooks (raw SpreadsheetML zips
written here).

    cd /Users/patrick/MBABenchV2-detchecks/judge
    python3 /Users/patrick/MBABench-deterministic-checks/heavy_run.py -- /Users/patrick/MBABenchV2/.venv/bin/python \
        -m detchecks.tests.test_checks_70

Plain asserts; also collectable by pytest.  Temporary files go to detchecks/scratch/colwidths70/.
The judge-port parity test imports judge/utils/workbook_properties.py (run from judge/).
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import traceback
import zipfile
from xml.sax.saxutils import escape, quoteattr

from detchecks.api import Engine
from detchecks.checks import c70 as M
from detchecks.checks.c70 import C70, paint_cols, stored_pieces, text_px, wide_outliers, width_runs
from detchecks.checks.c69 import glyph_row
from detchecks.core.refs import index_to_col
from detchecks.core.sheet import ColInfo
from detchecks.errors import GradingError

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "colwidths70")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WS = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
OPENPYXL = "Microsoft Excel Compatible / Openpyxl 3.1.5"

_TMP = None
_N = [0]


def tmp(name: str = "") -> str:
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_70_", dir=SCRATCH)
    _N[0] += 1
    return os.path.join(_TMP, f"{_N[0]:03d}_{name or 'book'}.xlsx")


# ============================================================================ builder
class Styles:
    """styles.xml builder; Normal = Calibri 11 unless given (MDW 7 px)."""

    def __init__(self, normal=("Calibri", 11)):
        self.fonts = [f'<font><sz val="{normal[1]}"/><name val="{normal[0]}"/></font>']
        self.xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
        self.numfmts = {}

    def fmt(self, code: str) -> int:
        for i, c_ in self.numfmts.items():
            if c_ == code:
                return i
        i = 164 + len(self.numfmts)
        self.numfmts[i] = code
        return i

    def xf(self, name="Calibri", size=11, bold=False, numfmt=0, wrap=False, horizontal=None, shrink=False,
           indent=0, rotation=0) -> int:
        self.fonts.append(f'<font>{"<b/>" if bold else ""}<sz val="{size}"/><name val="{name}"/></font>')
        fid = len(self.fonts) - 1
        nid = numfmt if isinstance(numfmt, int) else self.fmt(numfmt)
        al = []
        if wrap:
            al.append('wrapText="1"')
        if horizontal:
            al.append(f'horizontal="{horizontal}"')
        if shrink:
            al.append('shrinkToFit="1"')
        if indent:
            al.append(f'indent="{indent}"')
        if rotation:
            al.append(f'textRotation="{rotation}"')
        alx = f'<alignment {" ".join(al)}/>' if al else ""
        self.xfs.append(f'<xf numFmtId="{nid}" fontId="{fid}" fillId="0" borderId="0" xfId="0" applyFont="1" '
                        f'applyNumberFormat="1" applyAlignment="1">{alx}</xf>')
        return len(self.xfs) - 1

    def xml(self) -> str:
        nf = "".join(f'<numFmt numFmtId="{i}" formatCode={quoteattr(c_)}/>' for i, c_ in self.numfmts.items())
        nfx = f'<numFmts count="{len(self.numfmts)}">{nf}</numFmts>' if nf else ""
        return (f'<styleSheet xmlns="{MAIN}">{nfx}<fonts count="{len(self.fonts)}">{"".join(self.fonts)}</fonts>'
                '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf fontId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(self.xfs)}">{"".join(self.xfs)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def c(ref, v=None, s=0, f=None, t=None):
    """One <c>.  v: str (inline string), number, or a formula cache (with f; None = no cache)."""
    sa = f' s="{s}"' if s else ""
    if f is not None:
        tt = t or ("str" if isinstance(v, str) else None)
        ta = f' t="{tt}"' if tt else ""
        vv = "" if v is None else f"<v>{escape(str(v))}</v>"
        return f'<c r="{ref}"{sa}{ta}><f>{escape(f)}</f>{vv}</c>'
    if v is None:
        return f'<c r="{ref}"{sa}/>'
    if isinstance(v, str):
        return f'<c r="{ref}"{sa} t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
    return f'<c r="{ref}"{sa}><v>{v!r}</v></c>'


def rich(ref, runs, s=0):
    """Inline rich string: runs = [(text, size or None)]."""
    rr = "".join((f'<r><rPr><sz val="{sz}"/></rPr>' if sz else "<r>") + f'<t xml:space="preserve">{escape(t)}</t></r>'
                 for t, sz in runs)
    sa = f' s="{s}"' if s else ""
    return f'<c r="{ref}"{sa} t="inlineStr"><is>{rr}</is></c>'


def ws(rows=None, *, cols="", fmt='<sheetFormatPr defaultRowHeight="15"/>', tail=""):
    """rows: {r: [cells]} or {r: (attrs, [cells])}."""
    body = []
    for r in sorted(rows or {}):
        x = rows[r]
        attrs, cells = x if isinstance(x, tuple) else ("", x)
        body.append(f'<row r="{r}"{(" " + attrs) if attrs else ""}>{"".join(cells)}</row>')
    colx = f"<cols>{cols}</cols>" if cols else ""
    return f"{fmt}{colx}<sheetData>{''.join(body)}</sheetData>{tail}"


def col(lo, hi, width=None, hidden=False):
    w = f' width="{width}" customWidth="1"' if width is not None else ""
    h = ' hidden="1"' if hidden else ""
    return f'<col min="{lo}" max="{hi}"{w}{h}/>'


def merges(*refs):
    return f'<mergeCells count="{len(refs)}">' + "".join(f'<mergeCell ref="{r}"/>' for r in refs) + "</mergeCells>"


def book(sheets, styles=None, *, states=None, app=None, name=""):
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
        rr += f'<Relationship Id="rId2" Type="{REL}/extended-properties" Target="docProps/app.xml"/>'
        z.writestr("docProps/app.xml", '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
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
    z.writestr("xl/workbook.xml", f'{DECL}<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>' + "".join(sh) + "</sheets></workbook>")
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG_REL}">' + "".join(wrels) + "</Relationships>")
    z.close()
    return path


def run(path):
    return Engine(path, [C70()]).run()[C70.key]


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
            assert n in msg, (n, msg[:800])
        return e
    raise AssertionError(f"expected GradingError containing {needles}")


class patched:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def stored(ui_chars: float, mdw: int = 7) -> float:
    """The stored <col width> Excel writes for a Column Width dialog value (UI 12.0 -> 12.7109375)."""
    return int((ui_chars * mdw + 5) / mdw * 256) / 256


def digits(n: int) -> str:
    """n digits: exactly n Calibri-11 characters wide (a Calibri 11 digit is the 7 px column unit)."""
    return "0" * n


def sentence(n: int) -> str:
    base = "The model rolls the balance forward each quarter and reports the closing cash position. "
    return (base * (n // len(base) + 1))[:n]


# ============================================================================ pure functions
def test_70_geometry_and_text():
    # overlapping <col> entries: a later entry overrides earlier ones on the columns it covers
    segs = paint_cols([ColInfo(6, 7, 9.0), ColInfo(6, 6, 44.0)])
    assert [(a, b, ci.width) for a, b, ci in segs] == [(6, 6, 44.0), (7, 7, 9.0)]
    segs = paint_cols([ColInfo(11, 24, 10.21875), ColInfo(11, 11, 40.0), ColInfo(12, 12, 22.0)])
    assert [(a, b, ci.width) for a, b, ci in segs] == [(11, 11, 40.0), (12, 12, 22.0), (13, 24, 10.21875)]
    segs = paint_cols([ColInfo(1, 3, 5.0), ColInfo(5, 9, 6.0), ColInfo(2, 6, 7.0)])
    assert [(a, b, ci.width) for a, b, ci in segs] == [(1, 1, 5.0), (2, 6, 7.0), (7, 9, 6.0)]
    # the painting is the core reader's one rule (SheetHead.col_segments / col_info, shared with 69 and 73)
    from detchecks.core import sheet as core_sheet
    assert paint_cols is core_sheet.paint_cols
    # stored pieces / runs: hidden columns are left out and break runs; widths rounded to 2 decimals
    pieces = stored_pieces(paint_cols([ColInfo(1, 2, 12.886), ColInfo(3, 3, 12.89, hidden=True), ColInfo(4, 5, 12.89)]), 8.43)
    assert pieces[:3] == [(1, 2, 12.89), (3, 3, None), (4, 5, 12.89)] and pieces[3] == (6, 16384, 8.43)
    assert width_runs(pieces, 1, 7) == [[1, 2, 12.89], [4, 5, 12.89], [6, 7, 8.43]]
    # text widths: Calibri 11 digits are 7 px; accented letters as their base letter; CJK 1 em
    cal = glyph_row("calibri", False)
    ppem = 11 * 96 / 72
    assert text_px(cal, "2029", ppem) == 28
    assert text_px(cal, "é", ppem) == text_px(cal, "e", ppem)
    assert text_px(cal, "中", ppem) == round(ppem)


def test_70_wide_outlier_port():
    def runs(*spec):        # (first, last, width)
        return [list(x) for x in spec]
    # judge rule: BA:BD at 34.89 between 12.89 runs (toy pair of the report) -> outlier
    g = wide_outliers(runs((1, 52, 12.89), (53, 56, 34.89), (57, 80, 12.89)))
    assert [(x[0], x[1], x[2]) for x in g] == [(53, 56, "judge")]
    assert M.judge_tag(g[0]) == "BA:BD width 34.9 vs neighbours 12.9 (2.7x): WIDE OUTLIER"
    # 2.46x is not an outlier; lone columns are never compared; spacers (< 4) are not neighbours
    assert wide_outliers(runs((1, 5, 12.0), (6, 7, 29.5), (8, 12, 12.0))) == []
    assert wide_outliers(runs((1, 5, 12.0), (6, 6, 40.0), (7, 11, 12.0)), unequal=False) == []
    g = wide_outliers(runs((1, 5, 12.0), (6, 6, 2.0), (7, 8, 40.0), (9, 9, 2.0), (10, 14, 12.0)))
    assert [(x[0], x[1]) for x in g] == [(7, 8)]
    # an edge group (no neighbour run on one side) and a group longer than both neighbours are not outliers
    assert wide_outliers(runs((1, 5, 12.0), (6, 7, 40.0))) == []
    assert wide_outliers(runs((1, 2, 12.0), (3, 9, 40.0), (10, 11, 12.0))) == []
    # extension: AE 48.0 / AF 35.78 between 13.0 runs (toy T2 Fail) is a group; the exact port misses it
    t2 = runs((9, 30, 13.0), (31, 31, 48.0), (32, 32, 35.78), (33, 68, 13.0))
    assert wide_outliers(t2, unequal=False) == []
    g = wide_outliers(t2, unequal=True)
    assert [(x[0], x[1], x[2], x[5]) for x in g] == [(31, 32, "ext", (48.0, 35.78))]
    assert "AE:AF width 48.0/35.8 vs neighbours 13.0" in M.judge_tag(g[0])
    # ... but not when one of them is under 2.5x, or when the stretch is a single column
    assert wide_outliers(runs((9, 30, 13.0), (31, 31, 48.0), (32, 32, 30.0), (33, 68, 13.0)), unequal=True) == []


def test_70_judge_port_parity():
    """The 'judge' groups equal judge/utils/workbook_properties.wide_outlier_tags on 600 random layouts."""
    import re
    try:
        from utils.workbook_properties import wide_outlier_tags
    except ImportError:          # not run from judge/
        print("  (skipped: judge utils not importable)")
        return
    rnd = random.Random(70)
    palette = [2.0, 3.5, 4.0, 8.43, 9.14, 12.89, 13.0, 16.0, 20.0, 32.5, 34.89, 35.78, 40.0, 48.0, 60.0]
    for _ in range(600):
        n = rnd.randint(4, 40)
        cols, props_runs, hidden = [], [], []
        c = 1
        while c <= n:
            ln = rnd.choice([1, 1, 2, 2, 3, 5, 8])
            w = rnd.choice(palette)
            hi = min(n, c + ln - 1)
            if rnd.random() < 0.15:
                cols.append(ColInfo(c, hi, None))           # no <col>: use the default -> skip in props
            else:
                h = rnd.random() < 0.08
                cols.append(ColInfo(c, hi, w, hidden=h))
                props_runs.append({"first": c, "last": hi, "value": w})
                if h:
                    hidden.extend(range(c, hi + 1))
            c = hi + 1
        cols = [ci for ci in cols if ci.width is not None]
        dflt = rnd.choice([None, 8.43, 5.21875, 13.0])
        lo = rnd.randint(1, 3)
        hi = rnd.randint(max(lo, n - 3), n)
        props = {"name": "S", "column_widths": props_runs, "used_range": f"{index_to_col(lo)}1:{index_to_col(hi)}9",
                 "default_col_width": dflt, "hidden_cols": hidden}
        want = set()
        for t in wide_outlier_tags(props):
            a, b = re.match(r"([A-Z]+):([A-Z]+) ", t).groups()
            want.add((a, b))
        pieces = stored_pieces(paint_cols(cols), dflt if dflt else 8.43)
        got = {(index_to_col(x[0]), index_to_col(x[1])) for x in wide_outliers(width_runs(pieces, lo, hi), unequal=False)}
        assert got == want, (props, got, want)
        ext = wide_outliers(width_runs(pieces, lo, hi), unequal=True)
        assert want <= {(index_to_col(x[0]), index_to_col(x[1])) for x in ext}


# ============================================================================ Test A (cap)
def test_70_cap_rule():
    """Toy T1 rebuilt: a number column widened to 150 characters fails; the boundaries."""
    st = Styles()
    s_hdr = st.xf("Arial", 10, bold=True)
    s_num = st.xf("Arial", 10, numfmt="#,##0.00")
    rows = {4: [c("H4", "Enterprise Value", s_hdr)], 5: [c("H5", 123456.78, s_num)], 6: [c("H6", 9876543.21, s_num)]}
    v = run(book([("Solution Model", ws(rows, cols=col(8, 8, stored(150))))], st))
    assert v["decision"] == "fail" and locs(v) == ["'Solution Model'!H:H"], v["mistakes"]
    d = v["mistakes"][0]["description"]
    assert "H is 150.00 characters wide" in d and "Enterprise Value" in d and "80-character cap" in d, d
    assert v["stats"]["tests_failed"] == ["A"]
    # under the cap: pass, whatever the content
    v = run(book([("S", ws(rows, cols=col(8, 8, stored(79.9))))], st))
    assert v["decision"] == "pass", v["mistakes"]
    # 100 wide: content needing 51 or 50 characters passes ("more than 2x"), 49 fails
    for n, want in ((51, "pass"), (50, "pass"), (49, "fail")):
        v = run(book([("S", ws({1: [c("B1", digits(n))]}, cols=col(2, 2, stored(100))))]))
        assert v["decision"] == want, (n, v["summary"])
    # cap 75 vs 80 (stats only): a 78-wide column needing 30 passes at 80 and would fail at 75
    v = run(book([("S", ws({1: [c("B1", digits(30))]}, cols=col(2, 2, stored(78))))]))
    assert v["decision"] == "pass" and v["stats"]["switches"]["cap_75"] == "fail"


def test_70_empty_hidden_and_default_columns():
    # an empty column with an explicit width over the cap fails (need 0); trailing E:XFD at 100 -> one mistake
    v = run(book([("S", ws({1: [c("A1", "label")]}, cols=col(3, 3, stored(120))))]))
    assert v["decision"] == "fail" and locs(v) == ["S!C:C"] and "no content" in v["mistakes"][0]["description"]
    v = run(book([("S", ws({1: [c("A1", "label")]}, cols=col(5, 16384, stored(100))))]))
    assert locs(v) == ["S!E:XFD"], locs(v)
    # hidden and zero-width columns are No hidden rows/columns (93)'s business
    v = run(book([("S", ws({1: [c("C1", "x")]}, cols=col(3, 3, stored(150), hidden=True) + col(4, 4, 0)))]))
    assert v["decision"] == "pass", v["mistakes"]
    # a sheet whose own default width is over the cap: every column without its own width is that wide
    fmt = f'<sheetFormatPr defaultColWidth="{stored(100)}" defaultRowHeight="15"/>'
    v = run(book([("S", ws({1: [c("A1", "label")]}, fmt=fmt, cols=col(2, 2, stored(10))))]))
    assert locs(v) == ["S!A:A", "S!C:XFD"], locs(v)
    # adjacent failing columns with the same test form one mistake; a gap splits them
    v = run(book([("S", ws({1: [c("A1", "x")]}, cols=col(5, 6, stored(120)) + col(7, 7, stored(79)) + col(8, 8, stored(120))))]))
    assert locs(v) == ["S!E:F", "S!H:H"], locs(v)


def test_70_wrapped_unwrapped_and_merged():
    st = Styles()
    s_wrap = st.xf(wrap=True)
    # wrapped text needs at most the cap (80): a 120-wide column of wrapped notes passes, 170 fails
    rows = {1: [c("B1", digits(300), s_wrap)]}
    v = run(book([("S", ws(rows, cols=col(2, 2, stored(120))))], st))
    assert v["decision"] == "pass", v["mistakes"]
    v = run(book([("S", ws(rows, cols=col(2, 2, stored(170))))], st))
    assert v["decision"] == "fail" and "capped at 80" in v["mistakes"][0]["description"], v["mistakes"]
    # the widest line of wrapped text counts; unwrapped text counts in full
    v = run(book([("S", ws({1: [c("B1", digits(150))]}, cols=col(2, 2, stored(120))))]))
    assert v["decision"] == "pass", v["mistakes"]
    v = run(book([("S", ws({1: [c("B1", digits(20) + "\n" + digits(30), s_wrap)]}, cols=col(2, 2, stored(100))))], st))
    assert v["decision"] == "fail" and "needs 30.00" in v["mistakes"][0]["description"], v["mistakes"]
    # a title merged over B:D needs the span: B (100 wide) keeps what C and D (10 each) leave -> pass
    title = {1: [c("B1", digits(150))], 2: [c("B2", "x")]}
    cols = col(2, 2, stored(100)) + col(3, 4, stored(10))
    v = run(book([("S", ws(title, cols=cols, tail=merges("B1:D1")))]))
    assert v["decision"] == "pass", v["mistakes"]
    assert v["stats"]["second_pass_sheets"] == 1
    # ... and fails when C and D (70 each) would hold the title anyway
    cols = col(2, 2, stored(100)) + col(3, 4, stored(70))
    v = run(book([("S", ws(title, cols=cols, tail=merges("B1:D1")))]))
    assert v["decision"] == "fail" and locs(v) == ["S!B:B"] and "merged B1:D1" in v["mistakes"][0]["description"]
    # a covered cell is never displayed: its long value does not justify column C
    rows = {5: [c("B5", "x"), c("C5", digits(150))]}
    v = run(book([("S", ws(rows, cols=col(3, 3, stored(100)), tail=merges("B5:C5")))]))
    assert v["decision"] == "fail" and locs(v) == ["S!C:C"], v["mistakes"]
    v = run(book([("S", ws(rows, cols=col(3, 3, stored(100))))]))
    assert v["decision"] == "pass", v["mistakes"]


def test_70_rich_text_fonts_units():
    # a 22 pt run doubles the need: 30 digits at 22 pt need about 60 characters -> a 110 column passes
    v = run(book([("S", ws({1: [rich("B1", [(digits(30), 22)])]}, cols=col(2, 2, stored(110))))]))
    assert v["decision"] == "pass", v["mistakes"]
    v = run(book([("S", ws({1: [rich("B1", [(digits(30), None)])]}, cols=col(2, 2, stored(110))))]))
    assert v["decision"] == "fail", v["summary"]
    # units: Normal font Arial 14 (MDW 10 px): stored 90 -> 89.5 characters, over the cap
    st = Styles(("Arial", 14))
    v = run(book([("S", ws({1: [c("B1", "short")]}, cols=col(2, 2, 90)))], st))
    assert v["decision"] == "fail" and "B is 89.50 characters wide" in v["mistakes"][0]["description"]
    assert v["stats"]["column_unit_px"] == 10 and "Arial 14" in v["mistakes"][0]["description"]
    # a value in a hidden row is not displayed and justifies nothing
    v = run(book([("S", ws({1: ('hidden="1"', [c("B1", digits(70))]), 2: [c("B2", "x")]}, cols=col(2, 2, stored(100))))]))
    assert v["decision"] == "fail", v["summary"]


# ============================================================================ Test B (WIDE OUTLIER)
def _timeline(ae_af, content, *, right=True):
    """Toy T2 rebuilt: labels in D, a timeline I:BP at 13.0 (stored), AE:AF widened."""
    st = Styles(("Aptos Narrow", 11))
    s_num = st.xf("Arial", 10, numfmt="#,##0.00;\\(#,##0.00\\)")
    last = 68 if right else 32
    cells1 = [c("D1", "Revenue")] + [c(f"{index_to_col(k)}1", 1234.5, s_num) for k in range(9, last + 1)]
    cells2 = [c("AE2", content, s_num), c("AF2", content, s_num)] if not isinstance(content, str) else \
        [c("AE2", content), c("AF2", content)]
    cols = (col(4, 4, 40.0) + col(9, 30, 13.0) + col(31, 31, ae_af[0]) + col(32, 32, ae_af[1])
            + (col(33, 68, 13.0) if right else ""))
    return book([("Solution Model", ws({1: cells1, 2: cells2}, cols=cols))], st)


def test_70_outlier_rule():
    # equal AE:AF at 35.78 (judge kind) holding short numbers -> fail
    v = run(_timeline((35.77734375, 35.77734375), -1934955.38))
    assert v["decision"] == "fail" and locs(v) == ["'Solution Model'!AE:AF"], v["mistakes"]
    d = v["mistakes"][0]["description"]
    assert "WIDE OUTLIER" in d and "I:AD" in d and "AG:BP" in d and "(1,934,955.38)" in d, d
    assert v["stats"]["tests_failed"] == ["B"] and v["stats"]["n_outlier_tags"] == 1
    # toy T2 Fail's unequal AE 48.0 / AF 35.78: caught by the extension only
    p = _timeline((48.0, 35.77734375), -1934955.38)
    v = run(p)
    assert v["decision"] == "fail" and locs(v) == ["'Solution Model'!AE:AF"], v["mistakes"]
    assert v["stats"]["switches"]["outlier_exact_port"] == "pass" and v["stats"]["n_outlier_tags_unequal"] == 1
    with patched(M, OUTLIER_UNEQUAL_GROUPS=False):
        assert run(p)["decision"] == "pass"
    # content needing 60 % of the width passes (the 50-75 % band is reported; 0.75 would fail it)
    v = run(_timeline((35.77734375, 35.77734375), digits(21)))
    assert v["decision"] == "pass", v["mistakes"]
    assert v["stats"]["n_outlier_band_columns"] == 2 and v["stats"]["switches"]["outlier_share_0.75"] == "fail"
    # a wide pair at the right edge of the used range is not an outlier (the judge's rule)
    v = run(_timeline((35.77734375, 35.77734375), -1934955.38, right=False))
    assert v["decision"] == "pass" and v["stats"]["n_outlier_tags"] == 0, v["mistakes"]


# ============================================================================ no "long text left unwrapped" test
def test_70_long_unwrapped_text_is_not_a_width_problem():
    """Patrick 2026-10-04: Test C (200+ characters left unwrapped, spilling into empty cells) is dropped -
    unwrapped text is a problem only when it is cut off, which Sufficient column widths (69) grades.  Every case
    that failed Test C now passes; a long unwrapped text still justifies its column's width under Test A."""
    st = Styles()
    s_right = st.xf(horizontal="right")
    note = sentence(250)
    for rows, why in (({5: [c("B5", note)]}, "250 characters spilling right"),
                      ({5: [c("E5", note, s_right)]}, "right-aligned, spilling left"),
                      ({5: [c("B5", note, f='REPT("a",250)')]}, "a cached text formula result")):
        v = run(book([("Summary", ws(rows))], st))
        assert v["decision"] == "pass" and v["stats"]["tests_failed"] == [], (why, v["mistakes"])
    v = run(book([("Instructions", ws({7: [c("B7", note)]}))], st))
    assert v["decision"] == "pass", v["mistakes"]
    assert not any(k in v["stats"]["switches"] for k in ("test_c_off", "test_c_on", "brief_in_test_c"))
    assert not any(k in v["stats"] for k in ("long_text_candidates", "merged_long_text"))
    assert not any(k.startswith("long_text") for k in v["stats"]["options"])
    assert not hasattr(M, "LONG_TEXT_UNWRAPPED") and not hasattr(M, "LONG_TEXT_MIN_CHARS")
    # Test A still counts unwrapped text at its full width: 250 characters (about 233 Normal characters wide)
    # justify a 150-character column; a 150 column of short labels fails
    v = run(book([("Summary", ws({5: [c("B5", note)]}, cols=col(2, 2, stored(150))))], st))
    assert v["decision"] == "pass", v["mistakes"]
    v = run(book([("Summary", ws({5: [c("B5", "short label")]}, cols=col(2, 2, stored(150))))], st))
    assert v["decision"] == "fail" and v["stats"]["tests_failed"] == ["A"], v["mistakes"]


# ============================================================================ brief, hidden sheets
def test_70_brief_and_hidden_sheets():
    # the Instructions sheet is graded by Tests A and B (whole workbook); EXCLUDE_CASE_BRIEF drops it
    p = book([("Instructions", ws({1: [c("B1", "short")]}, cols=col(2, 2, stored(150)))), ("Model", ws({1: [c("A1", 1)]}))])
    v = run(p)
    assert v["decision"] == "fail" and locs(v) == ["Instructions!B:B"] and v["stats"]["switches"]["brief_excluded"] == "pass"
    with patched(M, EXCLUDE_CASE_BRIEF=True):
        assert run(p)["decision"] == "pass"
    # hidden sheets are graded
    v = run(book([("Model", ws({1: [c("A1", 1)]})), ("Notes", ws({1: [c("A1", "x")]}, cols=col(3, 3, stored(120))))],
                 states={1: "hidden"}))
    assert locs(v) == ["Notes!C:C"] and "(hidden sheet)" in v["mistakes"][0]["description"]


# ============================================================================ values
def test_70_formula_values():
    st = Styles()
    s_wrap = st.xf(wrap=True)
    wide = col(8, 8, stored(150))
    # openpyxl-labelled, judged column holding only untrusted formula results -> cannot decide
    x = ws({1: [c("H1", None, s_wrap, f='"Enterprise Value"')], 2: [c("H2", 5.0, s_wrap, f="1+4")]}, cols=wide)
    graded("untrusted_value", lambda: run(book([("S", x)], st, app=OPENPYXL)), "cannot decide S!H1", "untrusted", "writer=openpyxl")
    # a constant that needs half the width settles the column: the untrusted values are not needed
    x2 = ws({1: [c("H1", None, s_wrap, f='"x"')], 3: [c("H3", digits(80))]}, cols=wide)
    v = run(book([("S", x2)], st, app=OPENPYXL))
    assert v["decision"] == "pass" and v["stats"]["undecided_cells"] == 1, v           # H1 skipped (recorded)
    assert v["stats"]["defaults"]["untrusted_value"]["count"] == 1, v["stats"]
    # the untrusted value skipped (Patrick 2026-10-05: every attempt graded), column H holds nothing measurable
    # and is graded as such: it fails beside J
    x3 = ws({1: [c("H1", None, s_wrap, f='"x"')]}, cols=wide + col(10, 10, stored(120)))
    v = run(book([("S", x3)], st, app=OPENPYXL))
    assert v["decision"] == "fail" and locs(v) == ["S!H:H", "S!J:J"] and v["stats"]["undecided_cells"] >= 1, locs(v)
    assert "were skipped" in v["summary"], v["summary"]
    with patched(M, UNTRUSTED_ONLY_IF_VERDICT_NEEDS=False):
        graded("untrusted_value", lambda: run(book([("S", x3)], st, app=OPENPYXL)), "cannot decide")
    # trusted caches (unknown writer) are read: short cached results in a 150 column fail; a formula
    # without any cache is untrusted whatever the writer
    xc = ws({1: [c("H1", "Enterprise Value", s_wrap, f='"Enterprise Value"')], 2: [c("H2", 5.0, s_wrap, f="1+4")]},
            cols=wide)
    v = run(book([("S", xc)], st))
    assert v["decision"] == "fail" and locs(v) == ["S!H:H"] and v["stats"]["formula_values_read"] >= 2
    assert "Enterprise Value" in v["mistakes"][0]["description"]
    graded("untrusted_value", lambda: run(book([("S", x)], st)), "cannot decide S!H1", "writer=unknown")
    # an untrusted formula outside the judged columns is never read (no Test C any more): an unwrapped formula
    # that could spill, a blocked one and a wrapped one all pass without reading a value
    v = run(book([("S", ws({1: [c("B1", None, f="A1&A1")], 2: [c("B2", None, f="A1&A1"), c("C2", 1)],
                            3: [c("B3", None, s_wrap, f="A1")]}))], st, app=OPENPYXL))
    assert v["decision"] == "pass" and v["stats"]["undecided_cells"] == 0 and v["stats"]["formula_values_read"] == 0, v


def test_70_unverified_number_format():
    """66-S3 in the core (2026-10-04): a number under a code mixing digit placeholders with unquoted date letters
    ('0 days') renders certain=False.  70 used to measure the engine's date reading of it; now such a cell is
    undecided like an untrusted value: decisive only when nothing else settles the column."""
    st = Styles()
    days = st.xf(numfmt="0 days")
    quoted = st.xf(numfmt='0" days"')
    wide = col(8, 8, stored(150))
    x = ws({1: [c("H1", 25.0, days)]}, cols=wide)
    graded("unverified_number_format", lambda: run(book([("S", x)], st)), "cannot decide S!H1", "'0 days' is not verified")
    # a constant that needs half the width settles the column: the unverified cell is not needed
    v = run(book([("S", ws({1: [c("H1", 25.0, days)], 3: [c("H3", digits(80))]}, cols=wide))], st))
    assert v["decision"] == "pass" and v["stats"]["undecided_cells"] == 0, v
    # the quoted unit is verified and measured: '25 days' in a 150-character column is excessive
    v = run(book([("S", ws({1: [c("H1", 25.0, quoted)]}, cols=wide))], st))
    assert v["decision"] == "fail" and locs(v) == ["S!H:H"], v


TESTS = [test_70_geometry_and_text, test_70_wide_outlier_port, test_70_judge_port_parity, test_70_cap_rule,
         test_70_empty_hidden_and_default_columns, test_70_wrapped_unwrapped_and_merged,
         test_70_rich_text_fonts_units, test_70_outlier_rule, test_70_long_unwrapped_text_is_not_a_width_problem,
         test_70_brief_and_hidden_sheets, test_70_formula_values, test_70_unverified_number_format]


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
