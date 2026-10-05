"""Unit tests for check 29 (Clean Name Manager): synthetic micro-workbooks for every trap in
the toys / handoff / report §4b.

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_names
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from xml.sax.saxutils import escape

from detchecks.api import grade
from detchecks.checks import REGISTRY
from detchecks.core.refs import col_to_index
from detchecks.errors import GradingError

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "names")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"
KEY = REGISTRY[29].key

_TMP = None


def tmp(name):
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_names_", dir=SCRATCH)
    return os.path.join(_TMP, name)


# ============================================================================ micro builder
def c(ref, f=None, v=None, s=None, shared=None):
    """One <c>.  f: formula text (None = constant); shared=(si, ref or None): a child when f is
    None; s: inline text value."""
    col = "".join(ch for ch in ref if ch.isalpha())
    row = int("".join(ch for ch in ref if ch.isdigit()))
    inner = ""
    if shared is not None:
        si, sref = shared
        r = f' ref="{sref}"' if sref else ""
        inner = f'<f t="shared" si="{si}"{r}>{escape(f)}</f>' if f else f'<f t="shared" si="{si}"/>'
    elif f is not None:
        inner = f"<f>{escape(f)}</f>"
    if s is not None:
        return row, col_to_index(col), f'<c r="{ref}" t="inlineStr">{inner}<is><t>{escape(s)}</t></is></c>'
    return row, col_to_index(col), f'<c r="{ref}">{inner}<v>{v if v is not None else 0}</v></c>'


def sheet(cells=(), tail=""):
    rows = {}
    for r, k, x in cells:
        rows.setdefault(r, []).append((k, x))
    body = "".join(f'<row r="{r}">' + "".join(x for _, x in sorted(rows[r])) + "</row>" for r in sorted(rows))
    return f"<sheetData>{body}</sheetData>{tail}"


def cf(sqref, formula=None, cfvo=None):
    if cfvo is not None:
        rule = (f'<cfRule type="colorScale" priority="1"><colorScale><cfvo type="min"/>'
                f'<cfvo type="formula" val="{escape(cfvo)}"/><color rgb="FFFF0000"/><color rgb="FF00FF00"/>'
                f"</colorScale></cfRule>")
    else:
        rule = f'<cfRule type="expression" priority="1" dxfId="0"><formula>{escape(formula)}</formula></cfRule>'
    return f'<conditionalFormatting sqref="{sqref}">{rule}</conditionalFormatting>'


def dv(sqref, f1):
    return (f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="{sqref}">'
            f"<formula1>{escape(f1)}</formula1></dataValidation></dataValidations>")


def x14dv(sqref, f1):
    return (f'<extLst><ext uri="{{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}}" xmlns:x14="{X14}">'
            f'<x14:dataValidations count="1" xmlns:xm="{XM}"><x14:dataValidation type="list">'
            f"<x14:formula1><xm:f>{escape(f1)}</xm:f></x14:formula1><xm:sqref>{sqref}</xm:sqref>"
            f"</x14:dataValidation></x14:dataValidations></ext></extLst>")


def sparkline(host, f):
    return (f'<extLst><ext uri="{{05C60535-1F16-4fd2-B633-F4F36F0B64E0}}" xmlns:x14="{X14}">'
            f'<x14:sparklineGroups xmlns:xm="{XM}"><x14:sparklineGroup><x14:sparklines><x14:sparkline>'
            f"<xm:f>{escape(f)}</xm:f><xm:sqref>{host}</xm:sqref></x14:sparkline></x14:sparklines>"
            f"</x14:sparklineGroup></x14:sparklineGroups></ext></extLst>")


def book(name, sheets, names=(), parts=None, wb_rels="", sheet_rels=None, ct_extra=""):
    """sheets: [(name, xml[, state])]; names: [(name, text, attrs or None)]; parts: extra zip
    members; sheet_rels: {sheet index: rels body}."""
    path = tmp(name)
    parts = dict(parts or {})
    sheet_rels = sheet_rels or {}
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("[Content_Types].xml",
               '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               f"{ct_extra}</Types>")
    z.writestr("_rels/.rels", f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RT}/officeDocument" '
                              f'Target="xl/workbook.xml"/></Relationships>')
    sh, wr = [], []
    for i, sd in enumerate(sheets):
        nm, xml = sd[0], sd[1]
        st = f' state="{sd[2]}"' if len(sd) > 2 and sd[2] else ""
        sh.append(f'<sheet name="{escape(nm, {chr(34): "&quot;"})}" sheetId="{i + 1}"{st} r:id="rId{i + 1}"/>')
        wr.append(f'<Relationship Id="rId{i + 1}" Type="{RT}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>')
        z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<worksheet xmlns="{MAIN}" '
                   f'xmlns:r="{REL}">{xml}</worksheet>')
        if i in sheet_rels:
            z.writestr(f"xl/worksheets/_rels/sheet{i + 1}.xml.rels",
                       f'<Relationships xmlns="{PKG}">{sheet_rels[i]}</Relationships>')
    dn = ""
    if names:
        dn = "<definedNames>" + "".join(
            f'<definedName name="{escape(n)}"' + "".join(f' {k}="{v}"' for k, v in (a or {}).items())
            + f">{escape(t)}</definedName>" for n, t, a in names) + "</definedNames>"
    z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<workbook xmlns="{MAIN}" '
                                  f'xmlns:r="{REL}"><sheets>{"".join(sh)}</sheets>{dn}</workbook>')
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG}">{"".join(wr)}{wb_rels}</Relationships>')
    for k, v in parts.items():
        z.writestr(k, v)
    z.close()
    return path


def run(path, **kw):
    return grade(path, checks=[29], **kw)[KEY]


def locs(v):
    return [m["location"] for m in v["mistakes"]]


def expect(v, decision, locations=None):
    assert v["decision"] == decision, (decision, v["summary"], v["mistakes"])
    if locations is not None:
        assert locs(v) == locations, (locations, locs(v), [m["description"] for m in v["mistakes"]])


def skipped(v, decision, locations=None):
    """Patrick 2026-10-05 (every attempt graded): unparsable formula text is skipped and recorded."""
    expect(v, decision, locations)
    assert v["stats"]["defaults"]["unparsable_formula"]["count"] >= 1, v["stats"]
    return v


def raises(fn, *needles):
    try:
        fn()
    except GradingError as e:
        msg = str(e) + " " + " ".join(str(x) for x in e.failures.values())
        for nd in needles:
            assert nd in msg, (nd, msg[:800])
        return e
    raise AssertionError(f"expected GradingError containing {needles}")


def one(name, cells, names, tail="", **kw):
    """One sheet 'S' plus a second sheet 'T' (for scope tests)."""
    return book(name, [("S", sheet(cells, tail)), ("T", sheet())], names=names, **kw)


# ============================================================================ tests
def test_counting_hidden_builtin():
    """T1/T2/T3 traps: hidden names (also broken ones) and built-ins never count; a visible
    add-in name counts like any other; usage is per name token, not per target cell."""
    names = [("Tax_rate", "S!$B$1", None),
             ("_xlnm.Print_Area", "S!$A$1:$C$9", {"localSheetId": "0"}),
             ("solver_opt", "Instructions!#REF!", {"hidden": "1"}),       # hidden AND broken
             ("IQ_DNTM", "700000", {"hidden": "1"}),
             ("RiskNumIterations", "100000", {"hidden": "1"})]
    p = one("h1.xlsx", [c("A1", "B1*(1-Tax_rate)")], names)
    v = run(p)
    expect(v, "pass", [])
    assert v["stats"]["n_hidden_not_counted"] == 3 and v["stats"]["n_builtin_not_counted"] == 1
    # the same add-in name made visible fails (filter by the hidden flag, not by a prefix)
    names2 = [n if n[0] != "RiskNumIterations" else ("RiskNumIterations", "100000", None) for n in names]
    expect(run(one("h2.xlsx", [c("A1", "B1*(1-Tax_rate)")], names2)), "fail", ["Name Manager: RiskNumIterations"])
    # hidden="0" is visible; a copy pointing at the same cell as a used name is still unused
    names3 = names + [("Tax_rate_v1", "S!$B$1", {"hidden": "0"})]
    v = run(one("h3.xlsx", [c("A1", "B1*(1-Tax_rate)")], names3))
    expect(v, "fail", ["Name Manager: Tax_rate_v1"])
    assert "is unused" in v["mistakes"][0]["description"]
    # no visible names at all: nothing to check
    v = run(one("h4.xlsx", [c("A1", "1")], [("IQ_X", "1", {"hidden": "1"})]))
    expect(v, "pass", [])
    assert v["summary"].startswith("No visible defined names")
    # bare sheet-local Print_Area / Print_Titles (ChatGPT writer) are built-ins; a global one is not
    v = run(one("h5.xlsx", [c("A1", "1")], [("Print_Area", "'S'!$A$1:$H$55", {"localSheetId": "0"}),
                                            ("Print_Titles", "'S'!$1:$7", {"localSheetId": "0"})]))
    expect(v, "pass", [])
    expect(run(one("h6.xlsx", [c("A1", "1")], [("Print_Area", "S!$A$1", None)])), "fail", ["Name Manager: Print_Area"])


def test_broken():
    """T2: #REF! in a visible definition fails even when used; inside a string it does not."""
    names = [("Bad", "'S'!#REF!", None), ("Bad2", "S!$B$2+#REF!", None), ("Txt", '"see #REF! note"', None),
             ("Gone", "Missing!$A$1", None), ("NA", "#N/A", None), ("Empty", "", None)]
    p = one("b1.xlsx", [c("A1", "Bad+Bad2+LEN(Txt)+Gone+NA+Empty")], names)
    v = run(p)
    expect(v, "fail", ["Name Manager: Bad", "Name Manager: Bad2", "Name Manager: Gone", "Name Manager: Empty"])
    d = {m["location"]: m["description"] for m in v["mistakes"]}
    assert "contains #REF!" in d["Name Manager: Bad"] and "unused" not in d["Name Manager: Bad"]
    assert "'Missing'" in d["Name Manager: Gone"]
    assert v["stats"]["error_constant_names_not_broken"] == ["NA"]
    # broken and unused: one mistake naming both
    v = run(one("b2.xlsx", [c("A1", "1")], [("Bad", "'S'!#REF!", None)]))
    expect(v, "fail", ["Name Manager: Bad"])
    assert "broken" in v["mistakes"][0]["description"] and "also unused" in v["mistakes"][0]["description"]
    # 3-D reference over existing sheets and an external workbook reference are not missing sheets
    v = run(one("b3.xlsx", [c("A1", "SUM(ThreeD)+Ext")], [("ThreeD", "S:T!$A$1", None),
                                                           ("Ext", "[1]Other!$A$1", None)]))
    expect(v, "pass", [])


def test_lambda_and_function_names():
    """T4: a LAMBDA name is used when called; its unused _v1 copy (a longer name with the used
    name as prefix) fails.  Year vs YEAR(): a built-in call is not the name."""
    lam = "_xlfn.LAMBDA(_xlpm.x,_xlpm.y,_xlpm.x*(1+_xlpm.y))"
    p = one("l1.xlsx", [c("A1", "F_twostageTV(B1,B2)")], [("F_twostageTV", lam, None), ("F_twostageTV_v1", lam, None)])
    expect(run(p), "fail", ["Name Manager: F_twostageTV_v1"])
    expect(run(one("l2.xlsx", [c("A1", "F_twostageTV(B1,B2)")], [("F_twostageTV", lam, None)])), "pass", [])
    # Year / Rate unused while YEAR() / RATE() are called; companion Year+1 uses it
    p = one("l3.xlsx", [c("A1", "YEAR(B1)+RATE(10,-1,100)")], [("Year", "S!$C$1", None), ("Rate", "S!$C$2", None)])
    expect(run(p), "fail", ["Name Manager: Year", "Name Manager: Rate"])          # Name Manager order
    expect(run(one("l4.xlsx", [c("A1", "YEAR(B1)+Year+1")], [("Year", "S!$C$1", None)])), "pass", [])
    # a recursive LAMBDA that only calls itself is unused
    rec = "_xlfn.LAMBDA(_xlpm.n,IF(_xlpm.n<=1,1,_xlpm.n*FACT2(_xlpm.n-1)))"
    expect(run(one("l5.xlsx", [c("A1", "1")], [("FACT2", rec, None)])), "fail", ["Name Manager: FACT2"])
    # plain (unprefixed) LAMBDA definitions as some writers store them; LAMBDAs calling each other
    p = one("l6.xlsx", [c("A1", "OUTER(2)")], [("OUTER", "LAMBDA(x,INNER(x)+1)", None),
                                               ("INNER", "LAMBDA(x,x*2)", None)])
    expect(run(p), "pass", [])


def test_prefix_and_tokens():
    """Prefix collisions, string literals, text cells, LET parameters, cell-address names."""
    p = one("t1.xlsx", [c("A1", "RateHigh*2"), c("A2", '"Rate"&"x"'), c("A3", s="Rate is the base rate"),
                        c("A4", "_xlfn.LET(_xlpm.Rate,5,_xlpm.Rate*2)"), c("A5", "LET( x, 1, Rate2, 2, x+Rate2)")],
            [("Rate", "S!$B$1", None), ("RateHigh", "S!$B$2", None), ("x", "S!$B$3", None),
             ("Rate2", "S!$B$4", None)])
    expect(run(p), "fail", ["Name Manager: Rate", "Name Manager: x", "Name Manager: Rate2"])
    # _xlpm.rate parameter vs the defined name Rate in the same LET
    expect(run(one("t2.xlsx", [c("A1", "_xlfn.LET(_xlpm.rate,0.05,_xlpm.rate*Rate)")], [("Rate", "S!$B$1", None)])),
           "pass", [])
    # Lim1 is cell LIM1: the name is unused, and the message says why
    v = run(one("t3.xlsx", [c("A1", "Lim1*2")], [("Lim1", "S!$C$54", None)]))
    expect(v, "fail", ["Name Manager: Lim1"])
    assert "cell address LIM1" in v["mistakes"][0]["description"]
    # tokenizer boundaries: names with '.', '\', digits; a long cell-like name beyond the grid
    p = one("t4.xlsx", [c("A1", r"Tax_2024+my.rate+\lead+AB2000000")],
            [("Tax_2024", "S!$B$1", None), ("my.rate", "S!$B$2", None), (r"\lead", "S!$B$3", None),
             ("AB2000000", "S!$B$4", None)])
    expect(run(p), "pass", [])
    # names are case-insensitive; an external workbook's name is not this workbook's name
    expect(run(one("t5.xlsx", [c("A1", "RATE_X*2")], [("Rate_x", "S!$B$1", None)])), "pass", [])
    expect(run(one("t6.xlsx", [c("A1", "[1]!Rate*2")], [("Rate", "S!$B$1", None)])), "fail", ["Name Manager: Rate"])


def test_scope():
    """Sheet-local vs global names with the same spelling (Excel scope rules)."""
    names = [("Rate", "S!$B$1", None), ("Rate", "T!$B$1", {"localSheetId": "1"})]
    # formula on S uses the global; T's local Rate is unused
    v = run(book("s1.xlsx", [("S", sheet([c("A1", "Rate*2")])), ("T", sheet())], names=names))
    expect(v, "fail", ["Name Manager: T!Rate"])
    # formula on T uses its local (shadowing the global): the global is unused
    v = run(book("s2.xlsx", [("S", sheet()), ("T", sheet([c("A1", "Rate*2")]))], names=names))
    expect(v, "fail", ["Name Manager: Rate"])
    # qualified use from another sheet reaches the local; both used
    v = run(book("s3.xlsx", [("S", sheet([c("A1", "Rate+T!Rate")])), ("T", sheet())], names=names))
    expect(v, "pass", [])
    # quoted sheet names; a local name of a sheet with an apostrophe
    v = run(book("s4.xlsx", [("Bob's Q1", sheet([c("A1", "Loc*2")])), ("U", sheet([c("A1", "'Bob''s Q1'!LocB")]))],
                 names=[("Loc", "'Bob''s Q1'!$B$1", {"localSheetId": "0"}),
                        ("LocB", "'Bob''s Q1'!$B$2", {"localSheetId": "0"})]))
    expect(v, "pass", [])
    # a bad localSheetId cannot be resolved: raise (no guess)
    raises(lambda: run(one("s5.xlsx", [c("A1", "1")], [("X", "S!$A$1", {"localSheetId": "7"})])), "localSheetId")


def test_consumers_in_sheet():
    """DV (main and x14, T5), CF formula and threshold, sparkline, hyperlink, literal
    INDIRECT/HYPERLINK, shared formula, hidden sheet."""
    nm = [("Lst", "S!$D$8:$D$10", None)]
    expect(run(one("c1.xlsx", [c("D6", s="Base")], nm, tail=dv("D6", "Lst"))), "pass", [])
    expect(run(one("c2.xlsx", [c("D6", s="Base")], nm, tail=x14dv("D6", "Lst"))), "pass", [])
    expect(run(one("c3.xlsx", [c("D6", s="Base")], nm)), "fail", ["Name Manager: Lst"])        # T5 Fail
    expect(run(one("c4.xlsx", [], nm, tail=cf("A1:A9", "A1>MAX(Lst)"))), "pass", [])
    expect(run(one("c5.xlsx", [], nm, tail=cf("A1:A9", cfvo="MAX(Lst)"))), "pass", [])
    expect(run(one("c6.xlsx", [], nm, tail=sparkline("E1", "Lst"))), "pass", [])
    hl = '<hyperlinks><hyperlink ref="A1" location="Lst" display="go"/></hyperlinks>'
    expect(run(one("c7.xlsx", [c("A1", s="go")], nm, tail=hl)), "pass", [])
    hl2 = '<hyperlinks><hyperlink ref="A1" location="\'S\'!A1" display="go"/></hyperlinks>'
    expect(run(one("c7b.xlsx", [c("A1", s="go")], nm, tail=hl2)), "fail", ["Name Manager: Lst"])
    for i, f in enumerate(['SUM(INDIRECT("Lst"))', 'SUM(INDIRECT("L"&"st"))', 'HYPERLINK("#Lst","go")',
                           'SUM(INDIRECT("S!Lst"))']):
        expect(run(one(f"c8{i}.xlsx", [c("A1", f)], nm)), "pass", [])
    # shared formula: the master carries the name, children have no text
    cells = [c("B2", "A2*Lst", shared=("0", "B2:B9"))] + [c(f"B{r}", shared=("0", None)) for r in range(3, 10)]
    expect(run(one("c9.xlsx", cells, nm)), "pass", [])
    # a formula on a very hidden sheet still uses the name
    v = run(book("c10.xlsx", [("S", sheet()), ("H", sheet([c("A1", "SUM(Lst)")]), "veryHidden")], names=nm))
    expect(v, "pass", [])


def _drawing_parts(textlink, chart_f):
    return {"xl/drawings/drawing1.xml":
            f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
            f'<xdr:sp textlink="{escape(textlink)}"/></xdr:wsDr>',
            "xl/drawings/_rels/drawing1.xml.rels":
            f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RT}/chart" Target="../charts/chart1.xml"/>'
            f"</Relationships>",
            "xl/charts/chart1.xml": f'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
                                    f"<c:f>{escape(chart_f)}</c:f></c:chartSpace>"}


def test_consumers_side_parts():
    """Charts, shape text links, form controls (ctrlProps, VML), tables, pivot caches, slicers."""
    srel = f'<Relationship Id="rIdD" Type="{RT}/drawing" Target="../drawings/drawing1.xml"/>'
    nm = [("DynRng", "OFFSET(S!$A$1,0,0,COUNTA(S!$A:$A),1)", None), ("Lbl", "S!$C$1", None)]
    p = one("p1.xlsx", [c("A1", v=1)], nm, tail='<drawing r:id="rIdD"/>',
            parts=_drawing_parts("Lbl", "[0]!DynRng"), sheet_rels={0: srel})
    expect(run(p), "pass", [])
    p = one("p2.xlsx", [c("A1", v=1)], nm, tail='<drawing r:id="rIdD"/>',
            parts=_drawing_parts("$A$1", "S!DynRng"), sheet_rels={0: srel})
    expect(run(p), "fail", ["Name Manager: Lbl"])
    # form control (ctrlProp) and legacy VML control
    nm2 = [("Choices", "S!$B$1:$B$3", None), ("Pick", "S!$C$1", None)]
    srel2 = (f'<Relationship Id="rIdC" Type="{RT}/ctrlProp" Target="../ctrlProps/ctrlProp1.xml"/>'
             f'<Relationship Id="rIdV" Type="{RT}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>')
    parts2 = {"xl/ctrlProps/ctrlProp1.xml": f'<formControlPr xmlns="{X14}" objectType="Drop" fmlaRange="Choices"/>',
              "xl/drawings/vmlDrawing1.vml": '<xml xmlns:x="urn:schemas-microsoft-com:office:excel"><x:ClientData '
                                             'ObjectType="Drop"><x:FmlaLink>Pick</x:FmlaLink></x:ClientData></xml>'}
    expect(run(one("p3.xlsx", [], nm2, tail='<legacyDrawing r:id="rIdV"/>', parts=parts2, sheet_rels={0: srel2})),
           "pass", [])
    # table calculated column
    srel3 = f'<Relationship Id="rIdT" Type="{RT}/table" Target="../tables/table1.xml"/>'
    tbl = (f'<table xmlns="{MAIN}" id="1" name="T1" displayName="T1" ref="A1:B3"><tableColumns count="2">'
           f'<tableColumn id="1" name="a"/><tableColumn id="2" name="b"><calculatedColumnFormula>[@a]*Fx'
           f"</calculatedColumnFormula></tableColumn></tableColumns></table>")
    expect(run(one("p4.xlsx", [], [("Fx", "S!$D$1", None)], tail='<tableParts count="1"><tablePart r:id="rIdT"/>'
                   '</tableParts>', parts={"xl/tables/table1.xml": tbl}, sheet_rels={0: srel3})), "pass", [])
    # pivot cache built on a named range
    pc = (f'<pivotCacheDefinition xmlns="{MAIN}" xmlns:r="{REL}"><cacheSource type="worksheet">'
          f'<worksheetSource name="SalesData"/></cacheSource></pivotCacheDefinition>')
    p = book("p5.xlsx", [("S", sheet())], names=[("SalesData", "S!$A$1:$C$9", None)],
             parts={"xl/pivotCache/pivotCacheDefinition1.xml": pc},
             wb_rels=f'<Relationship Id="rIdP" Type="{RT}/pivotCacheDefinition" '
                     f'Target="pivotCache/pivotCacheDefinition1.xml"/>')
    # pivotCaches element in workbook.xml
    with zipfile.ZipFile(p) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["xl/workbook.xml"] = items["xl/workbook.xml"].replace(
        b"</definedNames>", b'</definedNames><pivotCaches><pivotCache cacheId="1" r:id="rIdP"/></pivotCaches>')
    with zipfile.ZipFile(p, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    expect(run(p), "pass", [])
    # Excel slicer name (=#N/A by design) is used by its slicer cache; without the cache it is unused
    sc = '<slicerCacheDefinition xmlns="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" ' \
         'name="Slicer_Region" sourceName="Region"/>'
    slicer_rel = (f'<Relationship Id="rIdS" Type="http://schemas.microsoft.com/office/2007/relationships/slicerCache" '
                  f'Target="slicerCaches/slicerCache1.xml"/>')
    nm3 = [("Slicer_Region", "#N/A", None)]
    expect(run(one("p6.xlsx", [], nm3, parts={"xl/slicerCaches/slicerCache1.xml": sc}, wb_rels=slicer_rel)),
           "pass", [])
    expect(run(one("p7.xlsx", [], nm3)), "fail", ["Name Manager: Slicer_Region"])


def test_names_using_names():
    """Transitive use, cycles, roots (hidden / built-in names are consumers)."""
    base = [("A", "S!$B$1", None), ("B", "A*2", None)]
    # A used through B which a cell uses
    expect(run(one("n1.xlsx", [c("A1", "B+1")], base)), "pass", [])
    # A used only by B, B unused: both fail; A's message names B
    v = run(one("n2.xlsx", [c("A1", "1")], base))
    expect(v, "fail", ["Name Manager: A", "Name Manager: B"])
    assert "only names that are themselves unused refer to it (B)" in v["mistakes"][0]["description"]
    # a cycle of two names nothing else uses
    expect(run(one("n3.xlsx", [c("A1", "1")], [("P", "Q+1", None), ("Q", "P+1", None)])), "fail",
           ["Name Manager: P", "Name Manager: Q"])
    # used only by a hidden name (add-in, e.g. solver_adj) or by a built-in: used
    expect(run(one("n4.xlsx", [c("A1", "1")], [("DecisionVar", "S!$B$1", None),
                                               ("solver_adj", "DecisionVar", {"hidden": "1"})])), "pass", [])
    expect(run(one("n5.xlsx", [c("A1", "1")], [("PrintRng", "S!$A$1:$C$9", None),
                                               ("_xlnm.Print_Area", "PrintRng", {"localSheetId": "0"})])), "pass", [])
    # a chain through a hidden helper the cells use
    expect(run(one("n6.xlsx", [c("A1", "Helper")], [("Helper", "Base*2", {"hidden": "1"}),
                                                    ("Base", "S!$B$1", None)])), "pass", [])


def test_gates():
    """No guess: consumers that cannot be read raise only when a name would be flagged unused."""
    nm = [("Rate", "S!$B$1", None)]
    # dynamic INDIRECT whose text could be the name (a text cell holds it)
    p = one("g1.xlsx", [c("A1", s="Rate"), c("A2", "SUM(INDIRECT(A1))")], nm)
    raises(lambda: run(p), "built from text", "Rate")
    # ... or a literal fragment of the computed text matches
    p = one("g2.xlsx", [c("A2", 'SUM(INDIRECT("Rate_"&A1))')], [("Rate_2024", "S!$B$1", None)])
    raises(lambda: run(p), "Rate_2024", "can produce it")
    # two-letter / column-like fragments are ignored ("'Data'!B"&n builds a cell address)
    p = one("g2b.xlsx", [c("A2", 'SUM(INDIRECT("\'Data\'!B"&A1))')], [("Rate_2024", "S!$B$1", None)])
    expect(run(p), "fail", ["Name Manager: Rate_2024"])
    # sheet-switching INDIRECT cannot produce an unrelated name: graded normally
    p = one("g3.xlsx", [c("A1", s="T"), c("A2", "INDIRECT(\"'\"&A1&\"'!B5\")")], [("Unrelated", "S!$B$1", None)])
    expect(run(p), "fail", ["Name Manager: Unrelated"])
    # the same dynamic INDIRECT is irrelevant when every name is used
    p = one("g4.xlsx", [c("A1", s="Rate"), c("A2", "SUM(INDIRECT(A1))+Rate")], nm)
    expect(run(p), "pass", [])
    # VBA project
    vba = f'<Relationship Id="rIdV" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" ' \
          f'Target="vbaProject.bin"/>'
    p = one("g5.xlsx", [c("A1", "1")], nm, parts={"xl/vbaProject.bin": b"\x00"}, wb_rels=vba)
    raises(lambda: run(p), "VBA")
    expect(run(one("g6.xlsx", [c("A1", "Rate")], nm, parts={"xl/vbaProject.bin": b"\x00"}, wb_rels=vba)), "pass", [])
    # a macro name
    raises(lambda: run(one("g7.xlsx", [c("A1", "1")], [("Macro1", "S!$A$1", {"function": "1", "xlm": "1"})])),
           "macro name")
    # malformed formula text raises only when the check needs to parse it
    skipped(run(one("g8.xlsx", [c("A1", "SUM(Rate")], nm)), "fail", ["Name Manager: Rate"])   # skipped since 2026-10-05
    expect(run(one("g9.xlsx", [c("A1", "SUM(C16:C22"), c("A2", "Rate")], nm)), "pass", [])


def test_early_stop_and_stats():
    """Once every counted name is used, later sheets are not streamed."""
    sheets = [("S", sheet([c("A1", "Rate")])), ("Big", sheet([c(f"A{r}", "1+1") for r in range(1, 50)]))]
    v = run(book("e1.xlsx", sheets, names=[("Rate", "S!$B$1", None)]))
    expect(v, "pass", [])
    assert v["stats"]["n_formula_texts_scanned"] == 1, v["stats"]
    assert v["stats"]["counted_names"] == [{"name": "Rate", "scope": None, "used_by": "S!A1", "problems": []}]


def test_review_dynamic_text_refs():
    """Review findings 29-gate-texthit-overbroad, 29-gate-fragment-before-bang and
    29-dynamic-indirect-helper-cell-false-fail: a run-time reference raises only when the
    shape of its text can produce the unused name's spelling."""
    nm = [("WACC", "S!$B$1", None)]
    # sheet-switching INDIRECT: the text always ends in '!B5' (a cell), even with a 'WACC' label
    p = one("r1.xlsx", [c("A1", s="T"), c("A3", s="WACC"), c("A2", "INDIRECT(\"'\"&A1&\"'!B5\")")], nm)
    v = run(p)
    expect(v, "fail", ["Name Manager: WACC"])
    assert v["stats"]["dynamic_text_refs"]["cannot_name"] == 1, v["stats"]
    # a literal before a later '!' is part of the sheet name, never of the name
    p = one("r2.xlsx", [c("A1", v=1), c("A2", 'INDIRECT("Data"&A1&"!B5")')], [("DataStart", "S!$B$1", None)])
    expect(run(p), "fail", ["Name Manager: DataStart"])
    # ADDRESS() yields a cell address; a HYPERLINK to a URL held in a cell reaches no name
    expect(run(one("r3.xlsx", [c("A3", s="WACC"), c("A2", "INDIRECT(ADDRESS(1,2))")], nm)), "fail",
           ["Name Manager: WACC"])
    expect(run(one("r3b.xlsx", [c("A3", s="WACC"), c("A2", 'INDIRECT(ADDRESS(1,2,1,TRUE,A3)&":"&ADDRESS(5,2))')],
                   nm)), "fail", ["Name Manager: WACC"])
    expect(run(one("r4.xlsx", [c("B1", s="https://example.com"), c("A3", s="WACC"), c("A2", 'HYPERLINK(B1,"src")')],
                   nm)), "fail", ["Name Manager: WACC"])
    expect(run(one("r4b.xlsx", [c("A3", s="WACC"), c("A2", 'HYPERLINK("https://x.com/"&A3,"src")')], nm)),
           "fail", ["Name Manager: WACC"])
    # ... but once some text starts with '#', HYPERLINK(B1) may jump to any name: raise
    p = one("r4c.xlsx", [c("B1", s="#WACC"), c("A2", 'HYPERLINK(B1,"go")')], nm)
    raises(lambda: run(p), "WACC", "can produce it")
    # fixed name part on a computed sheet: the only Rate is used
    p = one("r5.xlsx", [c("A1", s="S"), c("A2", "INDIRECT(\"'\"&A1&\"'!\"&\"Rate\")")], [("Rate", "S!$B$1", None)])
    v = run(p)
    expect(v, "pass", [])
    assert "its text always ends in the name" in v["stats"]["counted_names"][0]["used_by"], v["stats"]
    # ... with a global and a sheet-local Rate the target depends on the sheet: raise if one looks unused
    names = [("Rate", "S!$B$1", None), ("Rate", "T!$B$1", {"localSheetId": "1"})]
    p = book("r5b.xlsx", [("S", sheet([c("A1", s="S"), c("A2", "INDIRECT(A1&\"!Rate\")")])), ("T", sheet())],
             names=names)
    raises(lambda: run(p), "Rate", "more than one name has that spelling")
    # the INDIRECT text built in a helper formula cell: any name may come out: raise (never fail)
    two = [("Rate_Base", "S!$C$1", None), ("Rate_High", "S!$C$2", None)]
    p = one("r6.xlsx", [c("A1", '"Rate_"&B1'), c("B1", s="Base"), c("A2", "INDIRECT(A1)*100")], two)
    raises(lambda: run(p), "Rate_Base", "Rate_High", "INDIRECT(\u2026)")
    p = one("r6b.xlsx", [c("A1", 'CHOOSE(B1,"Rate_Base","Rate_High")'), c("B1", v=1), c("A2", "INDIRECT(A1)")], two)
    raises(lambda: run(p), "Rate_Base")
    # a fixed prefix bounds the names: "Rate_"&B1 cannot produce WACC
    p = one("r7.xlsx", [c("B1", s="Base"), c("A2", 'INDIRECT("Rate_"&B1)')], two + nm)
    raises(lambda: run(p), "Rate_Base", "Rate_High")
    p = one("r7b.xlsx", [c("B1", s="Base"), c("A2", 'INDIRECT("Rate_"&B1)+Rate_Base+Rate_High')], two + nm)
    expect(run(p), "fail", ["Name Manager: WACC"])
    # ... unless a sheet name starts with the prefix (the computed part may end the qualifier)
    p = book("r7c.xlsx", [("S", sheet([c("B1", s="Base"), c("A2", 'INDIRECT("Rate_"&B1)+Rate_Base+Rate_High')])),
                          ("Rate_Inputs", sheet())], names=two + nm)
    raises(lambda: run(p), "WACC")
    # EVALUATE literal text uses its names; computed EVALUATE text may use any
    expect(run(one("r8.xlsx", [c("A1", "1")], [("Calc", '_xlfn.EVALUATE("Rate*2")', {"hidden": "1"}),
                                               ("Rate", "S!$B$1", None)])), "pass", [])
    raises(lambda: run(one("r8b.xlsx", [c("A1", s="Rate"), c("A2", "1")],
                           [("Calc", "EVALUATE(S!$A$1&\"*2\")", {"hidden": "1"}), ("Rate", "S!$B$1", None)])),
           "Rate", "EVALUATE")


def test_review_hidden_switch():
    """Review finding 29-hidden-switch-breaks-transitivity: with hidden names not counted as
    consumers, use still passes through a hidden helper that is itself used."""
    import detchecks.checks.c29 as C
    old = C.HIDDEN_AND_BUILTIN_NAMES_USE
    try:
        C.HIDDEN_AND_BUILTIN_NAMES_USE = False
        expect(run(one("hs1.xlsx", [c("A1", "Helper")], [("Helper", "Base*2", {"hidden": "1"}),
                                                         ("Base", "S!$B$1", None)])), "pass", [])
        expect(run(one("hs2.xlsx", [c("A1", "A")], [("A", "H+1", None), ("H", "B*2", {"hidden": "1"}),
                                                    ("B", "S!$B$1", None)])), "pass", [])
        # a chain of two hidden helpers, the outer one used from a data validation
        expect(run(one("hs3.xlsx", [], [("Hlp_a", "Hlp_b+1", {"hidden": "1"}), ("Hlp_b", "V*2", {"hidden": "1"}),
                                        ("V", "S!$B$1", None)], tail=dv("A1", "Hlp_a"))), "pass", [])
        # the report's loophole case: a visible name used only by an UNUSED hidden helper fails
        v = run(one("hs4.xlsx", [c("A1", "1")], [("V", "S!$B$1", None), ("Helper", "V*2", {"hidden": "1"})]))
        expect(v, "fail", ["Name Manager: V"])
        assert "only names that are themselves unused refer to it (Helper)" in v["mistakes"][0]["description"]
        # built-ins are not roots either: PrintRng used only by the print area is unused
        expect(run(one("hs5.xlsx", [c("A1", "1")], [("PrintRng", "S!$A$1:$C$9", None),
                                                    ("_xlnm.Print_Area", "PrintRng", {"localSheetId": "0"})])),
               "fail", ["Name Manager: PrintRng"])
    finally:
        C.HIDDEN_AND_BUILTIN_NAMES_USE = old
    # default policy: hidden names are consumers, so the same V passes (question for Patrick)
    expect(run(one("hs6.xlsx", [c("A1", "1")], [("V", "S!$B$1", None), ("Helper", "V*2", {"hidden": "1"})])),
           "pass", [])


def test_review_unknown_scope():
    """Review finding 29-bad-localsheetid-on-uncounted-name: a hidden / built-in name whose
    localSheetId names no sheet raises only when it can change the verdict."""
    used = [("Rate", "S!$B$1", None)]
    expect(run(one("u1.xlsx", [c("A1", "Rate")],
                   used + [("solver_opt", "S!$B$1", {"localSheetId": "7", "hidden": "1"})])), "pass", [])
    expect(run(one("u2.xlsx", [c("A1", "Rate")],
                   used + [("_xlnm.Print_Area", "S!$A$1:$B$2", {"localSheetId": "7"})])), "pass", [])
    expect(run(one("u3.xlsx", [c("A1", "1")], [("solver_opt", "S!$B$1", {"localSheetId": "7", "hidden": "1"})])),
           "pass", [])
    # spelled like a visible name: it could shadow it on its unknown sheet
    raises(lambda: run(one("u4.xlsx", [c("A1", "Rate")], used + [("Rate", "S!$B$9", {"localSheetId": "7",
                                                                                   "hidden": "1"})])),
           "localSheetId 7", "shadow")
    # a consumer whose every possible reading resolves to the one global name: that name is used
    expect(run(one("u5.xlsx", [c("A1", "1")], [("DecisionVar", "S!$B$1", None),
                                               ("solver_adj", "DecisionVar", {"localSheetId": "7", "hidden": "1"})])),
           "pass", [])
    # ... with a sheet-local name of the same spelling the reading depends on the sheet: raise
    p = book("u6.xlsx", [("S", sheet([c("A1", "1")])), ("T", sheet([c("A1", "DecVar")]))],
             names=[("DecVar", "S!$B$1", None), ("DecVar", "T!$B$1", {"localSheetId": "1"}),
                    ("solver_adj", "DecVar", {"localSheetId": "7", "hidden": "1"})])
    raises(lambda: run(p), "DecVar", "scope or sheet is unknown")
    # a visible name with a bad localSheetId still raises (cannot be resolved)
    raises(lambda: run(one("u7.xlsx", [c("A1", "1")], [("X", "S!$A$1", {"localSheetId": "7"})])), "localSheetId")


def test_review_prefilter():
    """Review findings 29-perf-regex-alternation / 29-perf-prefilter: the pre-filter looks up
    whole identifier runs, so a name inside a longer identifier or a string costs no parse,
    while every real use (qualified, unicode, dotted, backslash names) is still found."""
    nm = [("Rate", "S!$B$1", None), ("Rate", "T!$B$1", {"localSheetId": "1"}), ("my.rate", "S!$B$2", None),
          (r"\lead", "S!$B$3", None), ("Zinssatz_\u00e4", "S!$B$4", None), ("Unused", "S!$B$5", None)]
    cells = [c("A1", "RateHigh*2"), c("A2", "T_Rate+1"), c("A3", "SUM(RateX)"),          # no parse
             c("A4", "S!Rate+T!Rate"), c("A5", "MY.RATE+\\LEAD"), c("A6", "ZINSSATZ_\u00c4*2")]
    v = run(book("pf1.xlsx", [("S", sheet(cells)), ("T", sheet())], names=nm))
    expect(v, "fail", ["Name Manager: Unused"])
    assert v["stats"]["sites_parsed"] == {"cell": 3}, v["stats"]
    # 400 unused names: one pass, one parse per text that spells a name
    many = [(f"IQ_N{k}", "1", None) for k in range(400)]
    cells = [c(f"A{r}", f"B{r}*2+IQ_N{r}x") for r in range(1, 200)]
    v = run(one("pf2.xlsx", cells, many))
    assert v["decision"] == "fail" and v["stats"]["n_unused"] == 400, v["stats"]
    assert v["stats"]["sites_parsed"] == {}, v["stats"]


def test_rereview_gate_only_undecided():
    """Second review, 29-rr-gate-ignores-broken: the no-guess gates apply only to names whose
    verdict depends on them (unused AND not broken).  A broken name fails whatever VBA or a
    run-time INDIRECT might do, so it never raises."""
    vba = (f'<Relationship Id="rIdV" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" '
           f'Target="vbaProject.bin"/>')
    vparts = {"xl/vbaProject.bin": b"\x00"}
    bad = [("Bad", "'S'!#REF!", None)]
    # broken + unused + VBA project: fail (was a false raise); the text does not claim it is unused
    v = run(one("rb1.xlsm", [c("A1", "1")], bad, parts=vparts, wb_rels=vba))
    expect(v, "fail", ["Name Manager: Bad"])
    d = v["mistakes"][0]["description"]
    assert "broken" in d and "is not decided" in d and "also unused" not in d, d
    # broken + unused + INDIRECT(A1) that could produce its spelling: fail (was a false raise)
    v = run(one("rb2.xlsx", [c("A1", s="Bad"), c("A2", "SUM(INDIRECT(A1))")], bad))
    expect(v, "fail", ["Name Manager: Bad"])
    assert "is not decided" in v["mistakes"][0]["description"]
    # broken + used + VBA (control): fail, text says broken only
    v = run(one("rb3.xlsm", [c("A1", "Bad+1")], bad, parts=vparts, wb_rels=vba))
    expect(v, "fail", ["Name Manager: Bad"])
    assert "unused" not in v["mistakes"][0]["description"]
    # broken + unused, nothing unreadable (control): the plain "also unused" wording
    v = run(one("rb4.xlsx", [c("A1", "1")], bad))
    expect(v, "fail", ["Name Manager: Bad"])
    assert "also unused" in v["mistakes"][0]["description"]
    # an unbroken unused name beside the broken one still raises, and the gate names only it
    e = raises(lambda: run(one("rb5.xlsm", [c("A1", "1")], bad + [("Rate", "S!$B$1", None)], parts=vparts,
                               wb_rels=vba)), "VBA", "Rate")
    assert "Bad" not in " ".join(str(x) for x in e.failures.values())
    # a broken macro name: fail, not the macro gate
    v = run(one("rb6.xlsx", [c("A1", "1")], [("Macro1", "S!#REF!", {"function": "1", "xlm": "1"})]))
    expect(v, "fail", ["Name Manager: Macro1"])
    # ... while an unbroken one still raises
    raises(lambda: run(one("rb7.xlsx", [c("A1", "1")], [("Macro1", "S!$A$1", {"function": "1", "xlm": "1"})])),
           "macro name")


def test_rereview_hidden_malformed_definition():
    """Second review, 29-rr-hidden-malformed-def-raises-early: an uncounted (hidden / built-in)
    name whose text cannot be parsed raises only when a name it spells would otherwise be
    called unused."""
    rate = ("Rate", "S!$B$1", None)
    hid = ("Helper", "Rate+(", {"hidden": "1"})
    # Rate is used by a cell: the hidden helper cannot change the verdict (was a raise in start())
    expect(run(one("hm1.xlsx", [c("A1", "Rate*2")], [rate, hid])), "pass", [])
    # spells no relevant name: never parsed
    expect(run(one("hm2.xlsx", [c("A1", "Rate*2")], [rate, ("Helper", "Zzz+(", {"hidden": "1"})])), "pass", [])
    # Rate otherwise unused: the helper may be its only user -> raise, naming the parse problem
    skipped(run(one("hm3.xlsx", [c("A1", "1")], [rate, hid])), "fail", ["Name Manager: Rate"])   # helper skipped
    # a visible name the helper does not spell is graded normally beside it
    expect(run(one("hm4.xlsx", [c("A1", "Rate")], [rate, hid, ("Other", "S!$B$2", None)])), "fail",
           ["Name Manager: Other"])
    # a built-in with malformed text spelling an unused name raises too
    skipped(run(one("hm5.xlsx", [c("A1", "1")], [rate, ("_xlnm.Print_Area", "Rate:(", {"localSheetId": "0"})])),
            "fail", ["Name Manager: Rate"])
    # malformed hidden text mentioning INDIRECT: skipped, it produces no name (Patrick 2026-10-05; it raised before)
    skipped(run(one("hm6.xlsx", [c("A1", "1")], [("Other", "S!$B$2", None),
                                                 ("Helper", "INDIRECT(A1", {"hidden": "1"})])),
            "fail", ["Name Manager: Other"])
    expect(run(one("hm6b.xlsx", [c("A1", "Other")], [("Other", "S!$B$2", None),
                                                     ("Helper", "INDIRECT(A1", {"hidden": "1"})])), "pass", [])
    # a counted name's malformed definition still raises at once (broken or not cannot be told)
    skipped(run(one("hm7.xlsx", [c("A1", "Rate")], [("Rate", "S!$B$1+(", None)])), "pass", [])      # not graded
    # hidden names not consumers: an unused malformed helper is irrelevant, a used one is not
    import detchecks.checks.c29 as C
    old = C.HIDDEN_AND_BUILTIN_NAMES_USE
    try:
        C.HIDDEN_AND_BUILTIN_NAMES_USE = False
        expect(run(one("hm8.xlsx", [c("A1", "1")], [rate, hid])), "fail", ["Name Manager: Rate"])
        skipped(run(one("hm9.xlsx", [c("A1", "Helper")], [rate, hid])), "fail", ["Name Manager: Rate"])
    finally:
        C.HIDDEN_AND_BUILTIN_NAMES_USE = old


def test_rereview_prefixed_calls():
    """Second review, 29-rr-prefilter-misses-xludf-call: a call stored with Excel's _xludf. /
    _xll. prefix is a reference to the name of that spelling (core rule, docs/formula.md); the
    identifier-run pre-filter must let such texts through."""
    lam = "_xlfn.LAMBDA(_xlpm.x,_xlpm.x*2)"
    v = run(one("px1.xlsx", [c("A1", "_xludf.Rate(1)")], [("Rate", lam, None)]))
    expect(v, "pass", [])
    assert v["stats"]["sites_parsed"] == {"cell": 1}, v["stats"]
    expect(run(one("px2.xlsx", [c("A1", "_xludf.MyFn(1)")], [("MyFn", lam, None)])), "pass", [])
    expect(run(one("px3.xlsx", [c("A1", "_xll.MyFn(1)+_xlfn._xludf.Other(2)")],
                   [("MyFn", lam, None), ("Other", lam, None)])), "pass", [])
    # controls: a plain call of a built-in spelling is the built-in; a prefixed call of a name
    # that does not exist uses nothing
    expect(run(one("px4.xlsx", [c("A1", "Rate(1)")], [("Rate", lam, None)])), "fail", ["Name Manager: Rate"])
    expect(run(one("px5.xlsx", [c("A1", "_xludf.MyFn(1)")], [("Other", "S!$B$1", None)])), "fail",
           ["Name Manager: Other"])
    # a dotted name keeps its dot
    expect(run(one("px6.xlsx", [c("A1", "_xludf.my.rate(1)")], [("my.rate", lam, None)])), "pass", [])
    # the prefixed call inside a name definition and in a data validation
    expect(run(one("px8.xlsx", [c("A1", "Outer")], [("Outer", "_xludf.Inner(1)", None), ("Inner", lam, None)])),
           "pass", [])
    expect(run(one("px9.xlsx", [], [("MyFn", lam, None)], tail=dv("A1", "_xludf.MyFn(1)"))), "pass", [])


TESTS = [test_counting_hidden_builtin, test_broken, test_lambda_and_function_names, test_prefix_and_tokens,
         test_scope, test_consumers_in_sheet, test_consumers_side_parts, test_names_using_names, test_gates,
         test_early_stop_and_stats, test_review_dynamic_text_refs, test_review_hidden_switch,
         test_review_unknown_scope, test_review_prefilter, test_rereview_gate_only_undecided,
         test_rereview_hidden_malformed_definition, test_rereview_prefixed_calls]


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
