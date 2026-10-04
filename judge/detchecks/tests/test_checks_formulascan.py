"""Unit tests for checks 80 (volatile functions), 87 (whole-column references) and 95
(external links): synthetic micro-workbooks for every trap in the toys / handoff.

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_formulascan
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
SCRATCH = os.path.join(os.path.dirname(HERE), "scratch", "formulascan")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"
K80, K87, K95 = REGISTRY[80].key, REGISTRY[87].key, REGISTRY[95].key

_TMP = None


def tmp(name):
    global _TMP
    if _TMP is None:
        os.makedirs(SCRATCH, exist_ok=True)
        _TMP = tempfile.mkdtemp(prefix="test_fscan_", dir=SCRATCH)
    return os.path.join(_TMP, name)


# ============================================================================ micro builder
def c(ref, f=None, v=None, s=None, shared=None, array=None, attrs=""):
    """One <c>.  f: formula text (None = constant); shared=(si, ref or None) - a child when f
    is None; array: ref of an array formula.  s: shared-string-free inline text value."""
    col = "".join(ch for ch in ref if ch.isalpha())
    row = int("".join(ch for ch in ref if ch.isdigit()))
    inner = ""
    if shared is not None:
        si, sref = shared
        r = f' ref="{sref}"' if sref else ""
        inner = f'<f t="shared" si="{si}"{r}>{escape(f) if f else ""}</f>' if f else f'<f t="shared" si="{si}"/>'
    elif array is not None:
        inner = f'<f t="array" ref="{array}">{escape(f)}</f>'
    elif f is not None:
        inner = f"<f{attrs}>{escape(f)}</f>"
    if s is not None:
        return row, col_to_index(col), f'<c r="{ref}" t="inlineStr">{inner}<is><t>{escape(s)}</t></is></c>'
    val = f"<v>{v}</v>" if v is not None else "<v>0</v>"
    return row, col_to_index(col), f'<c r="{ref}">{inner}{val}</c>'


def sheet(cells=(), tail=""):
    rows = {}
    for r, k, x in cells:
        rows.setdefault(r, []).append((k, x))
    body = "".join(f'<row r="{r}">' + "".join(x for _, x in sorted(rows[r])) + "</row>" for r in sorted(rows))
    return f"<sheetData>{body}</sheetData>{tail}"


def cf(sqref, *formulas, typ="expression", extra=""):
    rules = "".join(f'<cfRule type="{typ}" priority="{i + 1}"{extra}>' + (f"<formula>{escape(f)}</formula>" if f else "")
                    + "</cfRule>" for i, f in enumerate(formulas or [None]))
    return f'<conditionalFormatting sqref="{sqref}">{rules}</conditionalFormatting>'


def dv(sqref, f1):
    return (f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="{sqref}">'
            f"<formula1>{escape(f1)}</formula1></dataValidation></dataValidations>")


def x14dv(sqref, f1):
    return (f'<extLst><ext uri="{{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}}" xmlns:x14="{X14}">'
            f'<x14:dataValidations count="1" xmlns:xm="{XM}"><x14:dataValidation type="list">'
            f"<x14:formula1><xm:f>{escape(f1)}</xm:f></x14:formula1><xm:sqref>{sqref}</xm:sqref>"
            f"</x14:dataValidation></x14:dataValidations></ext></extLst>")


def x14cf(sqref, f):
    return (f'<extLst><ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}" xmlns:x14="{X14}">'
            f'<x14:conditionalFormattings><x14:conditionalFormatting xmlns:xm="{XM}">'
            f'<x14:cfRule type="expression" priority="1" id="{{00000000-0000-0000-0000-000000000001}}">'
            f"<xm:f>{escape(f)}</xm:f></x14:cfRule><xm:sqref>{sqref}</xm:sqref>"
            f"</x14:conditionalFormatting></x14:conditionalFormattings></ext></extLst>")


def sparkline(host, f):
    return (f'<extLst><ext uri="{{05C60535-1F16-4fd2-B633-F4F36F0B64E0}}" xmlns:x14="{X14}">'
            f'<x14:sparklineGroups xmlns:xm="{XM}"><x14:sparklineGroup><x14:sparklines><x14:sparkline>'
            f"<xm:f>{escape(f)}</xm:f><xm:sqref>{host}</xm:sqref></x14:sparkline></x14:sparklines>"
            f"</x14:sparklineGroup></x14:sparklineGroups></ext></extLst>")


def book(name, sheets, names=(), links=(), parts=None, wb_rels="", sheet_rels=None):
    """sheets: [(name, xml, state)]; names: [(name, text, attrs)]; links: [(target, [sheet names])]
    (externalLink parts declared in <externalReferences>); parts: extra zip members;
    sheet_rels: {sheet index: rels body}."""
    path = tmp(name)
    parts = dict(parts or {})
    sheet_rels = sheet_rels or {}
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("[Content_Types].xml",
               '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               "</Types>")
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
    ext = ""
    if links:
        refs = []
        for k, (target, snames) in enumerate(links, 1):
            wr.append(f'<Relationship Id="rIdL{k}" Type="{RT}/externalLink" Target="externalLinks/externalLink{k}.xml"/>')
            refs.append(f'<externalReference r:id="rIdL{k}"/>')
            sn = "".join(f'<sheetName val="{escape(s)}"/>' for s in snames)
            z.writestr(f"xl/externalLinks/externalLink{k}.xml",
                       f'<externalLink xmlns="{MAIN}" xmlns:r="{REL}"><externalBook r:id="rId1">'
                       f"<sheetNames>{sn}</sheetNames></externalBook></externalLink>")
            z.writestr(f"xl/externalLinks/_rels/externalLink{k}.xml.rels",
                       f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RT}/externalLinkPath" '
                       f'Target="{escape(target)}" TargetMode="External"/></Relationships>')
        ext = "<externalReferences>" + "".join(refs) + "</externalReferences>"
    dn = ""
    if names:
        dn = "<definedNames>" + "".join(
            f'<definedName name="{n}"' + "".join(f' {k}="{v}"' for k, v in (a or {}).items()) + f">{escape(t)}</definedName>"
            for n, t, a in names) + "</definedNames>"
    z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<workbook xmlns="{MAIN}" '
                                  f'xmlns:r="{REL}"><sheets>{"".join(sh)}</sheets>{ext}{dn}</workbook>')
    z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG}">{"".join(wr)}{wb_rels}</Relationships>')
    for k, v in parts.items():
        z.writestr(k, v)
    z.close()
    return path


def run(path, n, **kw):
    return grade(path, checks=[n], **kw)[REGISTRY[n].key]


def locs(v):
    return [m["location"] for m in v["mistakes"]]


def expect(v, decision, locations=None):
    assert v["decision"] == decision, (decision, v["summary"], v["mistakes"])
    if locations is not None:
        assert locs(v) == locations, (locations, locs(v), [m["description"] for m in v["mistakes"]])


def raises(fn, *needles):
    try:
        fn()
    except GradingError as e:
        msg = str(e) + " " + " ".join(str(x) for x in e.failures.values())
        for nd in needles:
            assert nd in msg, (nd, msg[:600])
        return e
    raise AssertionError(f"expected GradingError containing {needles}")


# ============================================================================ 80
def test_80_cells():
    p = book("v1.xlsx", [("Model", sheet([
        c("A1", s="Note: avoid OFFSET(, INDIRECT( and TODAY() in this model"),   # text cell
        c("B1", '"TODAY()"'),                                                  # string literal
        c("C1", 'N("uses OFFSET(A1,1,1)")+A1'),
        c("D1", "NOW$2+1"),                                                     # column NOW, not a call
        c("E1", "_xlfn.LET(_xlpm.today,1,_xlpm.today+1)"),                      # LET parameter called today
        c("F1", "_xludf.TODAY()"),                                              # a user function, not the built-in
        c("G1", "_xlfn.RANDARRAY(3)"),                                          # outside the rubric list
        c("A3", "SUM(D5:OFFSET(D5,0,3))"),                                      # range-ending call
        c("B3", "MID(CELL(\"filename\",A1),FIND(\"]\",CELL(\"filename\",A1))+1,255)"),
        c("C3", "_xlfn.BYROW(A1:A3,_xleta.RAND)"),
        c("D3", "offset(a1,1,0)"),                                              # lower case (openpyxl)
        c("E3", "SUM(A1,\r\n\tNOW())"),
    ]))])
    v = run(p, 80)
    expect(v, "fail")
    got = {m["location"]: m["description"] for m in v["mistakes"]}   # grouped per function set
    assert set(got) == {"Model!A3", "Model!B3", "Model!C3", "Model!D3", "Model!E3"}, got
    assert "OFFSET" in got["Model!A3"] and "CELL" in got["Model!B3"] and "RAND" in got["Model!C3"]
    assert "NOW" in got["Model!E3"]
    assert v["stats"]["randarray_sites_not_counted"] == 1
    clean = book("v1b.xlsx", [("Model", sheet([c("A1", s="OFFSET("), c("B1", '"TODAY()"'), c("D1", "NOW$2+1"),
                                               c("G1", "_xlfn.RANDARRAY(3)")]))])
    expect(run(clean, 80), "pass", [])


def test_80_shared_hidden_array():
    p = book("v2.xlsx", [("Calc", sheet([
        c("B2", "OFFSET(A2,1,0)+1", shared=("0", "B2:B50")),
        *[c(f"B{r}", None, shared=("0", None)) for r in range(3, 51)],
        c("D1", "OFFSET(A1:A3,1,0)*2", array="D1:D3"),
    ]), "veryHidden")])
    v = run(p, 80)
    expect(v, "fail", ["Calc!D1", "Calc!B2:B50"])
    assert "49 formulas" in v["mistakes"][1]["description"]
    # a shared child whose master never appeared raises
    bad = book("v2b.xlsx", [("Calc", sheet([c("B3", None, shared=("7", None))]))])
    raises(lambda: run(bad, 80), "no master")


def test_80_names():
    p = book("v3.xlsx", [("Data", sheet([c("A1", "SUM(RevRng)"), c("A2", "Total*2"), c("A3", "MyFn(3)"),
                                         c("A4", "SUM(A1:A3)"), c("A5", 'SUM(INDIRECT("ByText"))')],
                                        tail=dv("B9", "ListRng")))],
             names=[("RevRng", "OFFSET(Data!$B$1,0,0,5,1)", None),
                    ("Base", "OFFSET(Data!$C$1,0,0,3,1)", None),
                    ("Total", "SUM(Base)", None),
                    ("MyFn", "_xlfn.LAMBDA(_xlpm.r,INDIRECT(\"Data!B\"&_xlpm.r))", None),
                    ("ByText", "OFFSET(Data!$D$1,0,0,2,1)", None),        # used via INDIRECT("ByText")
                    ("ListRng", "OFFSET(Data!$E$1,0,0,COUNTA(Data!$E:$E),1)", None),   # used by a DV list
                    ("Unused", "INDIRECT(\"Data!Z1\")", None),
                    ("Hid", "TODAY()", {"hidden": "1"}),
                    ("_xlnm.Print_Area", "OFFSET(Data!$A$1,0,0,COUNTA(Data!$A:$A),5)", {"localSheetId": "0"}),
                    ("Fine", "Data!$B$1:$B$5", None)])
    v = run(p, 80)
    got = {m["location"]: m["description"] for m in v["mistakes"]}
    assert set(got) == {"Name Manager: RevRng", "Name Manager: Base", "Name Manager: MyFn",
                        "Name Manager: ByText", "Name Manager: ListRng", "Data!A5"}, got
    assert "Data!A1" in got["Name Manager: RevRng"]
    assert "Data!A2" in got["Name Manager: Base"]                 # through Total
    assert "Data!A3" in got["Name Manager: MyFn"]
    assert "data validation" in got["Name Manager: ListRng"]
    assert v["stats"]["n_flagged_cells"] == 1                    # A5 (INDIRECT); cells using names are not mistakes
    # unused names (also a dynamic Print_Area) are stats only (COUNT_UNUSED_NAMES = False)
    assert [x.split("=")[0] for x in v["stats"]["volatile_names_unused_not_counted"]] == \
        ["Unused", "Hid", "_xlnm.Print_Area (built-in)"], v["stats"]
    only_unused = book("v3b.xlsx", [("Data", sheet([c("A1", "1+1")]))],
                       names=[("Unused", "INDIRECT(\"Data!Z1\")", None)])
    expect(run(only_unused, 80), "pass", [])
    # a dynamic chart range: the OFFSET name is used by a chart series
    chart = book("v3c.xlsx", [("Data", sheet([c("A1", v=1)], tail='<drawing r:id="rIdD"/>'))],
                 names=[("DynRng", "OFFSET(Data!$A$1,0,0,COUNTA(Data!$A:$A),1)", None)],
                 parts=_drawing_parts("$B$1", "Data!DynRng"),
                 sheet_rels={0: f'<Relationship Id="rIdD" Type="{RT}/drawing" Target="../drawings/drawing1.xml"/>'})
    v = run(chart, 80)
    expect(v, "fail", ["Name Manager: DynRng"])
    assert "chart" in v["mistakes"][0]["description"]


def test_80_cf_dv():
    p = book("v4.xlsx", [("S", sheet([c("A1", v=1)], tail=cf("A1:A9", "A1<TODAY()")
                                     + dv("B1", "INDIRECT(\"list\")")))])
    v = run(p, 80)
    expect(v, "fail", ["S!A1:A9", "S!B1"])
    p2 = book("v4b.xlsx", [("S", sheet([c("A1", v=1)], tail=x14dv("C2 C5", "OFFSET($A$1,0,0,3,1)")))])
    expect(run(p2, 80), "fail", ["S!C2,C5"])
    p3 = book("v4c.xlsx", [("S", sheet([c("A1", v=1)], tail=cf("A1:A9", typ="timePeriod",
                                                                  extra=' timePeriod="today"')))])
    expect(run(p3, 80), "fail", ["S!A1:A9"])
    p4 = book("v4d.xlsx", [("S", sheet([c("A1", v=1)], tail=x14cf("A1:A3", "A1>NOW()")))])
    expect(run(p4, 80), "fail", ["S!A1:A3"])


def test_80_stamp_alone():
    # the rubric's date stamp: =TODAY() alone, referenced by nothing
    ok = book("s1.xlsx", [("Cover", sheet([c("B6", s="Date"), c("C6", "TODAY()"), c("C7", " NOW( ) ")])),
                          ("Calc", sheet([c("A1", "Cover!B6&\"x\""), c("A2", "SUM(Cover!C8:C9)"),
                                          c("A3", "SUM(D:D)")]))],
              names=[("_xlnm.Print_Area", "Cover!$A$1:$H$40", {"localSheetId": "0"})])
    v = run(ok, 80)
    expect(v, "pass", [])
    assert v["stats"]["date_stamps_exempt"] == ["Cover!C6", "Cover!C7"]
    # not alone
    for i, t in enumerate(['"Prepared "&TEXT(TODAY(),"d mmm yyyy")', "TODAY()-D6", "INT(NOW())", "TODAY()+0"]):
        p = book(f"s2_{i}.xlsx", [("Cover", sheet([c("C6", t)]))])
        expect(run(p, 80), "fail", ["Cover!C6"])


def _stamp_ref(name, cells2=(), names=(), tail1="", tail2="", parts=None, wb_rels="", sheet_rels=None):
    p = book(name, [("Cover", sheet([c("C6", "TODAY()")], tail=tail1)),
                    ("Calc", sheet(list(cells2), tail=tail2))],
             names=names, parts=parts, wb_rels=wb_rels, sheet_rels=sheet_rels)
    return run(p, 80)


def test_80_stamp_referenced():
    cases = {
        "cell": dict(cells2=[c("A1", "YEARFRAC(Cover!C6,DATE(2030,1,1))")]),
        "range": dict(cells2=[c("A1", "MAX(Cover!$A$1:$D$10)")]),
        "whole column": dict(cells2=[c("A1", "COUNT(Cover!C:C)")]),
        "whole row": dict(cells2=[c("A1", "COUNT(Cover!6:6)")]),
        "3-D": dict(cells2=[c("A1", "SUM(Cover:Calc!C6)")]),
        "name": dict(names=[("AsOf", "Cover!$C$6", None)]),
        "unused name": dict(names=[("Block", "Cover!$A$1:$H$40", None)]),
        "CF elsewhere": dict(tail2=cf("A1:A5", "A1<Cover!$C$6")),
        "DV": dict(tail2=dv("B1", "Cover!$C$5:$C$7")),
        "same-sheet CF relative": dict(tail1=cf("D1:D10", "C1>5")),
        "sparkline": dict(tail2=sparkline("B9", "Cover!C1:C9")),
        "shared child": dict(cells2=[c("D1", "Cover!C1*2", shared=("0", "D1:D10")),
                                     *[c(f"D{r}", None, shared=("0", None)) for r in range(2, 11)]]),
        "table": dict(cells2=[c("A1", "SUM(Stamp[Day])")]),
        "range operator": dict(cells2=[c("A1", "SUM(Cover!A1:INDEX(Cover!$E$1:$E$10,8))")]),
    }
    table_parts = {
        "xl/tables/table1.xml": f'<table xmlns="{MAIN}" id="1" name="Stamp" displayName="Stamp" ref="C5:C6">'
                                f'<tableColumns count="1"><tableColumn id="1" name="Day"/></tableColumns></table>'}
    for label, kw in cases.items():
        if label == "table":
            kw = dict(kw, parts=table_parts,
                      sheet_rels={0: f'<Relationship Id="rIdT" Type="{RT}/table" Target="../tables/table1.xml"/>'})
        v = _stamp_ref(f"s3_{label.replace(' ', '_')}.xlsx", **kw)
        assert v["decision"] == "fail" and locs(v) == ["Cover!C6"], (label, v["summary"], v["mistakes"])
        assert "referenced by" in v["mistakes"][0]["description"], (label, v["mistakes"])
    # negative controls: a shared group that never reaches C6, a CF on Cover that reads other cells
    v = _stamp_ref("s3_neg.xlsx", cells2=[c("D1", "Cover!C7*2", shared=("0", "D1:D3")),
                                          c("D2", None, shared=("0", None)), c("D3", None, shared=("0", None))],
                   tail1=cf("D1:D3", "$E1>5"))
    expect(v, "pass", [])
    # a chart series and a shape text link
    drawing = (f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
               f'<xdr:sp textlink="{{TL}}"/></xdr:wsDr>')
    for label, tl, chart_f in (("chart", "$A$1", "Cover!$C$6"), ("textlink", "$C$6", "Cover!$A$1")):
        parts = {"xl/drawings/drawing1.xml": drawing.replace("{TL}", tl),
                 "xl/drawings/_rels/drawing1.xml.rels":
                     f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RT}/chart" '
                     f'Target="../charts/chart1.xml"/></Relationships>',
                 "xl/charts/chart1.xml": f'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
                                         f"<c:f>{chart_f}</c:f></c:chartSpace>"}
        p = book(f"s4_{label}.xlsx", [("Cover", sheet([c("C6", "TODAY()")], tail='<drawing r:id="rIdD"/>'))],
                 parts=parts, sheet_rels={0: f'<Relationship Id="rIdD" Type="{RT}/drawing" Target="../drawings/drawing1.xml"/>'})
        v = run(p, 80)
        expect(v, "fail", ["Cover!C6"])


def test_80_stamp_order_and_second_pass():
    # the referencing formula sits on a sheet BEFORE the stamp, and earlier on the same sheet
    p = book("s5.xlsx", [("Calc", sheet([c("A1", "Last!B9+1")])),
                         ("Last", sheet([c("A1", "B9"), c("B9", "NOW()")]))])
    v = run(p, 80)
    expect(v, "fail", ["Last!B9"])
    assert "Calc!A1" in v["mistakes"][0]["description"] and "Last!A1" in v["mistakes"][0]["description"]


# ============================================================================ 87
def test_87_forms():
    p = book("w1.xlsx", [("Data", sheet([c("A1", v=1)])), ("My Sheet", sheet([c("A1", v=1)])),
                         ("Calc", sheet([
                             c("A1", "SUM($1:$1)"), c("A2", "SUM(Data!$A:$C)"), c("A3", "SUM('My Sheet'!A:A)"),
                             c("A4", "SUM(Data:Calc!A:A)"), c("A5", "SUM([1]Data!A:A)"), c("A6", "sum(a:a)"),
                             c("A7", "SUM(\r\n$A:$A)"), c("A8", "ROW(1:10)"), c("A9", "SUM($A$8:INDEX($A:$A,20))"),
                             c("A10", "SUM(A1:A1048576)"),
                             # passes
                             c("B1", "SUM(A2:A1048576)"), c("B2", 'TEXT(A1/24,"hh:mm")'), c("B3", '"Mix "&"1:2"'),
                             c("B4", s="Segment columns B:E are inputs; ratio 1:1"), c("B5", "SUM(A:.A)"),
                             c("B6", "SUM(_xlfn._TRO_TRAILING(A:A))"), c("B7", "SUM(_xlfn.TRIMRANGE((A:A)))"),
                             c("B8", "SUM(Table1[Val])"), c("B9", "SUM(StartN:EndN)"), c("B10", "SUM(A1#)"),
                             c("B11", 'COUNTIF(INDIRECT("A:A"),10)'),
                         ]))], links=[("Other.xlsx", ["Data"])],
             names=[("StartN", "Calc!$A$1", None), ("EndN", "Calc!$A$5", None)])
    v = run(p, 87)
    assert locs(v) == ["Calc!A1", "Calc!A2:A7", "Calc!A8", "Calc!A9:A10"], v["mistakes"]   # grouped per kind
    assert v["stats"]["sheet_edge_ranges_not_counted"] == 1 and v["stats"]["trimmed_ranges_seen"] == 3


def test_87_shared_names_rules():
    p = book("w2.xlsx", [("S", sheet([c("C2", "SUM(A:A)-A2", shared=("0", "C2:C50")),
                                      *[c(f"C{r}", None, shared=("0", None)) for r in range(3, 51)],
                                      c("D1", "MEDIAN(Peer_EV)")],
                                     tail=cf("E1:E9", "COUNTIF($A:$A,E1)>1") + dv("F1", "$A:$A")))],
             names=[("Peer_EV", "S!$H:$H", None), ("Hid", "S!$1:$1", {"hidden": "1"}),
                    ("Chain", "SUM(Peer2)", None), ("Peer2", "S!$J:$J", {"hidden": "1"}),
                    ("_xlnm.Print_Titles", "S!$1:$3", {"localSheetId": "0"}),
                    ("_xlnm.Print_Area", "S!$A:$H", {"localSheetId": "0"})])
    v = run(p, 87)
    assert locs(v) == ["Name Manager: Peer_EV", "S!C2:C50", "S!E1:E9", "S!F1"], locs(v)
    d = {m["location"]: m["description"] for m in v["mistakes"]}
    assert "S!D1" in d["Name Manager: Peer_EV"]
    assert "49 formulas" in d["S!C2:C50"]
    assert [x.split("=")[0] for x in v["stats"]["whole_ref_names_unused_not_counted"]] == \
        ["Hid", "Peer2", "_xlnm.Print_Titles (built-in)", "_xlnm.Print_Area (built-in)"], v["stats"]
    # the hidden name becomes a mistake once a cell uses it through another name
    p3 = book("w2b.xlsx", [("S", sheet([c("A1", "Chain*2")]))],
              names=[("Chain", "SUM(Peer2)", None), ("Peer2", "S!$J:$J", {"hidden": "1"})])
    v = run(p3, 87)
    assert locs(v) == ["Name Manager: Peer2"] and "S!A1" in v["mistakes"][0]["description"], v["mistakes"]
    p2 = book("w3.xlsx", [("S", sheet([c("A1", "SUM(A2:A9)")], tail=x14cf("A1:A3", "COUNTIF($B:$B,A1)>1")))])
    expect(run(p2, 87), "fail", ["S!A1:A3"])


# ============================================================================ 95
def test_95_places():
    # openpyxl-style literal reference, no externalLink part
    p = book("x1.xlsx", [("S", sheet([c("A1", "'[Other.xlsx]Sheet1'!A1"), c("A2", "'C:\\dir\\[Book.xlsx]S'!B2"),
                                      c("A3", "Other.xlsx!Rate*2")]))])
    expect(run(p, 95), "fail", ["S!A1", "S!A2", "S!A3"])          # grouped per target workbook
    # a sheet really named Other.xlsx makes Other.xlsx!Rate local
    p = book("x1b.xlsx", [("Other.xlsx", sheet()), ("S", sheet([c("A3", "Other.xlsx!Rate*2")]))],
             names=[("Rate", "'Other.xlsx'!$A$1", {"localSheetId": "0"})])
    expect(run(p, 95), "pass", [])
    # declared link used nowhere + name + DV + x14 DV + CF + sparkline
    p = book("x2.xlsx", [("S", sheet([c("A1", v=1)], tail=cf("A1:A4", "A1>[1]Lists!$A$1") + dv("B1", "[1]Lists!$A$2:$A$4")
                                     + x14dv("D5 C56", "[1]!ScenarioList"))),
                         ("T", sheet([c("A1", v=1)], tail=sparkline("B9", "[1]Lists!A1:A9")))],
             links=[("file:///C:/Models/Scenario_Lists.xlsx", ["Lists"])],
             names=[("Prior", "'[1]Annual Summary'!$G$7", None)])
    v = run(p, 95)
    assert locs(v) == ["External link [1]", "Name Manager: Prior", "S!A1:A4", "S!B1", "S!D5,C56",
                       "T!B9"], locs(v)
    assert "Scenario_Lists.xlsx" in v["mistakes"][0]["description"]
    p = book("x3.xlsx", [("S", sheet([c("A1", v=1)]))], links=[("Book2.xlsx", [])])
    expect(run(p, 95), "fail", ["External link [1]"])
    assert "no formula uses it" in run(p, 95)["mistakes"][0]["description"]
    # shared children of an external master are one rectangle
    p = book("x4.xlsx", [("S", sheet([c("B2", "[1]Comps!$I$5*A2", shared=("0", "B2:B9")),
                                      *[c(f"B{r}", None, shared=("0", None)) for r in range(3, 10)]]))],
             links=[("Q3_comps.xlsx", ["Comps"])])
    v = run(p, 95)
    expect(v, "fail", ["External link [1]", "S!B2:B9"])
    assert "used by 8 formula sites" in v["mistakes"][0]["description"]


def _drawing_parts(textlink, chart_f):
    return {"xl/drawings/drawing1.xml":
            f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
            f'<xdr:sp textlink="{escape(textlink)}"/></xdr:wsDr>',
            "xl/drawings/_rels/drawing1.xml.rels":
            f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RT}/chart" Target="../charts/chart1.xml"/>'
            f"</Relationships>",
            "xl/charts/chart1.xml": f'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
                                    f"<c:f>{escape(chart_f)}</c:f></c:chartSpace>",
            "xl/ctrlProps/ctrlProp1.xml": f'<formControlPr xmlns="{X14}" objectType="Drop" fmlaLink="$C$1" '
                                          f'fmlaRange="$B$1:$B$3"/>'}


def test_95_87_drawings():
    srels = (f'<Relationship Id="rIdD" Type="{RT}/drawing" Target="../drawings/drawing1.xml"/>'
             f'<Relationship Id="rIdC" Type="{RT}/ctrlProp" Target="../ctrlProps/ctrlProp1.xml"/>')
    p = book("d1.xlsx", [("S", sheet([c("A1", v=1)], tail='<drawing r:id="rIdD"/>'))],
             parts=_drawing_parts("[1]S!$A$1", "'[1]S'!$A$1:$A$5"), sheet_rels={0: srels},
             links=[("Other.xlsx", ["S"])])
    v = run(p, 95)
    assert locs(v) == ["External link [1]", "S", "S"], locs(v)
    assert "chart" in v["mistakes"][1]["description"] and "shape" in v["mistakes"][2]["description"]
    assert "used by 2 formula sites" in v["mistakes"][0]["description"]
    parts = _drawing_parts("$A$1", "S!$A$1")
    parts["xl/ctrlProps/ctrlProp1.xml"] = parts["xl/ctrlProps/ctrlProp1.xml"].replace('fmlaLink="$C$1"',
                                                                                      'fmlaLink="[Book.xlsx]S!$C$1"')
    p = book("d2.xlsx", [("S", sheet([c("A1", v=1)], tail='<drawing r:id="rIdD"/>'))], parts=parts,
             sheet_rels={0: srels})
    v = run(p, 95)
    expect(v, "fail", ["S"])
    assert "form control" in v["mistakes"][0]["description"]
    # 87: a chart series over a whole column is a chart source, not a formula
    p = book("d3.xlsx", [("S", sheet([c("A1", v=1)], tail='<drawing r:id="rIdD"/>'))],
             parts=_drawing_parts("$A$1", "S!$A:$A"), sheet_rels={0: srels})
    expect(run(p, 87), "pass", [])


def test_95_not_links():
    hl = '<hyperlinks><hyperlink ref="F7" r:id="rIdH"/><hyperlink ref="F8" r:id="rIdF"/></hyperlinks>'
    p = book("y1.xlsx", [("S", sheet([c("F6", s="Source: Q3_comps.xlsx, [Book.xlsx]Sheet1!A1"),
                                      c("F7", s="nasdaq"), c("F8", s="file"),
                                      c("A1", '"[Book.xlsx]Sheet1!A1"&"x"'), c("A2", "[0]!Rate"),
                                      c("A3", 'HYPERLINK("C:\\x\\Other.xlsx","open")'),
                                      c("A4", 'SWITCH(B1,"[Act/360]Nominal",1,2)')], tail=hl))],
             names=[("Rate", "S!$B$1", None)],
             sheet_rels={0: f'<Relationship Id="rIdH" Type="{RT}/hyperlink" Target="https://www.nasdaq.com/x" '
                            f'TargetMode="External"/><Relationship Id="rIdF" Type="{RT}/hyperlink" '
                            f'Target="Other.xlsx" TargetMode="External"/>'},
             parts={"xl/externalLinks/externalLink9.xml": f'<externalLink xmlns="{MAIN}"/>'})
    v = run(p, 95)
    expect(v, "pass", [])
    assert v["stats"]["hyperlinks_to_files_not_counted"] == 1
    assert v["stats"]["orphan_external_link_parts"] == ["xl/externalLinks/externalLink9.xml"]


def test_95_meta_connections():
    p = book("z1.xlsx", [("S", sheet([c("A1", "[1]S!A1")]))], links=[("Other.xlsx", ["S"])])
    v = run(p, 95, task_meta={"requires_external_links": True})
    expect(v, "pass", [])
    assert v["stats"]["n_links_found_but_required"] == 2
    expect(run(p, 95, task_meta={"requires_external_links": False}), "fail")
    raises(lambda: run(p, 95, task_meta={"requires_external_links": "yes"}), "must be true/false")

    def conn(body):
        return dict(parts={"xl/connections.xml": f'<connections xmlns="{MAIN}">{body}</connections>'},
                    wb_rels=f'<Relationship Id="rIdC" Type="{RT}/connections" Target="connections.xml"/>')
    odbc = '<connection id="1" name="Sales" type="1"><dbPr connection="DSN=Sales" command="SELECT 1"/></connection>'
    p = book("z2.xlsx", [("S", sheet())], **conn(odbc))
    expect(run(p, 95), "fail", ["Data connection: Sales"])
    p = book("z3.xlsx", [("S", sheet())], **conn(odbc.replace('type="1"', 'type="1" deleted="1"')
                                                 + '<connection id="2" name="ThisWorkbookDataModel" type="5">'
                                                   '<dbPr connection="Data Model Connection" command="Model"/></connection>'))
    v = run(p, 95)
    expect(v, "pass", [])
    assert v["stats"]["deleted_connections"] == 1 and v["stats"]["internal_connections"] == 1
    pq = ('<connection id="1" name="Query - Q1" type="5"><dbPr connection="Provider=Microsoft.Mashup.OleDb.1;'
          'Data Source=$Workbook$;Location=Q1" command="SELECT * FROM [Q1]"/></connection>')
    p = book("z4.xlsx", [("S", sheet())], **conn(pq))
    raises(lambda: run(p, 95), "Power Query")
    p = book("z5.xlsx", [("S", sheet([c("A1", "[1]S!A1")]))], links=[("Other.xlsx", ["S"])], **conn(pq))
    v = run(p, 95)
    expect(v, "fail")
    assert v["stats"]["power_query"] == ["Query - Q1"]


def test_cross_check_isolation():
    """Each check ignores the others' traps."""
    p = book("q1.xlsx", [("S", sheet([c("A1", "SUM(A:A)"), c("A2", "OFFSET(A1,1,1)"), c("A3", "[1]S!A1")]))],
             links=[("Other.xlsx", ["S"])])
    assert locs(run(p, 87)) == ["S!A1"]
    assert locs(run(p, 80)) == ["S!A2"]
    assert locs(run(p, 95)) == ["External link [1]", "S!A3"]


# ============================================================================ review fixes (2026-10-03)
def _databar(sqref, lo, hi, typ="formula"):
    return (f'<conditionalFormatting sqref="{sqref}"><cfRule type="dataBar" priority="1"><dataBar>'
            f'<cfvo type="{typ}" val="{escape(lo, {chr(34): "&quot;"})}"/>'
            f'<cfvo type="{typ}" val="{escape(hi, {chr(34): "&quot;"})}"/>'
            f'<color rgb="FF638EC6"/></dataBar></cfRule></conditionalFormatting>')


def _colorscale(sqref, mid):
    return (f'<conditionalFormatting sqref="{sqref}"><cfRule type="colorScale" priority="1"><colorScale>'
            f'<cfvo type="min"/><cfvo type="formula" val="{escape(mid, {chr(34): "&quot;"})}"/><cfvo type="max"/>'
            f'<color rgb="FFF8696B"/><color rgb="FFFFEB84"/><color rgb="FF63BE7B"/></colorScale></cfRule>'
            f'</conditionalFormatting>')


def _x14databar(sqref, f):
    return (f'<extLst><ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}" xmlns:x14="{X14}">'
            f'<x14:conditionalFormattings><x14:conditionalFormatting xmlns:xm="{XM}">'
            f'<x14:cfRule type="dataBar" id="{{00000000-0000-0000-0000-000000000002}}"><x14:dataBar>'
            f'<x14:cfvo type="num"><xm:f>{escape(f)}</xm:f></x14:cfvo><x14:cfvo type="autoMax"/></x14:dataBar>'
            f"</x14:cfRule><xm:sqref>{sqref}</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings>"
            f"</ext></extLst>")


def test_cfvo_thresholds():
    """Colour-scale / data-bar / icon-set thresholds are CF formulas (review finding cfvo-80/87/95)."""
    for i, tail in enumerate([_databar("A1:A9", "0", "MAX(OFFSET($A$1,0,0,9,1))"),
                              _x14databar("A1:A9", "TODAY()"), _colorscale("A1:A9", 'INDIRECT("B1")')]):
        p = book(f"cv80_{i}.xlsx", [("S", sheet([c("A1", v=1)], tail=tail))])
        v = run(p, 80)
        expect(v, "fail", ["S!A1:A9"])
    ok = book("cv80_ok.xlsx", [("S", sheet([c("A1", v=1)], tail=_databar("A1:A9", "10", "90", typ="percentile")
                                           + _colorscale("B1:B9", "$C$1")))])
    expect(run(ok, 80), "pass", [])
    expect(run(ok, 87), "pass", [])
    expect(run(ok, 95), "pass", [])
    # a stamp read only by a data-bar threshold is referenced
    st = book("cv80_stamp.xlsx", [("Cover", sheet([c("A1", v=1), c("C6", "TODAY()")],
                                                  tail=_databar("A1:A9", "$C$6-30", "$C$6")))])
    v = run(st, 80)
    expect(v, "fail", ["Cover!C6"])
    assert "conditional format" in v["mistakes"][0]["description"], v["mistakes"]
    for i, tail in enumerate([_databar("A1:A9", "MIN($A:$A)", "MAX($A:$A)"), _x14databar("A1:A9", "MIN($1:$1)")]):
        p = book(f"cv87_{i}.xlsx", [("S", sheet([c("A1", v=1)], tail=tail))])
        expect(run(p, 87), "fail", ["S!A1:A9"])
    p = book("cv95.xlsx", [("S", sheet([c("A1", v=1)], tail=_databar("A1:A9", "0", "'[Other.xlsx]S'!$A$1")))])
    v = run(p, 95)
    expect(v, "fail", ["S!A1:A9"])
    assert "Other.xlsx" in v["mistakes"][0]["description"]


def _ctrl(fmla_range, fmla_link="$C$1"):
    return {"xl/ctrlProps/ctrlProp1.xml": f'<formControlPr xmlns="{X14}" objectType="Drop" '
                                          f'fmlaLink="{escape(fmla_link)}" fmlaRange="{escape(fmla_range)}"/>'}


_CTRL_REL = f'<Relationship Id="rIdC" Type="{RT}/ctrlProp" Target="../ctrlProps/ctrlProp1.xml"/>'


def test_form_control_uses():
    """A name used only by a form control's input range is used (review finding ctrl-use-80/87)."""
    p = book("fc80.xlsx", [("Lists", sheet([c("A1", s="a"), c("A2", s="b")])), ("S", sheet([c("B1", v=1)]))],
             names=[("DynList", "OFFSET(Lists!$A$1,0,0,COUNTA(Lists!$A:$A),1)", None)],
             parts=_ctrl("DynList"), sheet_rels={1: _CTRL_REL})
    v = run(p, 80)
    expect(v, "fail", ["Name Manager: DynList"])
    assert "form control on 'S' (fmlaRange)" in v["mistakes"][0]["description"], v["mistakes"]
    p = book("fc87.xlsx", [("Lists", sheet([c("A1", s="a")])), ("S", sheet([c("B1", v=1)]))],
             names=[("AllItems", "Lists!$A:$A", None)], parts=_ctrl("AllItems"), sheet_rels={1: _CTRL_REL})
    expect(run(p, 87), "fail", ["Name Manager: AllItems"])
    # a form control whose input range covers a stamp references it
    p = book("fc80_stamp.xlsx", [("Cover", sheet([c("C6", "TODAY()")])), ("S", sheet([c("B1", v=1)]))],
             parts=_ctrl("Cover!$C$5:$C$7"), sheet_rels={1: _CTRL_REL})
    v = run(p, 80)
    expect(v, "fail", ["Cover!C6"])
    assert "form control" in v["mistakes"][0]["description"]


def test_80_stamp_name_endpoints():
    """A range ending in defined names is placed through the names (review finding stamp-name-endpoint)."""
    names = [("StartN", "Cover!$A$1", None), ("EndN", "Cover!$A$5", None)]
    for i, f in enumerate(["SUM(StartN:EndN)", "SUM(A1:EndN)", "SUM(Cover!StartN:EndN)"]):
        p = book(f"ne_{i}.xlsx", [("Cover", sheet([c("A1", v=1), c("E1", f), c("A5", v=2), c("C6", "TODAY()")]))],
                 names=names)
        v = run(p, 80)
        expect(v, "pass", [])
        assert v["stats"]["date_stamps_exempt"] == ["Cover!C6"], v["stats"]
    # the box between the names covers the stamp although neither name does; used from another sheet
    p = book("ne_hit.xlsx", [("Cover", sheet([c("C6", "TODAY()")])), ("Calc", sheet([c("A1", "MAX(Top:Bottom)")]))],
             names=[("Top", "Cover!$C$1", None), ("Bottom", "Cover!$C$9", None)])
    v = run(p, 80)
    expect(v, "fail", ["Cover!C6"])
    assert "Calc!A1" in v["mistakes"][0]["description"]
    # an undefined endpoint is #NAME? and references nothing
    p = book("ne_undef.xlsx", [("Cover", sheet([c("E1", "SUM(A1:Nowhere)"), c("C6", "TODAY()")]))])
    expect(run(p, 80), "pass", [])
    # a dynamic endpoint cannot be placed: no guess
    p = book("ne_dyn.xlsx", [("Cover", sheet([c("E1", "SUM(A1:EndD)"), c("C6", "TODAY()")]))],
             names=[("EndD", "INDEX(Cover!$A:$A,5)", None)])
    raises(lambda: run(p, 80), "cannot place reference", "not one fixed reference")


def test_80_stamp_builtin_name_used():
    """A built-in name covering the stamp references it once a formula uses the name (finding
    stamp-builtin-name-used); written with or without the _xlnm. prefix."""
    pa = [("_xlnm.Print_Area", "Cover!$A$1:$H$40", {"localSheetId": "0"})]
    for i, f in enumerate(["COUNT(Cover!Print_Area)", "COUNT(Print_Area)", "ROWS(_xlnm.Print_Area)"]):
        p = book(f"bi_{i}.xlsx", [("Cover", sheet([c("A1", f), c("C6", "TODAY()")]))], names=pa)
        v = run(p, 80)
        expect(v, "fail", ["Cover!C6"])
        assert "Cover!A1" in v["mistakes"][0]["description"], (f, v["mistakes"])
    # unused print area: still not a reference
    p = book("bi_unused.xlsx", [("Cover", sheet([c("A1", "1+1"), c("C6", "TODAY()")]))], names=pa)
    expect(run(p, 80), "pass", [])
    # a used built-in name that calls OFFSET is a used volatile name, also when written without _xlnm.
    p = book("bi_vol.xlsx", [("S", sheet([c("A1", "ROWS(Print_Area)")]))],
             names=[("_xlnm.Print_Area", "OFFSET(S!$A$1,0,0,5,5)", {"localSheetId": "0"})])
    v = run(p, 80)
    expect(v, "fail", ["Name Manager: S!_xlnm.Print_Area"])


def test_bad_scope_names():
    """A name whose localSheetId names no sheet raises only when a flagged name's use depends
    on it (review finding badscope-unrelated)."""
    junk = ("Junk", "S!$B$2", {"localSheetId": "7"})
    p = book("bs87.xlsx", [("S", sheet([c("A1", "SUM(A2:A3)")]))],
             names=[("_xlnm.Print_Titles", "S!$1:$1", {"localSheetId": "0"}), junk])
    expect(run(p, 87), "pass", [])
    p = book("bs80.xlsx", [("S", sheet([c("A1", "SUM(A2:A3)")]))],
             names=[("_xlnm.Print_Area", "OFFSET(S!$A$1,0,0,5,5)", {"localSheetId": "0"}), junk])
    expect(run(p, 80), "pass", [])
    # the flagged name itself has a broken scope: is it used?  No guess for 80/87 ...
    p = book("bs80b.xlsx", [("S", sheet([c("A1", "SUM(Dyn)")]))],
             names=[("Dyn", "OFFSET(S!$A$1,0,0,5,1)", {"localSheetId": "7"})])
    raises(lambda: run(p, 80), "cannot tell whether", "localSheetId")
    # ... but 95 counts every name, used or not
    p = book("bs95.xlsx", [("S", sheet([c("A1", "1")]))], names=[("Ext", "[Other.xlsx]S!$A$1", {"localSheetId": "7"})])
    v = run(p, 95)
    expect(v, "fail", ["Name Manager: '?'!Ext"])
    assert "cannot be traced" in v["mistakes"][0]["description"]
    # a broken-scope name on the chain to an otherwise unused flagged name: no guess; once a cell uses
    # the flagged name, the verdict no longer depends on it
    names = [("Peer", "S!$H:$H", None), ("Via", "SUM(Peer)", {"localSheetId": "7"})]
    p = book("bs87c.xlsx", [("S", sheet([c("A1", "1")]))], names=names)
    raises(lambda: run(p, 87), "cannot tell whether", "'peer'")
    p = book("bs87d.xlsx", [("S", sheet([c("A1", "MAX(Peer)")]))], names=names)
    expect(run(p, 87), "fail", ["Name Manager: Peer"])


def test_87_undecidable_name_certain_fail():
    """A flagged name whose use cannot be traced (broken localSheetId on it or on the chain to it)
    does not make 87 raise once another site already fails for certain: the verdict does not
    depend on it, and it goes to stats (second review, 87-raise-despite-certain-fail)."""
    dyn = ("Dyn", "S!$A:$A", {"localSheetId": "7"})
    chain = [("Peer", "S!$H:$H", None), ("Via", "SUM(Peer)", {"localSheetId": "7"})]
    cases = [
        # (file, cells, names, CF/DV tail, expected locations, undecidable name)
        ("uc_cell.xlsx", [c("A1", "SUM(B:B)")], [dyn], "", ["S!A1"], "Dyn"),
        ("uc_chain.xlsx", [c("A1", "SUM(B:B)")], chain, "", ["S!A1"], "Peer"),
        ("uc_name.xlsx", [c("A1", "SUM(Used)")], [("Used", "S!$C:$C", None), dyn], "",
         ["Name Manager: Used"], "Dyn"),
        ("uc_cf.xlsx", [c("A1", "1")], [dyn], cf("A1:A5", "COUNTIF($A:$A,A1)>1"), ["S!A1:A5"], "Dyn"),
        ("uc_dv.xlsx", [c("A1", "1")], chain, dv("D5", "Lists!$A:$A"), ["S!D5"], "Peer"),
    ]
    for fn, cells, names, tail, want, und in cases:
        sheets = [("S", sheet(cells, tail=tail))] + ([("Lists", sheet())] if "Lists" in tail else [])
        v = run(book(fn, sheets, names=names), 87)
        expect(v, "fail", want)
        st = v["stats"]["whole_ref_names_use_undecidable_not_counted"]
        assert len(st) == 1 and st[0].startswith(und + "=") and "localSheetId" in st[0], (fn, st)
        assert "Formulas/Avoid" not in st[0], st
        assert v["stats"]["n_names_flagged"] == (1 if fn == "uc_name.xlsx" else 0), (fn, v["stats"])
    # nothing else fails: still no guess (both the name itself and the chain)
    p = book("uc_alone.xlsx", [("S", sheet([c("A1", "SUM(A2:A3)")]))], names=[dyn])
    raises(lambda: run(p, 87), "cannot tell whether", "'Dyn'", "localSheetId")
    p = book("uc_alone_chain.xlsx", [("S", sheet([c("A1", "SUM(A2:A3)")]))], names=chain)
    raises(lambda: run(p, 87), "cannot tell whether", "'Peer'", "'peer'")
    # a bounded edge range or a trimmed range is not a certain fail: still raises
    p = book("uc_edge.xlsx", [("S", sheet([c("A1", "SUM(A2:A1048576)+SUM(B:.B)")]))], names=[dyn])
    raises(lambda: run(p, 87), "cannot tell whether", "'Dyn'")


def test_87_name_leading_equals():
    """A name stored with a leading '=' (some writers) is classified and described once with '='
    (second review, 87-name-leading-equals-description)."""
    p = book("leq.xlsx", [("S", sheet([c("A1", "SUM(Rng)")]))], names=[("Rng", "=S!$A:$A", None)])
    v = run(p, 87)
    expect(v, "fail", ["Name Manager: Rng"])
    d = v["mistakes"][0]["description"]
    assert "(=S!$A:$A)" in d and "==" not in d, d
    p = book("leq_unused.xlsx", [("S", sheet([c("A1", "1")]))], names=[("Rng", "=S!$A:$A", None)])
    v = run(p, 87)
    expect(v, "pass", [])
    assert v["stats"]["whole_ref_names_unused_not_counted"] == ["Rng=S!$A:$A"], v["stats"]


def test_87_shared_copy_reaches_edge():
    """A shared copy that stretches a bounded range to a whole column fails (finding shared-child-whole)."""
    p = book("se.xlsx", [("S", sheet([c("B1", "SUM($A$1:A1048575)", shared=("0", "B1:B2")),
                                      c("B2", None, shared=("0", None))]))])
    v = run(p, 87)
    expect(v, "fail", ["S!B2"])
    assert "A1048576" in v["mistakes"][0]["description"] and v["stats"]["shared_copies_reclassified"] == 1
    # running totals that stay bounded, an edge-anchored range moving away from row 1, and a copy
    # pushed off the grid (#REF!) all pass
    p = book("se_ok.xlsx", [("S", sheet([c("B1", "SUM($A$1:A1)", shared=("0", "B1:B9")),
                                         *[c(f"B{r}", None, shared=("0", None)) for r in range(2, 10)],
                                         c("D1", "SUM(A2:$A$1048576)", shared=("1", "D1:D3")),
                                         c("D2", None, shared=("1", None)), c("D3", None, shared=("1", None)),
                                         c("E1", "SUM($A$1:A1048575)", shared=("2", "E1:E3")),
                                         c("E3", None, shared=("2", None))]))])
    v = run(p, 87)
    expect(v, "pass", [])
    assert v["stats"]["shared_copies_reclassified"] == 0


def test_use_tally_saturation():
    """Usage tallies stop parsing after USE_TALLY_SATURATION uses (finding uses-cached-parse)."""
    cells = [c(f"A{r}", f"INDEX(Rng,{r})+{r}") for r in range(1, 21)]
    p = book("sat.xlsx", [("S", sheet(cells))], names=[("Rng", "S!$Z:$Z", None)])
    v = run(p, 87)
    expect(v, "fail", ["Name Manager: Rng"])
    d = v["mistakes"][0]["description"]
    assert "used by at least 3 formula sites" in d and "S!A1, S!A2, S!A3" in d, d
    p = book("sat2.xlsx", [("S", sheet(cells[:2]))], names=[("Rng", "S!$Z:$Z", None)])
    d = run(p, 87)["mistakes"][0]["description"]
    assert "used by 2 formula sites" in d and "at least" not in d, d


def test_95_missing_file_named_sheet():
    """Other.xlsx!A1 with no such sheet: not counted (question for Patrick), reported in stats."""
    p = book("mf.xlsx", [("S", sheet([c("A1", "Other.xlsx!A1*2"), c("A2", "Foo!A1")]))])
    v = run(p, 95)
    expect(v, "pass", [])
    assert v["stats"]["refs_to_missing_file_named_sheets_not_counted"] == 1, v["stats"]


TESTS = [test_80_cells, test_80_shared_hidden_array, test_80_names, test_80_cf_dv, test_80_stamp_alone,
         test_80_stamp_referenced, test_80_stamp_order_and_second_pass, test_87_forms, test_87_shared_names_rules,
         test_95_places, test_95_87_drawings, test_95_not_links, test_95_meta_connections, test_cross_check_isolation,
         test_cfvo_thresholds, test_form_control_uses, test_80_stamp_name_endpoints, test_80_stamp_builtin_name_used,
         test_bad_scope_names, test_87_undecidable_name_certain_fail, test_87_name_leading_equals,
         test_87_shared_copy_reaches_edge, test_use_tally_saturation,
         test_95_missing_file_named_sheet]


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
