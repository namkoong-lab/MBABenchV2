"""Tests for detchecks.core.formula (plain asserts, no test framework).

Run:  cd judge && python -m detchecks.tests.test_formula

Real formula strings below were copied from the toy workbooks
(~/Downloads/drive-download-20261001T230033Z-1-001/<check>/{Pass,Fail}/...) - the toy
and cell are named next to each.  No workbook is opened by these tests.
"""
from __future__ import annotations

import sys
import traceback

from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.formula.translate import Translator

from detchecks.core import formula as F
from detchecks.errors import GradingError


# --------------------------------------------------------------------------- helpers
def ops(text):
    return [(o.kind, o.raw) for o in F.parse(text).operands]


def refkinds(text):
    return [(o.kind, o.raw) for o in F.parse(text).operands if o.kind not in ("number", "string", "bool")]


def fnames(text):
    return [c.name for c in F.parse(text).functions]


def raises(fn, *a, exc=F.FormulaError, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError(f"{fn.__name__}{a} did not raise {exc.__name__}")


def whole(text, **kw):
    return [o.raw for o in F.whole_column_or_row_refs(text, **kw)]


# --------------------------------------------------------------------------- lexer basics
def test_simple_and_leading_equals():
    assert ops("SUM(A1:B2)") == [("range", "A1:B2")]
    assert ops("=SUM(A1:B2)") == [("range", "A1:B2")]
    f = F.parse("A1+B2*3")
    assert [o.kind for o in f.operands] == ["cell", "cell", "number"]
    assert f.operands[0].bounds == (1, 1, 1, 1) and f.operands[1].bounds == (2, 2, 2, 2)


def test_prefixes_are_stripped_and_builtin():
    f = F.parse("_xlfn.XLOOKUP(A1,_xlfn._xlws.FILTER(B1:B5,C1:C5>0),_xlfn.STDEV.P(D1:D3))")
    assert [c.name for c in f.functions] == ["XLOOKUP", "FILTER", "STDEV.P"]
    assert [c.prefix for c in f.functions] == ["_xlfn.", "_xlfn._xlws.", "_xlfn."]
    assert all(c.builtin for c in f.functions)
    f = F.parse("_xludf.MYUDF(1)+_xll.ADDIN(2)+_xlfn.SINGLE(A1:A3)")
    assert [(c.name, c.builtin) for c in f.functions] == [("MYUDF", False), ("ADDIN", False), ("SINGLE", True)]
    # _xlfn._TRO_* keep their leading underscore after the prefix is stripped
    assert fnames("_xlfn._TRO_TRAILING(A:A)") == ["_TRO_TRAILING"]
    # lower-case functions written by openpyxl
    assert fnames("sum(a1:a3)+offset(a1,1,0)") == ["SUM", "OFFSET"]


def test_let_lambda_parameters_are_locals():
    f = F.parse("_xlfn.LET(_xlpm.x,1,_xlpm.x+A1)")
    assert [o.kind for o in f.operands] == ["param", "number", "param", "cell"]
    assert f.params == frozenset({"x"})
    # _xlfn._xlpm. spelling
    f = F.parse("_xlfn.LET(_xlfn._xlpm.x,1,_xlfn._xlpm.x+1)")
    assert [o.kind for o in f.operands] == ["param", "number", "param", "number"]
    # openpyxl-written LET without _xlpm.: declared names are locals, others stay names
    f = F.parse("LET(x,1,y,2,x+y+Rate)")
    assert [(o.kind, o.raw) for o in f.operands if o.kind in ("param", "name")] == [
        ("param", "x"), ("param", "y"), ("param", "x"), ("param", "y"), ("name", "Rate")]
    # LAMBDA params, and a LET-bound lambda called like a function is local, not a name
    f = F.parse("LET(f,LAMBDA(a,a*2),f(3)+g(1))")
    calls = {c.name: (c.local, c.builtin) for c in f.functions}
    assert calls["F"] == (True, False) and calls["G"] == (False, False) and calls["LAMBDA"][1]
    assert [r.name for r in f.names_referenced()] == ["g"]
    # _xlpm.f( call is local
    f = F.parse("_xlfn.LET(_xlpm.f,_xlfn.LAMBDA(_xlpm.a,_xlpm.a*2),_xlpm.f(3))")
    assert [c.local for c in f.functions if c.name == "F"] == [True]
    # a parameter that shadows a defined name hides it inside its scope only
    f = F.parse("Rate+LET(Rate,2,Rate*A1)")
    assert [o.kind for o in f.operands if o.raw == "Rate"] == ["name", "param", "param"]
    # real toy formula (50/T4 EasyRev 'Solution Model')
    f = F.parse("_xlfn.LET(_xlpm.T,_xlfn.TOROW('1_Historical_Data'!$B$5:$B$40),MONTH(_xlpm.T)&\"/\"&YEAR(_xlpm.T))")
    assert {o.name for o in f.operands if o.kind == "param"} == {"T"}
    assert not f.names_referenced()


def test_eta_lambda_function_values():
    # 49/T3 MonthlyDCF: BYROW(..., _xleta.CONCAT)
    f = F.parse("_xlfn.BYROW('Monthly Model'!$C$60:$D$174,_xleta.CONCAT)")
    assert [(c.name, c.eta) for c in f.functions] == [("BYROW", False), ("CONCAT", True)]
    assert [o.kind for o in f.operands] == ["range"]
    assert "RAND" in F.parse("_xlfn.MAP(A1:A3,_xleta.RAND)").function_names()


def test_implicit_intersection():
    f = F.parse("@A1:A5")
    assert f.operands[0].kind == "range" and f.operands[0].implicit
    f = F.parse("_xlfn.SINGLE(Sheet1!A1:A5)*2")
    assert f.operands[0].implicit and f.operands[0].sheet == "Sheet1"
    f = F.parse("@INDIRECT(\"A1\")")
    assert f.functions[0].implicit and f.functions[0].name == "INDIRECT"


def test_spill_references():
    f = F.parse("SUM(A1#)")
    assert (f.operands[0].kind, f.operands[0].raw, f.operands[0].spill) == ("spill", "A1", True)
    f = F.parse("'Other Sheet'!$B$2#*2")
    o = f.operands[0]
    assert o.kind == "spill" and o.sheet == "Other Sheet" and o.bounds == (2, 2, 2, 2)
    f = F.parse("_xlfn.ANCHORARRAY('Solution Model - G1L3W2'!$H$641)")      # 50/T2 TAM G22
    assert f.operands[0].kind == "spill" and f.operands[0].sheet == "Solution Model - G1L3W2"
    f = F.parse("OFFSET(A1,0,0)#")
    assert f.functions[0].spilled
    # a spilled name
    f = F.parse("SUM(MyRange#)")
    assert f.operands[0].kind == "name" and f.operands[0].spill


def test_trimmed_ranges():
    for text, trim in [("SUM(A:.A)", "trailing"), ("SUM(A.:A)", "leading"), ("SUM(A.:.A)", "all"),
                       ("SUM($C:.$C)", "trailing"), ("SUM(A1:.B10)", "trailing"),
                       ("_xlfn._TRO_TRAILING(A:A)", "trailing"), ("_xlfn._TRO_LEADING(A:A)", "leading"),
                       ("_xlfn._TRO_ALL(Sheet1!1:3)", "all"), ("_xlfn.TRIMRANGE(A:A)", "trimrange"),
                       ("_xlfn.TRIMRANGE((A:A))", "trimrange"), ("TRIMRANGE(A:A,2,2)", "trimrange")]:
        f = F.parse(text)
        refs = [o for o in f.operands if o.kind not in ("number",)]
        assert len(refs) == 1 and refs[0].kind == "trimmed_range" and refs[0].trim == trim, (text, refs)
        assert F.whole_column_or_row_refs(text) == [], text
    # a name that merely ends with a dot is not trim syntax
    f = F.parse("SUM(Start.:Finish)")
    assert f.operands[0].kind == "range" and f.operands[0].names == ("Start.", "Finish")


def test_structured_references():
    for text, table in [("SUM(Table1[Col])", "Table1"), ("Table1[[#This Row],[Col]]", "Table1"),
                        ("[@Col]*2", None), ("Table1[@[Col 1]]", "Table1"), ("ROWS(Table1[#All])", "Table1"),
                        ("SUM(Table1[[#Headers],[A]:[B]])", "Table1"), ("SUM(Tbl['#Count])", "Tbl"),
                        ("SUM(Table1[])", "Table1")]:
        f = F.parse(text)
        s = [o for o in f.operands if o.kind == "structured"]
        assert len(s) == 1 and s[0].table == table, (text, f.operands)
        assert F.whole_column_or_row_refs(text) == []
        assert not f.names_referenced()


def test_external_references():
    cases = [
        ("[1]Sheet1!A1", "1", "Sheet1", "cell"),
        ("'[1]Sheet 1'!A1", "1", "Sheet 1", "cell"),
        ("[1]!Name", "1", None, "name"),
        ("[Book.xlsx]Sheet1!A1", "Book.xlsx", "Sheet1", "cell"),
        ("'C:\\dir\\[Book.xlsx]Sheet'!A1", "C:\\dir\\Book.xlsx", "Sheet", "cell"),
        ("'https://x.com/a/[Book.xlsx]Sheet1'!A1", "https://x.com/a/Book.xlsx", "Sheet1", "cell"),
        ("'C:\\Models\\Lookups\\Scenario_Lists.xlsx'!ScenarioList", "C:\\Models\\Lookups\\Scenario_Lists.xlsx", None, "name"),
        ("[1]Jan:Dec!A1", "1", "Jan", "cell"),
        ("'[Book.xlsx]Jan:Dec'!A1", "Book.xlsx", "Jan", "cell"),
        ("'[1]My Data'!A:A", "1", "My Data", "whole_column"),
    ]
    for text, ext, sheet, kind in cases:
        o = F.parse(text).operands[0]
        assert (o.external, o.sheet, o.kind) == (ext, sheet, kind), (text, o)
        assert F.references_external_workbook(text), text
        assert not F.references_other_sheet(text, "Anything"), text      # another BOOK is not another sheet
    # [0]! is this workbook
    o = F.parse("[0]!Rate").operands[0]
    assert o.external is None and o.workbook_scoped and o.name == "Rate"
    assert not F.references_external_workbook("[0]!Rate")
    # toy forms: 51/T1 Nike M63, 51/T2 DailyDetail G44, 51/T3 8Options AW6, 95/T1 Meridian C8, 95/T3 J8
    for text in ["[1]Prices!$C$5", "'[1]Annual Summary'!$G$41", "[1]Offer!$C$12",
                 "'[1]P&L Budget \u2013 GL'!$O$10", "[1]Comps!$I$5"]:
        assert F.references_external_workbook(text), text
    # 51/T4 TechVentures C27: external reference inside a calculation
    f = F.parse("[1]Rates!$B$2*C25")
    assert f.references_external_workbook() and [o.raw for o in f.external_references()] == ["[1]Rates!$B$2"]
    # 95/T4 SaaSScenario: x14 data-validation list <xm:f>
    assert F.references_external_workbook("[1]!ScenarioList")
    # 95/T2 CFForecast: defined name text
    assert F.references_external_workbook("'[1]Annual Summary'!$G$7")
    # web hyperlink / file names inside text are not links (95/T3)
    assert not F.references_external_workbook('HYPERLINK("https://www.nasdaq.com/market","Nasdaq")')
    assert not F.references_external_workbook('"Source: Q3_comps.xlsx"&A1')
    assert not F.references_external_workbook('"[Book.xlsx]Sheet1!A1"')
    # 51/T3: cross-sheet links (green) are NOT external
    assert not F.references_external_workbook("MAX($AU$6:$AX$6)-AV$6+Assumptions!$C$4")


def test_ambiguous_book_qualifier():
    f = F.parse("Other.xlsx!Revenue*2")
    o = f.operands[0]
    assert o.ambiguous_book and o.sheet == "Other.xlsx" and o.external is None
    raises(F.references_external_workbook, "Other.xlsx!Revenue*2", exc=F.AmbiguousReferenceError)
    assert F.references_external_workbook("Other.xlsx!Revenue*2", sheet_names=["Inputs", "Calc"])
    assert not F.references_external_workbook("Other.xlsx!Revenue*2", sheet_names=["other.xlsx"])
    assert F.references_external_workbook("'Other Book.xlsx'!Revenue", sheet_names=["Inputs"])
    # a cell body can only be a sheet: no ambiguity
    o = F.parse("Data.xlsx!A1").operands[0]
    assert not o.ambiguous_book and o.sheet == "Data.xlsx"
    assert not F.references_external_workbook("Data.xlsx!A1")
    # names_used_by refuses to guess without the sheet list
    raises(F.names_used_by, "Other.xlsx!Revenue", "Calc", F.NameTable([("Revenue", None, "Calc!$A$1", False)]),
           exc=F.AmbiguousReferenceError)
    tab = F.NameTable([("Revenue", None, "Calc!$A$1", False)], sheet_names=["Calc"])
    assert F.names_used_by("Other.xlsx!Revenue", "Calc", tab) == []          # another workbook's name
    tab = F.NameTable([("Revenue", "Other.xlsx", "'Other.xlsx'!$A$1", False)], sheet_names=["Calc", "Other.xlsx"])
    assert [n.scope for n in F.names_used_by("Other.xlsx!Revenue", "Calc", tab)] == ["Other.xlsx"]
    # module helpers pass sheet_names on to name resolution
    names = [("Ext", None, "Other.xlsx!Revenue", False), ("Revenue", None, "Calc!$A$1", False)]
    assert F.references_external_workbook("Ext*2", names=names, scope_sheet="Calc", sheet_names=["Calc"])


def test_three_d_and_quoted_sheets():
    o = F.parse("SUM(Sheet1:Sheet3!A1)").operands[0]
    assert (o.sheet, o.sheet_end, o.kind) == ("Sheet1", "Sheet3", "cell")
    o = F.parse("SUM('Jan 1:Dec 1'!A1:B2)").operands[0]
    assert (o.sheet, o.sheet_end, o.kind) == ("Jan 1", "Dec 1", "range")
    assert F.parse("SUM(Sheet1:Sheet3!A1)").other_sheets("Sheet2") == {"Sheet1", "Sheet3"}
    assert F.references_other_sheet("SUM(Sheet1:Sheet3!A1)", "Sheet1")       # spans other sheets
    o = F.parse("'It''s'!A1").operands[0]
    assert o.sheet == "It's" and o.qualifier == "'It''s'"
    # sheet names that look like references
    o = F.parse("'A1'!B2").operands[0]
    assert (o.sheet, o.kind, o.bounds) == ("A1", "cell", (2, 2, 2, 2))
    o = F.parse("R1C1!A1").operands[0]
    assert (o.sheet, o.kind) == ("R1C1", "cell")
    assert F.whole_column_or_row_refs("SUM('1:1'!A1)+SUM('A:B'!A1)") == []
    o = F.parse("'Tax & Combinations'!AW6").operands[0]
    assert o.sheet == "Tax & Combinations"


def test_error_literals():
    for e in F.ERROR_LITERALS:
        f = F.parse(f"IFERROR(A1,{e})")
        assert [(o.kind, o.value) for o in f.operands] == [("cell", None), ("error", e)], e
    f = F.parse("#n/a")
    assert f.operands[0].kind == "error" and f.operands[0].value == "#N/A"
    f = F.parse("E79-#REF!")                                                 # 22/T4 HouseorFlat E80
    assert [o.kind for o in f.operands] == ["cell", "error"] and len(f.ref_errors()) == 1
    f = F.parse("'4 - DCF'!#REF!")                                           # 29/T2 broken name
    assert f.operands[0].kind == "error" and f.operands[0].sheet == "4 - DCF" and f.ref_errors()
    assert F.parse("Instructions!#REF!").ref_errors()                        # hidden solver_opt
    assert not F.parse('"#REF!"&A1').ref_errors()
    raises(F.parse, "#BOGUS!")


def test_array_constants():
    f = F.parse("SUM({1,2;3,4})")
    assert f.operands[0].kind == "array" and f.operands[0].value == ((1.0, 2.0), (3.0, 4.0))
    f = F.parse('{"a",TRUE;#N/A,-1.5E-3}')
    assert f.operands[0].value == (("a", True), ("#N/A", -0.0015))
    assert f.strings == ("a",)
    f = F.parse('INDEX({"x","y"},2)')
    assert [o.kind for o in f.operands] == ["array", "number"]
    raises(F.parse, "{1,A1}")


def test_whitespace_cr_lf_tab():
    f = F.parse("SUM(\r\nA1,\tB1)")
    assert [o.raw for o in f.operands] == ["A1", "B1"] and f.functions[0].n_args == 2
    assert fnames("\tNOW()") == ["NOW"]
    assert fnames("1+\r\nOFFSET(A1,\r\n1,0)") == ["OFFSET"]
    assert ops(" A1 + B1 ") == [("cell", "A1"), ("cell", "B1")]
    assert whole("SUM(\r\n$A:$A\r\n)") == ["$A:$A"]
    # intersection operator (space) between two ranges
    assert ops("A1:B5 B2:C3") == [("range", "A1:B5"), ("range", "B2:C3")]


def test_strings_are_never_references_functions_or_names():
    f = F.parse('N("uses OFFSET(A1,1,1)")+A1')
    assert fnames('N("uses OFFSET(A1,1,1)")+A1') == ["N"]
    assert f.strings == ("uses OFFSET(A1,1,1)",)
    assert fnames('"TODAY()"') == [] and ops('"TODAY()"') == [("string", '"TODAY()"')]
    assert whole('"1:1"&"A:B"') == []
    assert whole('"Mix "&"1:2"') == []                                     # 87/T5 LiquiditySplatter AD9
    assert F.parse('"Rate"&"x"').names_referenced() == []
    assert not F.references_other_sheet('"Inputs!A1"', "Calc")
    f = F.parse('"say ""hi"" to A1"&B1')
    assert f.strings == ('say "hi" to A1',) and [o.raw for o in f.references()] == ["B1"]
    # INDIRECT with a literal: the literal is text, not a reference (80/T2 HouseorFlat AN600)
    f = F.parse("($AL600+$AM600)*((1+INDIRECT(\"'Assumptions'!D80\"))^(1/Assumptions!$D$8))-($AL600+$AM600)")
    assert "INDIRECT" in f.function_names()
    assert f.other_sheets("Solution Model - Owning (F)") == {"Assumptions"}
    assert f.strings == ("'Assumptions'!D80",)
    # CELL inside a text-producing header formula still calls CELL (80/T4 TheLiquidityEngine A2)
    f = F.parse('MID(CELL("filename",A1),FIND("]",CELL("filename",A1))+1,255)')
    assert [c.name for c in f.functions].count("CELL") == 2


def test_numbers_and_row_ranges():
    assert ops("1E+3+.5+1.5E-3+2") == [("number", "1E+3"), ("number", ".5"), ("number", "1.5E-3"), ("number", "2")]
    assert ops("SUM(1:1)") == [("whole_row", "1:1")]
    assert ops("SUM(10:12)") == [("whole_row", "10:12")]
    assert ops("5%") == [("number", "5")]
    assert ops("TRUE+FALSE") == [("bool", "TRUE"), ("bool", "FALSE")]
    assert fnames("TRUE()") == ["TRUE"]


def test_range_ending_in_a_function():
    f = F.parse("SUM(D5:OFFSET(D5,0,3))")
    assert fnames("SUM(D5:OFFSET(D5,0,3))") == ["SUM", "OFFSET"]
    assert [o.raw for o in f.references()] == ["D5", "D5"]
    f = F.parse("SUM($B$5:INDEX(A:A,3))")
    assert fnames("SUM($B$5:INDEX(A:A,3))") == ["SUM", "INDEX"] and whole("SUM($B$5:INDEX(A:A,3))") == ["A:A"]
    f = F.parse("SUM(INDEX(A:A,1):B5)")
    assert [o.raw for o in f.references()] == ["A:A", "B5"]
    f = F.parse("SUM(Sheet1!A1:INDEX(Sheet1!B:B,9))")
    assert [o.raw for o in f.references()] == ["Sheet1!A1", "Sheet1!B:B"]


def test_function_structure():
    f = F.parse("IF(A1>0,ROUND(SUM(B1:B3)/2,0),\"\")")
    names = [(c.name, c.depth, c.parent, c.parent_arg, c.n_args) for c in f.functions]
    assert names == [("IF", 0, None, None, 3), ("ROUND", 1, 0, 1, 2), ("SUM", 2, 1, 0, 1)]
    assert f.arg_text(0, 1) == "ROUND(SUM(B1:B3)/2,0)" and f.arg_text(1, 1) == "0"
    b = [o for o in f.operands if o.raw == "B1:B3"][0]
    assert b.func_path == ("IF", "ROUND", "SUM") and b.arg_index == 0 and b.call_index == 2
    assert F.parse("TODAY()").functions[0].n_args == 0
    assert F.parse("ROUNDDOWN(A1,)").functions[0].n_args == 2               # empty trailing argument
    assert [o.raw for o in f.operands_in_arg(f.functions[0], 0)] == ["A1", "0"]
    assert [c.name for c in F.parse("_xlfn.LAMBDA(_xlpm.r,_xlpm.r*2)(3)").functions] == ["LAMBDA"]


def test_name_validity():
    for bad in ["LIM1", "R2C3", "A1", "XFD1048576", "R", "C", "RC", "r1c1", "TAX2024", "$A$1", "Has Space",
                "1abc", "TRUE", ""]:
        assert F.name_validity_problem(bad) is not None, bad
    for good in ["Rate", "XFE1", "A1048577", "Tax_2024", "my.name", "\\name", "_private", "Tax", "Ma\u00dfe",
                 "Is_Valid?", "_xlnm.Print_Area", "R_", "C_Rate"]:
        assert F.name_validity_problem(good) is None, good
    # as tokens: LIM1 and R2C3 are references, never names
    f = F.parse("Lim1+R2C3+XFE1")
    assert [(o.kind, o.r1c1) for o in f.operands] == [("cell", False), ("cell", True), ("name", False)]
    assert f.operands[0].bounds == (1, F.col_index("LIM"), 1, F.col_index("LIM"))
    assert [r.name for r in f.names_referenced()] == ["XFE1"]


def test_malformed_formulas_raise_loudly():
    for bad in ["SUM(A1", "SUM(A1))", '"abc', "'Sheet1!A1", "A1+[", "{1,2", "1;2", "A1|B1", "Sheet1!", "SUM(A1}"]:
        raises(F.parse, bad)
    assert issubclass(F.FormulaError, GradingError)                        # NO FALLBACK: a grading error


# --------------------------------------------------------------------------- helpers per check
def test_references_other_sheet_49_50_traps():
    # 49/T3 + 50/T3 MonthlyDCF G61/G64/G87: self-qualified references are the same sheet
    t = "'Monthly Model'!$G$66+'Monthly Model'!$G$85"
    assert not F.references_other_sheet(t, "Monthly Model")
    assert not F.references_other_sheet(t, "monthly model")                 # case-insensitive
    assert F.references_other_sheet(t, "Annual Model")
    # pure cross-sheet pulls
    for text, own in [("Assumptions!$F$73", "Monthly Model"),                 # 50/T3 G115
                      ("Comp!$A$5:$A$19", "Solution Model - Regular"),       # 50/T1 C17
                      ("_xlfn.ANCHORARRAY('Solution Model - G1L3W2'!$H$641)", "Solution Model - Overview"),
                      ("_xlfn.TOROW('1_Historical_Data'!$C$5:$C$40)", "Solution Model")]:
        assert F.references_other_sheet(text, own), text
    f = F.parse("Assumptions!$F$73")
    assert f.pure_reference() is not None
    assert F.parse("_xlfn.ANCHORARRAY('Solution Model - G1L3W2'!$H$641)").pure_reference().sheet == "Solution Model - G1L3W2"
    assert F.parse("_xlfn.TOROW('1_Historical_Data'!$C$5:$C$40)").pure_reference() is None
    assert F.parse("_xlfn.TOROW('1_Historical_Data'!$C$5:$C$40)").pure_reference(
        transparent=("ANCHORARRAY", "SINGLE", "TOROW")).sheet == "1_Historical_Data"
    assert F.parse("-(Inputs!B2)").pure_reference() is not None
    assert F.parse("=+Inputs!B2").pure_reference() is not None
    assert F.parse("Inputs!B2*2").pure_reference() is None
    assert F.parse("Instructions!B2&\": \"&Instructions!B4").pure_reference() is None
    # mixed calculations with other sheets (either colour allowed by the 49/50 ruling)
    f = F.parse("IF('Monthly Model'!$D$6,$G$37,$G$28*Assumptions!$F$24/Assumptions!$F$25)")
    assert f.other_sheets("DCF Model (it.)") == {"Monthly Model", "Assumptions"}
    f = F.parse("_xlfn.ROUNDDOWN((_xlfn.ANCHORARRAY($H$1)-1)/Assumptions!$F$7*4,)+1")
    assert f.other_sheets("Monthly Model") == {"Assumptions"}
    # same-sheet calculations (49/T1, T4, T5 and the shared child of T2)
    for text in ['IFERROR(_xlfn.ANCHORARRAY($G$5)-$G$6:$AP$6,"n/a")', 'IFERROR(ABS(_xlfn.ANCHORARRAY($G$7)),"n/a")',
                 "IFERROR(_xlfn.ANCHORARRAY($K$6)/_xlfn.ANCHORARRAY($G$6),0)", "SUM($G$66:$G$72)",
                 "ABC$169+ABC$170"]:
        assert not F.references_other_sheet(text, "Solution Model"), text
    # a qualified NAME is not counted as another sheet until it is resolved
    assert not F.references_other_sheet("Inputs!Rate*2", "Calc")
    names = [("Rate", "Inputs", "Inputs!$B$2", False)]
    assert F.references_other_sheet("Inputs!Rate*2", "Calc", names=names)
    names = [("WACC", None, "Inputs!$B$9", False)]
    assert F.references_other_sheet("WACC*B2", "Calc", names=names)
    assert not F.references_other_sheet("WACC*B2", "Inputs", names=names)


def test_volatile_functions_80():
    vol = F.VOLATILE_FUNCTIONS
    assert F.parse("OFFSET(Assumptions!$D$6,0,0)&\" Valuation\"").function_names() & vol == {"OFFSET"}   # 80/T1
    assert F.parse("_xlfn.STDEV.P(OFFSET($K$16,0,0,COUNTA($C$16:$C$37),1))").function_names() & vol == {"OFFSET"}
    assert F.parse("RANDBETWEEN(1,100)").function_names() & vol == {"RANDBETWEEN"}                    # 80/T6 E10
    assert F.parse("_xlfn.STDEV.P(_xlfn.ANCHORARRAY($K$16))").function_names() & vol == set()          # 80/T1 PASS
    # the cover-sheet date stamp (80/T3 CorpBond Cover!C6) is exactly TODAY()
    f = F.parse("TODAY()")
    assert [c.name for c in f.functions] == ["TODAY"] and not f.operands
    assert F.parse("D6-TODAY()").function_names() == {"TODAY"}
    # literals mentioning volatile functions do not call them
    for text in ['"TODAY()"', 'N("uses OFFSET(A1,1,1)")+A1', '"OFFSET, INDIRECT and TODAY()"']:
        assert F.parse(text).function_names() & vol == set(), text
    # quick pre-filter is conservative
    assert F.quick_may_call("1+offset(A1,1,1)", vol)
    assert not F.quick_may_call('"OFFSET(A1)"&B1', vol)


def test_volatile_only_inside_defined_name_80():
    # 80/T5 FruitJuice: the volatile function lives only in the Name Manager
    names = F.NameTable([("Inflation", None, "OFFSET('Solution Model'!$H$7,0,0,1,120)", False),
                         ("SellInflation", None, "'Solution Model'!$H$8:$DW$8", False)])
    cell = "Assumptions!$F$67*Inflation/Assumptions!$F$317"
    assert F.parse(cell).function_names() & F.VOLATILE_FUNCTIONS == set()
    r = F.expand(cell, "Solution Model", names)
    assert r.calls_any("OFFSET") and [n.name for n in r.names] == ["Inflation"]
    assert F.functions_reached(cell, "Solution Model", names) == {"OFFSET"}
    # chains: Total = SUM(Base), Base = OFFSET(...)
    names = F.NameTable([("Total", None, "SUM(Base)", False), ("Base", None, "OFFSET(A1,0,0,3,1)", False)])
    r = F.expand("Total*2", "Calc", names)
    assert r.functions == {"SUM", "OFFSET"} and [n.name for n in r.names] == ["Total", "Base"]
    assert [tuple(n.name for n in via) for via, c in r.calls if c.name == "OFFSET"] == [("Total", "Base")]
    # LAMBDA name called as a function: MyFn=LAMBDA(r,INDIRECT(r))
    names = F.NameTable([("MyFn", None, "_xlfn.LAMBDA(_xlpm.r,_xlfn.INDIRECT(_xlpm.r))", False)])
    assert F.expand("MyFn(3)", "Calc", names).calls_any("INDIRECT")
    # cycles are safe
    names = F.NameTable([("A_", None, "B_+1", False), ("B_", None, "A_*2", False)])
    r = F.expand("A_", None, names)
    assert r.cycle and {n.name for n in r.names} == {"A_", "B_"}
    # an unparsable definition is loud
    names = F.NameTable([("Bad", None, "SUM(A1", False)])
    raises(F.expand, "Bad+1", None, names)


def test_whole_column_refs_87():
    # 87/T1 MeridianOutdoor: quoted sheet names with spaces and hyphens
    t = "_xlfn.XLOOKUP($A5,'Budget - Income Statement'!$A:$A,'Budget - Income Statement'!$B:$B)"
    w = F.whole_column_or_row_refs(t)
    assert [(o.raw, o.sheet, o.kind) for o in w] == [
        ("'Budget - Income Statement'!$A:$A", "Budget - Income Statement", "whole_column"),
        ("'Budget - Income Statement'!$B:$B", "Budget - Income Statement", "whole_column")]
    assert whole("_xlfn.XLOOKUP($A5,'Budget - Income Statement'!$A$9:$A$29,'Budget - Income Statement'!$B$9:$B$29)") == []
    # 87/T2 OperationsWeekly BTT21: whole rows
    assert whole("IF(BTT$13,-SUMIFS($17:$17,$4:$4,BTT$4,$3:$3,BTT$3)*Assumptions!$E$17,0)") == ["$17:$17", "$4:$4", "$3:$3"]
    assert whole("IF(BTT$13,-SUMIFS($H$17:$FLV$17,_xlfn.ANCHORARRAY($H$4),BTT$4,_xlfn.ANCHORARRAY($H$3),BTT$3)*Assumptions!$E$17,0)") == []
    # 87/T3 CapitalinMotion AA14: the TrimRef form passes, the plain form fails
    p = ('Z14*IF(_xlfn.XLOOKUP($D14,_xlfn._TRO_TRAILING(Assumption!$C:$C),_xlfn._TRO_TRAILING(Assumption!$F:$F))="Sales",'
         '(((1+Assumption!$F$11)*(1+Assumption!$F$12))^(1/Assumption!$F$7)),(((1+Assumption!$F$12)*(1+Assumption!$F$13))^(1/Assumption!$F$7)))')
    assert whole(p) == []
    assert whole(p.replace("_xlfn._TRO_TRAILING(Assumption!$C:$C)", "Assumption!$C:$C")
                  .replace("_xlfn._TRO_TRAILING(Assumption!$F:$F)", "Assumption!$F:$F")) == ["Assumption!$C:$C", "Assumption!$F:$F"]
    # 87/T4 FourShops H38: unquoted sheet prefix, string criteria
    assert whole('SUMIFS(Assumption!$F:$F,Assumption!$C:$C,"Total Debt",Assumption!$E:$E,"Whole company")') == [
        "Assumption!$F:$F", "Assumption!$C:$C", "Assumption!$E:$E"]
    # 87/T5 LiquiditySplatter: AD4 fails, AD9 string literal passes
    assert whole('INDEX($A:$A,2)&" - notes"') == ["$A:$A"]
    assert whole('"Mix "&"1:2"') == []
    # 87/T6 TwoNOne: the whole column lives only in the defined name Peer_EV
    pass_names = [("Peer_EV", None, "Comp!$H$5:$H$18", False), ("MonthsperYear", None, "'Assumptions - ABC'!$D$61", False)]
    fail_names = [("Peer_EV", None, "Comp!$H:$H", False), ("MonthsperYear", None, "'Assumptions - ABC'!$D$61", False)]
    assert whole("MEDIAN(Peer_EV)") == []
    assert whole("MEDIAN(Peer_EV)", names=pass_names, scope_sheet="Comp - MN Corp.") == []
    assert whole("MEDIAN(Peer_EV)", names=fail_names, scope_sheet="Comp - MN Corp.") == ["Comp!$H:$H"]
    assert F.parse("Comp!$H:$H").whole_column_or_row_refs()                 # the name text itself
    # other forms
    assert whole("SUM($1:$1)+SUM(Data!$A:$C)+SUM('My Sheet'!A:A)+SUM(Data:Sheet3!A:A)") == [
        "$1:$1", "Data!$A:$C", "'My Sheet'!A:A", "Data:Sheet3!A:A"]
    assert whole("SUM([1]Data!A:A)+SUM('[1]My Data'!A:A)") == ["[1]Data!A:A", "'[1]My Data'!A:A"]
    assert whole("sum(a:a)") == ["a:a"]
    assert whole("SUM(A1:A1048576)") == ["A1:A1048576"]                    # Excel shows A:A
    assert whole("SUM(A1:XFD1)") == ["A1:XFD1"]
    assert whole("SUM(A2:A1048576)") == [] and whole("SUM(A2:A1048576)", include_sheet_edge=True) == ["A2:A1048576"]
    assert whole("SUM(A2:A1000000)", include_sheet_edge=True) == []
    assert whole("ROW(1:10)+COLUMNS($E:E)") == ["1:10", "$E:E"]
    assert whole("SUM(A1#)+SUM(StartN:EndN)+_xlfn.ANCHORARRAY(A1)") == []
    assert whole('COUNTIF(INDIRECT("A:A"),10)') == []
    assert whole("SUM(Table1[Val])+ROWS(Table1[#All])") == []
    assert whole("Tax:Rev") == ["Tax:Rev"]                                 # Excel reads TAX:REV as columns


def test_quick_whole_filter_is_conservative():
    samples = ["SUM(A:A)", "SUM($1:$1)", "'My Sheet'!$A:$C", "A1:A1048576", "A1:XFD1", "SUM(A2:A1048576)",
               "SUM(A1:B2)", '"A:B"', "x", "SUM(A:.A)", "[1]Data!A:A", "SUM(\r\n$A:$A\r\n)", "INDEX($A:$A,2)"]
    for s in samples:
        full = bool(F.parse(s).whole_column_or_row_refs(include_sheet_edge=True))
        if full:
            assert F.quick_may_have_whole_refs(s), s
    assert not F.quick_may_have_whole_refs('"Mix "&"1:2"')
    assert not F.quick_may_have_whole_refs("SUM(A1:B2)")


# --------------------------------------------------------------------------- names (29)
_HIDDEN_ADDIN = [("solver_opt", None, "Instructions!#REF!", True), ("RiskNumIterations", None, "100000", True),
                 ("IQ_CH", None, "110000", True)]


def _used(formulas, names):
    tab = F.NameTable(names)
    used = set()
    for sheet, text in formulas:
        for n in F.names_used_by(text, sheet, tab):
            used.add(n.name)
    return used


def test_names_29_t1_easydcf():
    names = [("Tax_rate", None, "'Solution Model'!$H$37", False), ("WACC_rate", None, "'Solution Model'!$H$38", False),
             ("_xlnm.Print_Area", "Solution Model", "'Solution Model'!$A$1:$S$43", False)] + _HIDDEN_ADDIN
    fail_extra = [("Tax_rate_v1", None, "'Solution Model'!$H$37", False), ("WACC_rate_old", None, "'Solution Model'!$H$33", False)]
    cells = [("Solution Model", "I$8*(1-Tax_rate)"), ("Solution Model", "I$27/(1+WACC_rate)^(I$28-0.5)"),
             ("Solution Model", "$H$33*$H$35/($H$35+$H$36)+$H$34*(1-Tax_rate)*$H$36/($H$35+$H$36)")]
    assert _used(cells, names) == {"Tax_rate", "WACC_rate"}
    used = _used(cells, names + fail_extra)
    assert "Tax_rate_v1" not in used and "WACC_rate_old" not in used          # same target cell, different token
    tab = F.NameTable(names)
    assert tab.get("_xlnm.Print_Area", "Solution Model").builtin
    assert tab.get("solver_opt").hidden and F.parse(tab.get("solver_opt").text).ref_errors()


def test_names_29_t2_broken_definition():
    n = F.DefinedName("Tax_rate_GeneralCarSales", None, "'4 - DCF'!#REF!")
    assert n.formula().ref_errors()
    assert not F.DefinedName("WACC_rate_IT", None, "'1 - DCF'!$F$27").formula().ref_errors()


def test_names_29_t4_lambda_called_like_a_function():
    lam = ("_xlfn.LAMBDA(_xlpm.CF,_xlpm.WACC,_xlpm.numberofperiodsfirststage,_xlpm.growthratefirststage,"
           "_xlpm.growthratforperpetuity,((_xlpm.CF*(1+_xlpm.growthratefirststage)*(1-(((1+_xlpm.growthratefirststage)"
           "^_xlpm.numberofperiodsfirststage))/((1+_xlpm.WACC)^_xlpm.numberofperiodsfirststage)))/(_xlpm.WACC-"
           "_xlpm.growthratefirststage))+(_xlpm.CF*((1+_xlpm.growthratefirststage)^_xlpm.numberofperiodsfirststage)*"
           "(1+_xlpm.growthratforperpetuity))/((_xlpm.WACC-_xlpm.growthratforperpetuity)*((1+_xlpm.WACC)^"
           "_xlpm.numberofperiodsfirststage)))")
    names = [("F_twostageTV", None, lam, False), ("F_twostageTV_v1", None, lam, False)]
    cell = "F_twostageTV($K$30,$H$44,($H$8-'Solution Model'!$K$2),$H$7,$H$9)"
    assert _used([("Solution Model", cell)], names) == {"F_twostageTV"}
    tab = F.NameTable(names)
    assert tab.get("F_twostageTV").is_lambda
    # the LAMBDA's own parameters (_xlpm.WACC) are not the defined names WACC
    lam_f = F.parse(lam)
    assert not lam_f.names_referenced() and "wacc" in lam_f.params
    # 80/T2 HouseorFlat: visible LAMBDA name L_TAX_TaxtoPay called inside MAP(LAMBDA(...))
    t = ('IF(_xlfn.ANCHORARRAY($H$6)="Retirement",0,_xlfn.MAP(_xlfn.ANCHORARRAY($I$6),_xlfn.ANCHORARRAY($S$6),'
         '_xlfn.LAMBDA(_xlpm.S,_xlpm.I,L_TAX_TaxtoPay(_xlpm.S,Assumptions!$C$40:$C$44,Assumptions!$D$40:$D$44))))')
    assert _used([("Solution Model - Owning (F)", t)], [("L_TAX_TaxtoPay", None, "_xlfn.LAMBDA(_xlpm.x,_xlpm.x)", False)]) \
        == {"L_TAX_TaxtoPay"}


def test_names_29_t5_used_only_by_data_validation():
    names = [("ScenarioList", None, "Summary!$D$8:$D$10", False)]
    assert _used([("Summary", "ScenarioList")], names) == {"ScenarioList"}        # <dataValidation><formula1>
    assert _used([("Summary", "Assumption!$D$15:$F$15")], names) == set()


def test_names_29_t6_orphan_among_real_formulas():
    names = [("MonthsperYear", None, "'Assumptions - ABC'!$D$61", False), ("mtok", None, "'Assumptions - ABC'!$D$62", False),
             ("ModelLength", None, "'Assumptions - ABC'!$D$60", False)]
    cells = [("3-Statement - ABC", "MAX(S102-IF(T89,'Assumptions - ABC'!$E$193/MonthsperYear*mtok,0),0)"),
             ("3-Statement - ABC", "'Assumptions - ABC'!$E$6*mtok*(1+'Assumptions - ABC'!$E$7)^(LY$10-1)")]
    assert _used(cells, names) == {"MonthsperYear", "mtok"}


def test_names_scope_prefix_shadowing_and_collisions():
    names = [("Rate", None, "Inputs!$B$2", False), ("Rate", "Calc", "Calc!$B$9", False), ("RateHigh", None, "Inputs!$B$3", False)]
    tab = F.NameTable(names)
    assert tab.resolve("rate", "Calc").scope == "Calc"                      # sheet-local beats global
    assert tab.resolve("Rate", "Other").scope is None
    assert tab.resolve("Rate", None, qualifier_sheet="Calc").scope == "Calc"
    assert tab.resolve("Rate", "Calc", workbook_scoped=True).scope is None
    assert [n.scope for n in F.names_used_by("Rate*2", "Calc", tab)] == ["Calc"]
    assert [n.scope for n in F.names_used_by("[0]!Rate*2", "Calc", tab)] == [None]
    assert [n.scope for n in F.names_used_by("Calc!Rate*2", "Other", tab)] == ["Calc"]
    assert [n.name for n in F.names_used_by("RateHigh*2", "Other", tab)] == ["RateHigh"]       # prefix trap
    assert F.names_used_by("[1]!Rate", "Calc", tab) == []                    # external name
    assert F.names_used_by('"Rate"&A1', "Calc", tab) == []                   # string literal
    assert F.names_used_by("LET(Rate,2,Rate*A1)", "Other", tab) == []        # LET shadowing
    assert F.names_used_by("_xlfn.LET(_xlpm.Rate,2,_xlpm.Rate*A1)", "Other", tab) == []
    # built-in function names called as functions are the functions, not the names
    tab2 = F.NameTable([("Year", None, "Inputs!$B$1", False), ("Growth", None, "Inputs!$B$2", False)])
    assert F.names_used_by("YEAR(A1)+GROWTH(B1:B5)", "Calc", tab2) == []
    assert [n.name for n in F.names_used_by("Year+1", "Calc", tab2)] == ["Year"]
    # INDIRECT("Rate") / HYPERLINK("#Rate") literals only when asked
    assert F.names_used_by('INDIRECT("RateHigh")', "Calc", tab) == []
    assert [n.name for n in F.names_used_by('INDIRECT("RateHigh")', "Calc", tab, include_indirect_literals=True)] == ["RateHigh"]
    assert [n.name for n in F.names_used_by('HYPERLINK("#RateHigh","go")', "Calc", tab, include_indirect_literals=True)] == ["RateHigh"]
    assert [n.name for n in F.names_used_by('INDIRECT("Rate"&"High")', "Calc", tab, include_indirect_literals=True)] == ["RateHigh"]
    # range of two names
    tab3 = F.NameTable([("StartN", None, "Data!$A$2", False), ("EndN", None, "Data!$A$9", False)])
    assert [n.name for n in F.names_used_by("SUM(StartN:EndN)", "Calc", tab3)] == ["StartN", "EndN"]
    # names used by other names (transitive use) and through a hidden helper
    tab4 = F.NameTable([("Visible", None, "Helper*2", False), ("Helper", None, "Inputs!$A$1", True)])
    assert [n.name for n in F.expand("Visible", "Calc", tab4).names] == ["Visible", "Helper"]
    # Lim1 / R2C3 defined as names can never be referenced: the tokens are cells
    tab5 = F.NameTable([("Lim1", None, "Inputs!$A$1", False), ("R2C3", None, "Inputs!$A$2", False)])
    assert F.names_used_by("Lim1+R2C3", "Calc", tab5) == []
    assert tab5.get("Lim1").validity_problem and tab5.get("R2C3").validity_problem


def test_names_referenced_syntactic():
    refs = F.names_referenced("Rate+Sheet1!Tax+[0]!G+[1]!Ext+MyFn(1)+SUM(A1)+'My Sheet'!Local")
    assert [(r.name, r.sheet, r.external, r.workbook_scoped, r.as_call) for r in refs] == [
        ("Rate", None, None, False, False), ("Tax", "Sheet1", None, False, False), ("G", None, None, True, False),
        ("Ext", None, "1", False, False), ("MyFn", None, None, False, True), ("Local", "My Sheet", None, False, False)]


# --------------------------------------------------------------------------- shared formulas
def test_translate_basic_and_absolute():
    assert F.translate("A1+$B$2+C$3+$D4", "A1", "B3") == "B3+$B$2+D$3+$D6"
    assert F.translate("=SUM(A1:B2)", "C3", "C4") == "SUM(A2:B3)"
    # 49/T2 LargeBalance: ABC171 is a child of the master at AAK171
    assert F.translate("AAK$169+AAK$170", "AAK171", "ABC171") == "ABC$169+ABC$170"
    # real masters from the toys
    assert F.translate("SUM(_xlfn.ANCHORARRAY(H$24))", "H96", "BS96") == "SUM(_xlfn.ANCHORARRAY(BS$24))"
    assert F.translate("I$147-SUM(I$105:I$109,I$112:I$118)", "I104", "BT104") == "BT$147-SUM(BT$105:BT$109,BT$112:BT$118)"
    assert F.translate("COUNTIFS(_xlfn.ANCHORARRAY($I$11),TRUE,_xlfn.ANCHORARRAY($I$4),I$4,_xlfn.ANCHORARRAY($I$3),I$3)",
                       "I12", "K12") == \
        "COUNTIFS(_xlfn.ANCHORARRAY($I$11),TRUE,_xlfn.ANCHORARRAY($I$4),K$4,_xlfn.ANCHORARRAY($I$3),K$3)"
    assert F.translate("SUM($AL7:$AN7)", "AO7", "AO70") == "SUM($AL70:$AN70)"
    assert F.translate("SUM(B10:E10)", "F10", "F16") == "SUM(B16:E16)"


def test_translate_hard_cases():
    assert F.translate("SUM(A:A)+SUM($B:B)", "C1", "E9") == "SUM(C:C)+SUM($B:D)"           # whole columns
    assert F.translate("SUM(1:1)+SUM($2:2)", "C1", "E9") == "SUM(9:9)+SUM($2:10)"          # whole rows
    assert F.translate("'My Sheet'!A1+Other!$A1", "B2", "C3") == "'My Sheet'!B2+Other!$A2"   # cross-sheet
    assert F.translate("[1]Sheet1!A1*'[1]Data 2'!B$2", "A1", "B2") == "[1]Sheet1!B2*'[1]Data 2'!C$2"   # external
    assert F.translate("SUM(Table1[Col])+[@Qty]*A1", "A1", "A2") == "SUM(Table1[Col])+[@Qty]*A2"       # structured
    assert F.translate("SUM(A1#)", "B1", "B2") == "SUM(A2#)"                                 # spill
    assert F.translate("SUM(A:.A)", "B1", "C1") == "SUM(B:.B)"                               # trimmed
    assert F.translate("A1:OFFSET(A1,1,1)", "B1", "B2") == "A2:OFFSET(A2,1,1)"               # glued function
    assert F.translate('"A1"&A1&Rate&Lim1', "A1", "A2") == '"A1"&A2&Rate&Lim2'               # strings/names kept
    assert F.translate("SUM(\r\nA1,\tB1)", "A1", "A2") == "SUM(\r\nA2,\tB2)"                 # whitespace kept
    assert F.translate("Sheet1!A1:Sheet1!B5", "A1", "A2") == "Sheet1!A2:Sheet1!B6"
    assert F.translate("A1", "B2", "A1") == "#REF!"                                          # off the grid
    assert F.translate("Sheet1!A1+1", "B2", "B1") == "Sheet1!#REF!+1"
    assert F.translate("XFD1", "A1", "B1") == "#REF!"
    assert F.translate("A1048576", "A1", "A2") == "#REF!"
    assert F.translate("#REF!+A1", "A1", "A2") == "#REF!+A2"
    assert F.translate("A1", "A1", "A1") == "A1"
    raises(F.translate, "A1", "Rate", "A2")


def test_translate_matches_openpyxl_where_it_works():
    samples = ["SUM(A1:B2)*$C$3", "IF(A1>0,B$1,$C2)", "VLOOKUP(A1,Sheet2!$A$1:$C$10,2,FALSE)", "SUM(A:A)+1:1",
               "'My Sheet'!A1+B1", "INDEX($A$1:$A$9,ROW()-1)", "AAK$169+AAK$170", "SUMPRODUCT((A1:A5>0)*B1:B5)",
               "[1]Data!A1+A2", "ROUND(A1/B1,2)&\"%\""]
    for s in samples:
        for to in ("D7", "A1", "Z99", "AA3"):
            exp = Translator("=" + s, origin="A1").translate_formula(to)[1:]
            assert F.translate(s, "A1", to) == exp, (s, to, exp, F.translate(s, "A1", to))


# --------------------------------------------------------------------------- differential vs openpyxl
_REAL_FORMULAS = [
    # one or more per toy family (copied from the toy workbooks)
    "_xlfn.XLOOKUP($A5,'Budget - Income Statement'!$A$9:$A$29,'Budget - Income Statement'!$B$9:$B$29)",
    "IF(BTT$13,-SUMIFS($H$17:$FLV$17,_xlfn.ANCHORARRAY($H$4),BTT$4,_xlfn.ANCHORARRAY($H$3),BTT$3)*Assumptions!$E$17,0)",
    "Z14*IF(_xlfn.XLOOKUP($D14,_xlfn._TRO_TRAILING(Assumption!$C:$C),_xlfn._TRO_TRAILING(Assumption!$F:$F))=\"Sales\",1,2)",
    'SUMIFS(Assumption!$F:$F,Assumption!$C:$C,"Total Debt",Assumption!$E:$E,"Whole company")',
    'INDEX($A:$A,2)&" - notes"', '"Mix "&"1:2"', "MEDIAN(Peer_EV)", "[1]Prices!$C$5", "'[1]Annual Summary'!$G$41",
    "[1]Rates!$B$2*C25", "'[1]P&L Budget \u2013 GL'!$O$10", "[1]!ScenarioList", "OFFSET(Assumptions!$D$6,0,0)&\" Valuation\"",
    "_xlfn.STDEV.P(OFFSET($K$16,0,0,COUNTA($C$16:$C$37),1))", "TODAY()", "RANDBETWEEN(1,100)",
    'MID(CELL("filename",A1),FIND("]",CELL("filename",A1))+1,255)',
    "($AL600+$AM600)*((1+INDIRECT(\"'Assumptions'!D80\"))^(1/Assumptions!$D$8))-($AL600+$AM600)",
    "F_twostageTV($K$30,$H$44,($H$8-'Solution Model'!$K$2),$H$7,$H$9)", "I$8*(1-Tax_rate)",
    "ROUND(INDEX('Monthly Model'!$H$60:$CY$174,_xlfn.XMATCH(_xlfn.TEXTBEFORE(_xlfn.TEXTAFTER(Questions!$A36,\"for/of \"),\" as \"),"
    "_xlfn.BYROW('Monthly Model'!$C$60:$D$174,_xleta.CONCAT)),_xlfn.XMATCH(SUBSTITUTE(_xlfn.TEXTAFTER($A36,\" of \",-1),\"?\",\"\"),"
    "_xlfn.ANCHORARRAY('Monthly Model'!$H$2)&\"/\"&_xlfn.ANCHORARRAY('Monthly Model'!$H$3))),2)",
    "_xlfn.LET(_xlpm.T,_xlfn.TOROW('1_Historical_Data'!$B$5:$B$40),MONTH(_xlpm.T)&\"/\"&YEAR(_xlpm.T))",
    "_xlfn.XLOOKUP(_xlfn.ANCHORARRAY($C$17),Comp!$A$5:$A$19,Comp!$E$5:$E$19)/_xlfn.XLOOKUP(\"USD/\"&_xlfn.ANCHORARRAY($D$17),"
    "Assumptions!$C$13:$C$18,Assumptions!$D$13:$D$18)",
    "_xlfn.BYCOL($H$11:$DW$79,_xleta.SUM)*Inflation*Assumptions!$F$55",
    "'Monthly Model'!$G$66+'Monthly Model'!$G$85", "IFERROR(_xlfn.ANCHORARRAY($G$5)-$G$6:$AP$6,\"n/a\")",
]


def test_differential_against_openpyxl_tokenizer():
    """Function names and operand texts agree with openpyxl's Tokenizer on formulas it can
    lex (it gets spill '#', TAB/CR and 'A1:FUNC(' glue wrong; those are excluded/adjusted).
    The same comparison was run over all 1,623,195 distinct formulas of the toy workbooks
    (detchecks/scratch/diff_corpus.py): 0 differences."""
    for text in _REAL_FORMULAS:
        items = Tokenizer("=" + text).items
        theirs_f = []
        theirs_o = []
        for t in items:
            if t.type == "FUNC" and t.subtype == "OPEN" and t.value != "(":
                name = t.value[:-1]
                if ":" in name:
                    theirs_o.append(name.rsplit(":", 1)[0])
                    name = name.rsplit(":", 1)[1]
                theirs_f.append(F._norm_func(name)[0])
            elif t.type == "OPERAND" and t.subtype == "RANGE":
                if t.value.lower().startswith("_xleta."):
                    theirs_f.append(F._norm_func(t.value)[0])
                elif t.value.upper() not in ("TRUE", "FALSE"):
                    theirs_o.append(t.value)
        f = F.parse(text)
        assert sorted(theirs_f) == sorted(c.name for c in f.functions), text
        mine_o = [o.raw for o in f.operands if o.kind not in ("number", "string", "bool", "array")
                  and not (o.kind == "error" and o.qualifier is None)]
        assert sorted(theirs_o) == sorted(mine_o), (text, theirs_o, mine_o)



# --------------------------------------------------------------------------- review regressions (2026-10-02)
def test_review_f1_let_name_is_bound_after_its_value():
    tab = F.NameTable([("WACC", None, "Inputs!$B$2", False)])
    f = F.parse("LET(wacc,WACC,A1/wacc)")
    assert [(o.kind, o.raw) for o in f.operands] == [("param", "wacc"), ("name", "WACC"), ("cell", "A1"),
                                                     ("param", "wacc")]
    assert [n.name for n in F.names_used_by("LET(wacc,WACC,A1/wacc)", "S", tab)] == ["WACC"]   # check 29
    f = F.parse("_xlfn.LET(_xlpm.today,TODAY(),_xlpm.today+1)")
    c = f.calls("TODAY")[0]
    assert c.builtin and not c.local and f.function_names() == {"LET", "TODAY"}
    assert F.expand(f, "S", tab).functions & F.VOLATILE_FUNCTIONS == {"TODAY"}            # check 80
    # plain declarations: the value of a LATER name sees an earlier one; its own value does not
    f = F.parse("LET(x,x,y,x+1,y*x)")
    assert [o.kind for o in f.operands] == ["param", "name", "param", "param", "number", "param", "param"]
    # LAMBDA parameters live in the body only (declarations are params themselves)
    f = F.parse("LAMBDA(a,b,a+b)")
    assert [o.kind for o in f.operands] == ["param", "param", "param", "param"]
    # nested: the inner value is inside the outer scope
    f = F.parse("LET(x,1,LET(x,x+1,x))")
    assert [o.kind for o in f.operands if o.raw == "x"] == ["param", "param", "param", "param"]
    assert f.params == frozenset({"x"})


def test_review_f2_xlpm_declared_params_bind_only_xlpm_tokens():
    tab = F.NameTable([("Rate", None, "Inputs!$B$2", False)])
    t = "_xlfn.LET(_xlpm.rate,0.05,_xlpm.rate*Rate)"
    assert [(o.kind, o.raw) for o in F.parse(t).operands] == [
        ("param", "_xlpm.rate"), ("number", "0.05"), ("param", "_xlpm.rate"), ("name", "Rate")]
    assert [n.name for n in F.names_used_by(t, "S", tab)] == ["Rate"]
    f = F.parse("_xlfn.LET(_xlpm.offset,2,OFFSET(A1,_xlpm.offset,0))")
    off = f.calls("OFFSET")[0]
    assert off.builtin and not off.local and "OFFSET" in f.function_names()
    assert F.expand(f, "S", tab).functions & F.VOLATILE_FUNCTIONS == {"OFFSET"}
    # a plain call inside an _xlpm. scope is the defined (LAMBDA) name, the _xlpm. call is local
    f = F.parse("_xlfn.LET(_xlpm.f,_xlfn.LAMBDA(_xlpm.a,_xlpm.a*2),_xlpm.f(3)+f(4))")
    assert [(c.raw, c.local) for c in f.functions if c.name == "F"] == [("_xlpm.f", True), ("f", False)]
    assert [r.name for r in f.names_referenced()] == ["f"]
    assert f.params == frozenset({"f", "a"})


def test_review_f3_quoted_sheet_with_double_quote_keeps_prefilters_conservative():
    t1 = "SUM('Q\"1'!A:A)+LEN(\"x\")"
    t2 = "'Q\"1'!A1+INDIRECT(\"B\"&\"2\")"
    assert [o.raw for o in F.parse(t1).whole_column_or_row_refs()] == ["'Q\"1'!A:A"]
    assert F.quick_may_have_whole_refs(t1)
    assert F.quick_may_call(t2, ["INDIRECT"])
    assert F.mask_strings(t2) == "'Q\"1'!A1+INDIRECT(\" \"&\" \")"
    # strings holding quotes / brackets are still blanked; brackets keep their text
    assert F.mask_strings("\"it's [x]\"&T[a\"b]") == "\"        \"&T[a\"b]"
    assert not F.quick_may_call("\"it's OFFSET\"&'Sheet''1'!A1", ["OFFSET"])


def test_review_f5_structured_refs_need_the_table_sheet_map():
    raises(F.references_other_sheet, "SUM(SalesTbl[Amount])", "Summary")              # no guess
    tables = {"SalesTbl": "Data", "LocalTbl": "Summary"}
    assert F.references_other_sheet("SUM(SalesTbl[Amount])", "Summary", table_sheets=tables)
    assert F.parse("SUM(salestbl[Amount])").other_sheets("Summary", table_sheets=tables) == {"Data"}
    assert not F.references_other_sheet("SUM(LocalTbl[Amount])", "Summary", table_sheets=tables)
    assert not F.references_other_sheet("[@Amount]*2", "Summary")                    # own table: no map needed
    raises(F.references_other_sheet, "SUM(Missing[Amount])", "Summary", table_sheets=tables)
    assert F.references_other_sheet("SalesTbl[Amount]", "Summary", table_sheets=[("SalesTbl", "Data")])
    # through a defined name
    names = [("Sales", None, "SalesTbl[Amount]", False)]
    assert F.references_other_sheet("SUM(Sales)", "Summary", names=names, table_sheets=tables)
    raises(F.references_other_sheet, "SUM(Sales)", "Summary", names=names)
    raises(F.references_other_sheet, "SalesTbl[Amount]", "Summary", table_sheets={"SalesTbl": 3})


def test_review_f6_name_table_validates_inputs():
    assert F.NameTable([("Rate", None, "Inputs!$B$2", "0")]).get("Rate").hidden is False
    assert F.NameTable([("Rate", None, "Inputs!$B$2", "1")]).get("Rate").hidden is True
    assert F.NameTable([("Rate", None, "Inputs!$B$2", "true")]).get("Rate").hidden is True
    assert F.NameTable([("Rate", None, "Inputs!$B$2", None)]).get("Rate").hidden is False
    assert F.NameTable([("Rate", None, "=Inputs!$B$2")]).get("Rate").text == "Inputs!$B$2"
    raises(F.NameTable, [("Rate", None, "Inputs!$B$2", "yes")])
    raises(F.NameTable, [("Rate", 0, "X!$A$1", False)])                    # localSheetId, not a sheet name
    raises(F.NameTable, [("Rate", 2, "X!$A$1", False)])
    raises(F.NameTable, [("Rate", "", "X!$A$1", False)])
    raises(F.NameTable, [("Rate", None)])
    raises(F.NameTable, [42])
    raises(F.NameTable, "Rate")
    # the reader's own DefinedName objects are accepted
    from detchecks.core.package import DefinedName as PkgName
    tab = F.NameTable([PkgName("Rate", "Inputs!$B$2", None, None, False, False),
                       PkgName("Local", "Calc!$A$1", 1, "Calc", True, False)])
    assert tab.get("Rate").text == "Inputs!$B$2" and tab.get("Local", "Calc").hidden
    assert [n.name for n in F.names_used_by("Rate*Local", "Calc", tab)] == ["Rate", "Local"]
    bad = PkgName("Odd", "X!$A$1", 9, None, False, False, scope_error="localSheetId 9 names no sheet")
    raises(F.NameTable, [bad], exc=GradingError)                        # the reader's own loud error
    # a rebuilt table keeps the same DefinedName objects
    t1 = F.NameTable([("Rate", None, "X!$A$1", False)])
    assert F._table(t1, ["X"]).names[0] is t1.names[0]


def test_review_f9_odd_references():
    raises(F.parse, "#REF!A1")                                           # toy 22/T4: Excel rejects the token
    raises(F.parse, "SUM(#REF!A:A)")
    raises(F.parse, '"a"B1')
    raises(F.parse, "(A1)B1")
    raises(F.parse, "A:1")                                               # a row number needs a row partner
    assert F.parse("_xlfn.LAMBDA(_xlpm.r,_xlpm.r*2)(3)").functions[0].name == "LAMBDA"   # ')(' is legal
    assert F.parse("E79-#REF!").ref_errors() and F.parse("Sheet1!#REF!").ref_errors()
    o = F.parse("SUM(A:A:B5)").operands[0]
    assert (o.kind, o.shape, o.bounds) == ("whole_column", "column", (1, 1, F.MAX_ROW, 2))
    assert whole("SUM(A:A:B5)") == ["A:A:B5"]
    o = F.parse("A:XFD").operands[0]
    assert (o.kind, o.shape) == ("whole_column", "sheet")
    assert (F.parse("1:1048576").operands[0].kind, F.parse("A1:XFD1048576").operands[0].kind) == ("whole_row", "whole_row")
    # an unpaired column-shaped endpoint is a defined name (B and Tax are valid names)
    o = F.parse("A1:B").operands[0]
    assert o.kind == "range" and o.names == ("B",)
    tab = F.NameTable([("Tax", None, "X!$A$1", False), ("Rate", None, "X!$A$2", False)])
    assert [n.name for n in F.names_used_by("SUM(Tax:Rate)", "S", tab)] == ["Tax", "Rate"]
    assert F.shift("SUM(Tax:Rate)", 1, 1) == "SUM(Tax:Rate)"
    assert F.shift("SUM(A1:B)", 1, 1) == "SUM(B2:B)"
    assert whole("Tax:Rev") == ["Tax:Rev"]                               # both are columns: TAX:REV
    assert F.shift("SUM(Tax:Rev)", 0, 1) == "SUM(TAY:REW)"

# --------------------------------------------------------------------------- runner
def main() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:                      # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
