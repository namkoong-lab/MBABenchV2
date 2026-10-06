"""Tests for the shared conditional-format evaluator (checks/_cfeval.py), the cell lookup it reads other
cells through (core/lookup.py) and the conditional-format policy of the checks that use them
(Patrick 2026-10-05: "CONDITIONAL FORMATS never stop a grading"; "assume the built-in Excel
conditional formats always have visible text").

    cd judge && python -m detchecks.tests.test_cfeval

Synthetic micro-workbooks only (the builders of test_checks_fills / test_checks_zeros), no LibreOffice.
Plain asserts; also collectable by pytest.
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback

import detchecks.tests.test_checks_fills as F
import detchecks.tests.test_checks_zeros as Z
from detchecks.api import Engine
from detchecks.checks import _cfeval as E
from detchecks.checks.c65 import C65, NEG
from detchecks.checks.c65 import fires as fires65
from detchecks.checks.c66 import C66
from detchecks.checks.c66 import fires as fires66
from detchecks.checks.c94 import C94, eval_rule
from detchecks.core.lookup import BLANK, CellValues, Unavailable
from detchecks.core.package import Package
from detchecks.core.sheet import CfRule, ExcelError
from detchecks.core.values import detect_provenance

UNK = E.UNKNOWN


class FakeValues:
    """A CfEnv value source: {(sheet, row, col): value}; any other cell is empty (BLANK)."""

    def __init__(self, cells):
        self.cells = {(s.casefold(), r, c): v for (s, r, c), v in (cells or {}).items()}
        self.wanted = []

    def get(self, sheet, r, c):
        return self.cells.get((sheet.casefold(), r, c), BLANK)

    def want(self, sheet, box):
        self.wanted.append((sheet, box))


class _Name:
    def __init__(self, name, text, scope=None):
        self.name, self.text, self.scope, self.builtin = name, text, scope, False


def env(cells=None, sheet="S", names=()):
    return E.CfEnv(sheet, FakeValues(cells), names)


def val(f, cells=None, where=(1, 1, 1, 1), value=E.UNKNOWN_VALUE, names=(), sheet="S"):
    return E.formula_value(f, env(cells, sheet, names), where, value)


def uneval(x, *needles):
    assert E.is_unevaluable(x), x
    for n in needles:
        assert n in x.why, (n, x.why)
    return x


def R(**kw):
    return CfRule(**kw)


# ============================================================================ operators
def test_operators():
    assert val("1+2*3") == 7.0 and val("(1+2)*3") == 9.0 and val("7-2-1") == 4.0 and val("8/4/2") == 1.0
    assert val("-2^2") == 4.0 and val("2^3^2") == 64.0 and val("-(2^2)") == -4.0      # Excel: negation before ^
    assert val("50%") == 0.5 and val("200%*3") == 6.0 and val("+5") == 5.0 and val("--5") == 5.0
    assert val('"a"&"b"&1&TRUE') == "ab1TRUE" and val('1.5&""') == "1.5" and val('0.1+0.2&""') == "0.3"
    # comparisons: numbers < text < logicals, text case-insensitive, numbers to 15 significant digits
    assert val("1<2") is True and val("2<=2") is True and val("3>=4") is False and val("1<>1") is False
    assert val('"abc"="ABC"') is True and val('"a"<"B"') is True and val('1<"0"') is True and val('"z"<TRUE') is True
    assert val("0.1+0.2=0.3") is True and val("1/3*3=1") is True
    # an empty cell is 0 / "" / FALSE against a number / text / logical
    assert val("$B$1=0") is True and val('$B$1=""') is True and val("$B$1=FALSE") is True and val("$B$1<>0") is False
    assert val("$B$1+1") == 1.0 and val('$B$1&"x"') == "x"
    # errors propagate; division by zero; text coerced to numbers only when it is a plain number
    assert val("1/0") == ExcelError("#DIV/0!") and isinstance(val("1/0"), ExcelError)
    assert val("#N/A=1") == "#N/A" and val("1+#REF!") == "#REF!" and val('"x"&#N/A') == "#N/A"
    assert val('"5"+1') == 6.0 and val('" 1,000 "+0') == 1000.0 and val('"5%"*2') == 0.1 and val('"(5)"+0') == -5.0
    assert val('"abc"+1') == "#VALUE!"
    uneval(val('"1/2/2026"+0'), "date")
    assert val("0^0") == "#NUM!" and val("(-8)^0.5") == "#NUM!"


# ============================================================================ functions
def test_functions():
    s = {("S", 1, 4): "OK — all 30 checks pass", ("S", 2, 4): "FAIL — 2 check(s)", ("S", 3, 4): "  a   b  "}
    assert val("LEFT($D$1,2)", s) == "OK" and val("LEFT($D$2,4)", s) == "FAIL" and val('LEFT("abc")') == "a"
    assert val('RIGHT("abc",2)') == "bc" and val('RIGHT("abc",0)') == "" and val('LEFT("abc",-1)') == "#VALUE!"
    assert val('MID("abcdef",2,3)') == "bcd" and val('MID("abc",0,1)') == "#VALUE!" and val('MID("abc",5,2)') == ""
    assert val('LEN("abc")') == 3.0 and val("LEN($Z$9)") == 0.0 and val("LEN(12.5)") == 4.0
    assert val('UPPER("aBc")') == "ABC" and val('LOWER("AbC")') == "abc" and val("TRIM($D$3)", s) == "a b"
    assert val('VALUE("1,234.5")') == 1234.5 and val("VALUE(7)") == 7.0 and val('VALUE("x")') == "#VALUE!"
    assert val('TEXT(1234.5,"#,##0.00")') == "1,234.50" and val('TEXT(0.25,"0%")') == "25%"
    assert val("ISNUMBER(1)") is True and val('ISNUMBER("1")') is False and val('ISTEXT("a")') is True
    assert val("ISTEXT(1)") is False and val("ISBLANK($Z$9)") is True and val('ISBLANK("")') is False
    assert val("ISERROR(1/0)") is True and val("ISERROR(1)") is False and val("ISERR(#N/A)") is False
    assert val("ISNA(#N/A)") is True and val("ISLOGICAL(TRUE)") is True and val("ISNONTEXT(1)") is True
    assert val("AND(TRUE,1)") is True and val("AND(TRUE,0)") is False and val("OR(FALSE,0)") is False
    assert val("OR(FALSE,2)") is True and val("AND($Z$9,TRUE)") is True          # an empty cell is skipped
    assert val('AND("x",TRUE)') == "#VALUE!" and val("AND(1/0,TRUE)") == "#DIV/0!" and val("AND($Z$9)") == "#VALUE!"
    assert val("NOT(0)") is True and val("NOT(1)") is False and val('NOT("x")') == "#VALUE!"
    assert val('IF(1>2,"a","b")') == "b" and val("IF(TRUE,5)") == 5.0 and val("IF(FALSE,5)") is False
    assert val('IF(TRUE,"ok",COUNTIF(A1:A2,1))') == "ok"                      # the branch not taken is not evaluated
    assert val("IFERROR(1/0,7)") == 7.0 and val("IFERROR(3,7)") == 3.0
    assert val("ABS(-3)") == 3.0 and val('ABS("x")') == "#VALUE!"
    assert val("ROUND(2.5,0)") == 3.0 and val("ROUND(-2.5,0)") == -3.0 and val("ROUND(1.005,2)") == 1.01
    assert val("ROUND(1234,-2)") == 1200.0 and val("ROUNDUP(1.21,1)") == 1.3 and val("ROUNDDOWN(-1.29,1)") == -1.2
    assert val("INT(-1.5)") == -2.0 and val("TRUNC(-1.59,1)") == -1.5
    assert val("MOD(-3,2)") == 1.0 and val("MOD(5,0)") == "#DIV/0!" and val("ISEVEN(4)") is True and val("ISODD(4)") is False
    assert val('EXACT("a","A")') is False and val('EXACT("a","a")') is True
    assert val('SEARCH("P*S","xxPASS")') == 3.0 and val('SEARCH("z","abc")') == "#VALUE!"
    assert val('ISNUMBER(SEARCH("pass","TESTS PASSED"))') is True and val('FIND("A","bab")') == "#VALUE!"
    assert val('FIND("a","bab")') == 2.0 and val('CONCATENATE("a",1,"b")') == "a1b"
    assert val("ROW()", where=(7, 3, 1, 1)) == 7.0 and val("COLUMN()", where=(7, 3, 1, 1)) == 3.0
    assert val("ROW(B5)", where=(7, 3, 5, 2)) == 7.0                          # relative: shifted to the cell


# ============================================================================ references
def test_references_absolute_relative_and_cross_sheet():
    cells = {("S", 4, 2): 10.0, ("S", 5, 2): -1.0, ("Other", 1, 1): 3.0, ("My Sheet", 2, 2): "x",
             ("Other", 4, 1): "row4", ("Other", 5, 1): "row5"}
    # relative references are shifted from the rule's anchor (first range's top-left) to the cell
    assert val("B4>0", cells, where=(4, 3, 4, 3)) is True          # C4 reads B4
    assert val("B4>0", cells, where=(5, 3, 4, 3)) is False         # C5 reads B5
    assert val("$B4>0", cells, where=(5, 9, 4, 3)) is False        # column absolute, row relative
    assert val("B$4>0", cells, where=(5, 3, 4, 3)) is True         # row absolute
    assert val("$B$4>0", cells, where=(99, 9, 4, 3)) is True
    # another sheet, quoted names, relative references into another sheet
    assert val("Other!$A$1*2", cells) == 6.0 and val("'My Sheet'!$B$2", cells) == "x"
    assert val("Other!A4", cells, where=(4, 2, 4, 2)) == "row4" and val("Other!A4", cells, where=(5, 2, 4, 2)) == "row5"
    # the cell itself: its own value (also when the caller screens a hypothetical value)
    assert val('LEFT($D$4,4)="FAIL"', where=(4, 4, 4, 4), value="FAIL - 2") is True
    assert val("D4=0", where=(6, 4, 4, 4), value=0.0) is True       # D4 shifted to D6, the cell
    assert val("D4=0", where=(6, 4, 4, 4)) is UNK                   # its value unknown: UNKNOWN
    assert val("B4>0", cells, where=None) is UNK                    # position unknown and relative: UNKNOWN
    assert val("$B$4>0", cells, where=None) is True                 # absolute: no position needed
    assert val("ROW()=1", where=None) is UNK
    # relative references wrap round the grid as in Excel (row 1 - 1 = row 1048576)
    assert val("B1", {("S", 1048576, 2): 9.0}, where=(1, 3, 2, 3)) == 9.0
    # defined names: a constant or one absolute cell; workbook or sheet scope
    names = [_Name("Tol", "0.001"), _Name("Cap", "Other!$A$1"), _Name("Cap", "5", scope="S"), _Name("Rel", "Other!A1"),
             _Name("Span", "Other!$A$1:$A$9")]
    assert val("Tol*1000", cells, names=names) == 1.0 and val("Cap", cells, names=names) == 5.0       # sheet scope wins
    assert val("Cap", cells, names=names, sheet="T") == 3.0 and val("Missing") == "#NAME?"
    uneval(val("Rel", cells, names=names), "relative")
    uneval(val("Span", cells, names=names), "not a constant or a single cell")


def test_unevaluable_never_guessed():
    uneval(val("COUNTIF($A$1:$A$9,1)>0"), "COUNTIF")
    uneval(val("SUM(A1:A3)>0"), "SUM")
    uneval(val("A1:A3"), "range")
    uneval(val("$A:$A=1"), "whole column")
    uneval(val("YEAR($A$1)=2026"), "YEAR")
    uneval(val("[1]Sheet1!$A$1=1"), "another workbook")
    uneval(val("Sheet1:Sheet3!$A$1=1"), "across sheets")
    uneval(val("{1,2}=1"))
    uneval(E.formula_value("$A$9=1", None, (1, 1, 1, 1), 5.0), "no value source")
    bad = E.CfEnv("S", type("V", (), {"get": lambda self, s, r, c: Unavailable("S!A9 is a formula whose value is "
                                                                            "untrusted"), "want": lambda *a: None})())
    uneval(E.formula_value("$A$9=1", bad, (1, 1, 1, 1), 5.0), "untrusted")
    uneval(E.expression_fires('"text"', env()), "returns text")
    assert E.expression_fires("1/0", env()) is False and E.expression_fires("$Z$9", env()) is False   # error / empty
    assert E.expression_fires("2", env()) is True and E.expression_fires("0", env()) is False
    try:
        bool(E.Unevaluable("x"))
    except TypeError:
        pass
    else:
        raise AssertionError("an Unevaluable must not be used as a truth value")


# ============================================================================ rule types
def test_rule_types():
    e = env({("S", 1, 2): 10.0, ("S", 2, 2): "PASS"})
    w = (1, 1, 1, 1)
    # cellIs: every operator, literal and formula operands, errors
    for op, f, v, want in (("equal", "5", 5.0, True), ("notEqual", "5", 5.0, False), ("greaterThan", "4", 5.0, True),
                           ("lessThan", "4", 5.0, False), ("greaterThanOrEqual", "5", 5.0, True),
                           ("lessThanOrEqual", "4", 5.0, False), ("equal", '"pass"', "PASS", True)):
        assert eval_rule(R(type="cellIs", operator=op, formulas=[f]), v, w, e) is want, (op, f, v)
    assert eval_rule(R(type="cellIs", operator="between", formulas=["10", "1"]), 5.0, w, e) is True       # min / max
    assert eval_rule(R(type="cellIs", operator="notBetween", formulas=["1", "10"]), 50.0, w, e) is True
    assert eval_rule(R(type="cellIs", operator="greaterThan", formulas=["$B$1"]), 11.0, w, e) is True    # formula operand
    assert eval_rule(R(type="cellIs", operator="greaterThan", formulas=["$B$1/2"]), 4.0, w, e) is False
    assert eval_rule(R(type="cellIs", operator="equal", formulas=["0"]), ExcelError("#N/A"), w, e) is False
    assert eval_rule(R(type="cellIs", operator="equal", formulas=["1/0"]), 5.0, w, e) is False
    uneval(eval_rule(R(type="cellIs", operator="equal", formulas=["COUNT(A:A)"]), 5.0, w, e), "COUNT")
    # text rules (containsText with SEARCH wildcards, Excel 2026-10-03), blanks, errors
    assert eval_rule(R(type="containsText", text="P*S"), "PASS", w, e) is True
    assert eval_rule(R(type="notContainsText", text="F?IL"), "OK", w, e) is True
    assert eval_rule(R(type="beginsWith", text="P*"), "PASS", w, e) is False                 # LEFT(): literal
    assert eval_rule(R(type="endsWith", text="ss"), "PASS", w, e) is True
    assert eval_rule(R(type="containsBlanks"), "x", w, e) is False and eval_rule(R(type="notContainsBlanks"), 1.0) is True
    assert eval_rule(R(type="containsErrors"), ExcelError("#N/A")) is True
    assert eval_rule(R(type="notContainsErrors"), ExcelError("#N/A")) is False
    # expressions, other rule types
    assert eval_rule(R(type="expression", formulas=['$B$2="PASS"']), 1.0, w, e) is True
    uneval(eval_rule(R(type="top10", rank=10), 1.0, w, e), "top10")
    uneval(eval_rule(R(type="aboveAverage"), 1.0, w, e))
    # the same rules through Zeros as dashes (66) and Negatives in parentheses (65)
    assert fires66(R(type="cellIs", operator="lessThan", formulas=["$B$1"]), 0.0, w, e) is True
    assert fires66(R(type="expression", formulas=["ABS(A1)<0.001"]), 0.0, w, e) is True
    uneval(fires66(R(type="duplicateValues"), 0.0, w, e))
    assert fires65(R(type="cellIs", operator="lessThan", formulas=["$B$1"]), NEG, w, e) is True      # every negative < 10
    assert fires65(R(type="cellIs", operator="lessThan", formulas=["-$B$1"]), NEG, w, e) is UNK      # some < -10
    assert fires65(R(type="expression", formulas=["$B$1>5"]), NEG, w, e) is True        # does not read the cell
    assert fires65(R(type="expression", formulas=["A1<0"]), NEG, w, e) is True          # the cell: every negative
    assert fires65(R(type="expression", formulas=["A1<-5"]), NEG, w, e) is UNK


# ============================================================================ the lookup
def test_lookup_reads_other_cells_through_the_value_source():
    st = F.Styles()
    other = F.sheet([F.c("A1", 0, 3.0), F.c("A2", 0, "txt"), F.c("A3", 0, 6.0, f="A1*2"),
                     '<c r="B1"><f t="array" ref="B1:B3">ROW(A1:A3)</f><v>1</v></c>'])
    p = F.book(F.tmp("lookup.xlsx"), [("S", F.sheet([F.c("A1", 0, 1.0)])), ("Other", other)], st)
    pkg = Package.open(p)
    try:
        detect_provenance(pkg)
        cv = CellValues(pkg)
        cv.want("Other", (1, 1, 4, 2))
        assert cv.get("Other", 1, 1) == 3.0 and cv.get("other", 2, 1) == "txt" and cv.get("Other", 3, 1) == 6.0
        assert cv.get("Other", 4, 1) is BLANK                                      # an empty position in the box
        assert isinstance(cv.get("Other", 2, 2), Unavailable) and "array" in cv.get("Other", 2, 2).why
        assert isinstance(cv.get("Nope", 1, 1), Unavailable)
        assert cv._sheets["Other"].n_streams == 1                                  # one stream for the registered box
        assert cv.get("Other", 50, 50) is BLANK and cv._sheets["Other"].n_streams == 2   # an unregistered cell: read once
        assert cv.get("Other", 50, 50) is BLANK and cv._sheets["Other"].n_streams == 2   # ... and kept
    finally:
        pkg.close()
    # openpyxl-labelled file: its formula caches are untrusted, so a rule reading one cannot be evaluated
    p2 = Z.book([("S", Z.sheet([Z.c("A1", 0, 5.0), Z.c("B1", 0, 1.0, f="0+1")]))], Z.Styles(), app=Z.APP_OPX,
                name="lookup_opx.xlsx")
    pkg = Package.open(p2)
    try:
        detect_provenance(pkg)
        cv = CellValues(pkg)
        assert cv.get("S", 1, 1) == 5.0                      # a constant is always readable
        assert isinstance(cv.get("S", 1, 2), Unavailable) and "untrusted" in cv.get("S", 1, 2).why
    finally:
        pkg.close()


def test_prefetch_registers_every_referenced_cell():
    e = env(names=[_Name("Cap", "Other!$C$3")])
    tail = [type("CF", (), {"ranges": [(4, 3, 6, 3), (10, 3, 10, 3)], "rules": [
        R(type="expression", formulas=["AND($B4>Other!A1,Cap>0)"]), R(type="cellIs", operator="equal", formulas=["5"])]})()]
    E.prefetch(e, tail)
    got = sorted(e.values.wanted)
    assert ("S", (4, 2, 6, 2)) in got and ("S", (10, 2, 10, 2)) in got                  # $B4 over both ranges
    assert ("Other", (1, 1, 3, 1)) in got and ("Other", (7, 1, 7, 1)) in got            # A1 shifted over each range
    assert ("Other", (3, 3, 3, 3)) in got                                               # the name's cell
    assert len(got) == 5, got                                                           # literal cellIs: nothing


# ============================================================================ No white-on-white hiding (94)
def _cf(sqref, *rules):
    return f'<conditionalFormatting sqref="{sqref}">' + "".join(rules) + "</conditionalFormatting>"


def _expr(dxf, prio, formula, stop=False):
    from xml.sax.saxutils import escape
    return (f'<cfRule type="expression" dxfId="{dxf}" priority="{prio}"{" stopIfTrue=\"1\"" if stop else ""}>'
            f"<formula>{escape(formula)}</formula></cfRule>")


def _run94(cells, st, name, tail, sheets=None):
    shs = [("S", F.sheet(cells, tail=tail))] + list(sheets or [])
    return Engine(F.book(F.tmp(name), shs, st), [C94()]).run()[C94.key]


def test_94_attempt_2924_cover_check_line():
    """Attempt 2924 (corpus/attempts/2924), Cover!D4: green bold text 'OK — all 30 checks pass' (a formula
    over the Checks sheet); the rule LEFT($D$4,4)="FAIL" would turn it white bold with a red fill stored in
    fgColor only (Excel paints bgColor only: no fill, Excel 2026-10-03).  The rule is OFF, so the cell
    shows its green font and is visible: decided - before 2026-10-05 it raised 'cannot decide'."""
    st = F.Styles()
    green = st.xf(font=st.font("008000"))
    st.dxfs.append('<dxf><font><b val="1"/><color rgb="FFFFFFFF"/></font>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FFFF0000"/></patternFill></fill></dxf>')
    d = len(st.dxfs) - 1
    tail = _cf("D4", _expr(d, 1, 'LEFT($D$4,4)="FAIL"'))
    ok = F.c("D4", green, "OK — all 30 checks pass", f='IF(Checks!$D$4=0,"OK — all "&30&" checks pass","FAIL")')
    v = _run94([ok], st, "c2924_ok.xlsx", tail)
    assert v["decision"] == "pass" and v["stats"]["undecided_cells"] == 0, v["stats"]
    assert v["stats"]["cf_second_pass_sheets"] == ["S"] and v["stats"]["cf_cells_evaluated"] == 1
    assert v["stats"]["cf_assumptions"]["cells"] == 0, v["stats"]["cf_assumptions"]           # evaluated, not assumed
    # the same cell saying FAIL: the rule fires, white text on no fill -> concealed
    bad = F.c("D4", green, "FAIL — 2 check(s) failing: see Checks tab", f='IF(1,"FAIL — 2 check(s) failing")')
    v = _run94([bad], st, "c2924_fail.xlsx", tail)
    assert v["decision"] == "fail" and F.locs(v) == ["S!D4"], (F.locs(v), v["stats"])


def test_94_rules_reading_other_cells_and_sheets():
    st = F.Styles()
    d_white = st.dxf(font="FFFFFF")
    # A1:A3 turn white where the neighbour in B says "x" (relative reference, each row its own)
    tail = _cf("A1:A3", _expr(d_white, 1, '$B1="x"'))
    cells = [F.c("A1", 0, 1.0), F.c("A2", 0, 2.0), F.c("A3", 0, 3.0), F.c("B2", 0, "x")]
    v = _run94(cells, st, "rel.xlsx", tail)
    assert v["decision"] == "fail" and F.locs(v) == ["S!A2"], F.locs(v)
    # a switch on another sheet ('Model Inputs'!$C$2): on -> every cell white, off -> nothing
    tail = _cf("A1:A2", _expr(d_white, 1, "'Model Inputs'!$C$2=1"))
    for on, want in ((1.0, ["S!A1:A2"]), (0.0, [])):
        inp = ("Model Inputs", F.sheet([F.c("C2", 0, on)]))
        v = _run94([F.c("A1", 0, 5.0), F.c("A2", 0, "t")], st, f"xs{int(on)}.xlsx", tail, [inp])
        assert F.locs(v) == want and v["stats"]["cf_assumptions"]["cells"] == 0, (on, F.locs(v), v["stats"])
    # a stopIfTrue blocker reading another cell is evaluated too (2026-10-04 test s04 raised)
    tail = _cf("A1", '<cfRule type="expression" priority="1" stopIfTrue="1"><formula>$B$1&gt;0</formula></cfRule>',
               _expr(d_white, 2, "TRUE"))
    assert _run94([F.c("A1", 0, 5.0), F.c("B1", 0, 1.0)], st, "stop1.xlsx", tail)["decision"] == "pass"
    assert _run94([F.c("A1", 0, 5.0), F.c("B1", 0, -1.0)], st, "stop2.xlsx", tail)["decision"] == "fail"


def test_94_unevaluable_rule_assumed_visible():
    st = F.Styles()
    w = st.xf(font=st.font("FFFFFF"))
    d_white, d_black = st.dxf(font="FFFFFF"), st.dxf(font="000000")
    # a rule that cannot be evaluated would turn black text white: assumed visible (was GradingError)
    tail = _cf("A1:A2", _expr(d_white, 1, 'COUNTIF($B$1:$B$9,"x")>0'))
    v = _run94([F.c("A1", 0, 5.0), F.c("A2", 0, "label")], st, "u1.xlsx", tail)
    a = v["stats"]["cf_assumptions"]
    assert v["decision"] == "pass" and v["stats"]["undecided_cells"] == 0, v["stats"]
    assert a["cells"] == 2 and a["unevaluable_rules"] == 1 and "COUNTIF" in a["unevaluable_rule_examples"][0], a
    assert "COUNTIF" in a["examples"][0] and "S!A1" in a["examples"][0], a
    # ... and white text an unevaluable rule might reveal: assumed visible too
    tail = _cf("A1", _expr(d_black, 1, "YEAR($B$1)=2026"))
    v = _run94([F.c("A1", w, 5.0)], st, "u2.xlsx", tail)
    assert v["decision"] == "pass" and v["stats"]["cf_assumptions"]["cells"] == 1, v["stats"]
    # an unevaluable rule that cannot change the verdict is no assumption
    tail = _cf("A1", _expr(d_black, 1, "YEAR($B$1)=2026"))
    v = _run94([F.c("A1", 0, 5.0)], st, "u3.xlsx", tail)
    assert v["decision"] == "pass" and v["stats"]["cf_assumptions"]["cells"] == 0, v["stats"]
    # a cell concealed whatever the rule does stays a mistake
    tail = _cf("A1", _expr(d_white, 1, "YEAR($B$1)=2026"))
    v = _run94([F.c("A1", w, 5.0)], st, "u4.xlsx", tail)
    assert v["decision"] == "fail" and F.locs(v) == ["S!A1"], v["stats"]
    # a referenced value the grading cannot trust (openpyxl cache): unevaluable -> assumed visible
    p = F.book(F.tmp("u5.xlsx"), [("S", F.sheet([F.c("A1", 0, 5.0), F.c("B1", 0, 1.0, f="0+1")],
                                               tail=_cf("A1", _expr(d_white, 1, "$B$1=1"))))], st)
    import zipfile
    with zipfile.ZipFile(p) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["docProps/app.xml"] = (b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
                                 b'extended-properties"><Application>Microsoft Excel Compatible / Openpyxl 3.1.5'
                                 b'</Application></Properties>')
    with zipfile.ZipFile(p, "w") as z:
        for n, b in parts.items():
            z.writestr(n, b)
    v = Engine(p, [C94()]).run()[C94.key]
    assert v["decision"] == "pass" and v["stats"]["cf_assumptions"]["cells"] == 1, v["stats"]
    assert "untrusted" in v["stats"]["cf_assumptions"]["examples"][0], v["stats"]["cf_assumptions"]


def test_94_builtin_formats_never_hide_text():
    st = F.Styles()
    w = st.xf(font=st.font("FFFFFF"))
    pink = st.xf(font=st.font("FFC7CE"))                              # pale pink text: visible on white (Lc 22)
    on_navy = st.xf(fill=st.fill("1F3864"))                           # black text on navy: concealed by itself
    light_red = st.dxf(fill="FFC7CE")                                 # preset "Light Red Fill"
    red_text = st.dxf(font="9C0006")                                  # preset "Red Text"
    custom = st.dxf(fill="1F3864")                                    # NOT a preset: a navy fill (black on it: Lc 7.4)
    green = st.dxf(font="006100", fill="C6EFCE")                      # preset "Green Fill with Dark Green Text"
    always = lambda d: _cf("A1", _expr(d, 1, "TRUE"))                 # noqa: E731
    # pink text under Excel's "Light Red Fill" (FFC7CE on FFC7CE) would be concealed: counts as visible, recorded
    v = _run94([F.c("A1", pink, 5.0)], st, "b1.xlsx", always(light_red))
    assert v["decision"] == "pass" and v["stats"]["cf_builtin_visible"]["cells"] == 1, v["stats"]["cf_builtin_visible"]
    assert "Light Red Fill" in v["stats"]["cf_builtin_visible"]["examples"][0]
    # Excel's "Red Text" (9C0006) on a navy fill: visible (recorded); a custom navy fill under black text:
    # concealed
    v = _run94([F.c("A1", on_navy, 5.0)], st, "b2.xlsx", always(red_text))
    assert v["decision"] == "pass" and v["stats"]["cf_builtin_visible"]["cells"] == 1, v["stats"]
    v = _run94([F.c("A1", 0, 5.0)], st, "b3.xlsx", always(custom))
    assert v["decision"] == "fail" and v["stats"]["cf_builtin_visible"]["cells"] == 0, v["stats"]
    v = _run94([F.c("A1", w, 5.0)], st, "b4.xlsx", always(green))
    assert v["decision"] == "pass" and v["stats"]["cf_builtin_visible"]["cells"] == 0, v["stats"]   # visible anyway
    # a colour scale ending in white under white text never counts as hiding it (2026-10-04: GradingError)
    scale = ('<conditionalFormatting sqref="A1:A3"><cfRule type="colorScale" priority="1"><colorScale><cfvo type="min"/>'
             '<cfvo type="max"/><color rgb="FFFFFFFF"/><color rgb="FF1F4E78"/></colorScale></cfRule></conditionalFormatting>')
    v = _run94([F.c("A1", w, 1.0), F.c("A2", w, 5.0)], st, "b5.xlsx", scale)
    assert v["decision"] == "pass" and v["stats"]["cf_builtin_visible"]["cells"] == 2, v["stats"]
    # ... but a colour scale does not apply to text: white text in its range stays concealed
    v = _run94([F.c("A1", w, "label")], st, "b6.xlsx", scale)
    assert v["decision"] == "fail" and F.locs(v) == ["S!A1"], F.locs(v)
    # a number format blank is not the built-in format's doing: still concealed under a preset
    hide = st.xf(numfmt=";;;")
    v = _run94([F.c("A1", hide, 5.0)], st, "b7.xlsx", always(light_red))
    assert v["decision"] == "fail" and F.locs(v) == ["S!A1"], F.locs(v)


# ============================================================================ the other checks: rule OFF
def test_66_65_unevaluable_rule_is_off_and_recorded():
    # Zeros as dashes (66): a digit format the dash rule would replace, behind a rule that cannot be evaluated
    st = Z.Styles()
    g = st.xf("#,##0.00")
    dash = st.dxf('"-"')
    cells = [Z.c("A1", g, 0), Z.c("A2", g, 0)]
    tail = Z.cf_main("A1:A5", ("expression", dash, None, ["COUNTIF($Z$1:$Z$9,1)>0"], ""))
    v = Engine(Z.book([("S", Z.sheet(cells, tail=tail))], st, name="z66.xlsx"), [C66()]).run()[C66.key]
    a = v["stats"]["cf_assumptions"]
    assert v["decision"] == "fail" and Z.locs(v) == ["S!A1:A2"], v["mistakes"]          # the rule off: '0.00' shows
    assert a["cells"] == 2 and a["decisive_cells"] == 2 and a["unevaluable_rules"] == 1, a
    # the same rule reading a cell that can be read is evaluated: Z1 = 1 -> the dash shows
    tail = Z.cf_main("A1:A5", ("expression", dash, None, ["$Z$1=1"], ""))
    v = Engine(Z.book([("S", Z.sheet(cells + [Z.c("Z1", 0, 1)], tail=tail))], st, name="z66b.xlsx"),
               [C66()]).run()[C66.key]
    assert v["decision"] == "pass" and v["stats"]["cf_assumptions"]["cells"] == 0, v["stats"]["cf_assumptions"]
    # Negatives in parentheses (65): the parenthesis rule cannot be evaluated -> off -> '-5' fails
    st5 = Z.Styles()
    g5 = st5.xf("#,##0")
    par = st5.dxf("#,##0;(#,##0)")
    tail = Z.cf_main("A1:A5", ("expression", par, None, ['ISNUMBER(MATCH("x",$Z:$Z,0))'], ""))
    v = Engine(Z.book([("S", Z.sheet([Z.c("A1", g5, -5)], tail=tail))], st5, name="n65.xlsx"), [C65()]).run()[C65.key]
    assert v["decision"] == "fail" and v["stats"]["cf_assumptions"]["decisive_cells"] == 1, v["stats"]["cf_assumptions"]


TESTS = [test_operators, test_functions, test_references_absolute_relative_and_cross_sheet,
         test_unevaluable_never_guessed, test_rule_types, test_lookup_reads_other_cells_through_the_value_source,
         test_prefetch_registers_every_referenced_cell, test_94_attempt_2924_cover_check_line,
         test_94_rules_reading_other_cells_and_sheets, test_94_unevaluable_rule_assumed_visible,
         test_94_builtin_formats_never_hide_text, test_66_65_unevaluable_rule_is_off_and_recorded]


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
        for mod in (F, Z):
            d = getattr(mod, "_TMP", None)
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
