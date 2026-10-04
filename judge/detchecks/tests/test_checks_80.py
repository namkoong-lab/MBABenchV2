"""Regression tests for Avoid volatile functions (80), second review (2026-10-04).

The first-round tests live in test_checks_formulascan.py (test_80_*); this module reuses its
micro-workbook builder.

    cd /Users/patrick/MBABench-deterministic-checks
    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_checks_80
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback

from detchecks.checks.c80 import stamp_function
from detchecks.tests import test_checks_formulascan as T

book, sheet, c, cf, dv, run, expect, raises, locs = T.book, T.sheet, T.c, T.cf, T.dv, T.run, T.expect, T.raises, T.locs


# ------------------------------------------------------------------ finding 80-avoidable-gradingerror
def test_80_undecidable_site_when_fail_certain():
    """An undecidable site raises only when the verdict depends on it (review er4/er5)."""
    bad_scope = ("Dyn", 'INDIRECT("S!Z1")', {"localSheetId": "7"})
    # (a) an unused volatile name with a broken localSheetId, while a cell already calls OFFSET
    p = book("u_er4.xlsx", [("S", sheet([c("A1", "OFFSET(A2,1,0)")]))], names=[bad_scope])
    v = run(p, 80)
    expect(v, "fail", ["S!A1"])
    assert len(v["stats"]["volatile_names_use_undecidable"]) == 1, v["stats"]
    assert "undecidable site" in v["summary"], v["summary"]
    # ... and alone it still raises (the verdict depends on it)
    p = book("u_er4b.xlsx", [("S", sheet([c("A1", "1+1")]))], names=[bad_scope])
    raises(lambda: run(p, 80), "cannot tell whether", "localSheetId")
    # (b) a stamp reference with a dynamic name endpoint, while another cell =RAND() already fails
    end = [("End", "INDEX(Cover!$A$1:$A$10,5)", None)]
    p = book("u_er5.xlsx", [("Cover", sheet([c("A1", "SUM(A2:End)"), c("B1", "RAND()"), c("C6", "TODAY()")]))],
             names=end)
    v = run(p, 80)
    expect(v, "fail", ["Cover!B1"])
    assert v["stats"]["date_stamps_undecided"] == ["Cover!C6"] and v["stats"]["n_stamp_refs_unplaced"] == 1, v["stats"]
    # ... a stamp that another formula references is a mistake whatever the unplaced reference covers
    p = book("u_er5b.xlsx", [("Cover", sheet([c("A1", "SUM(A2:End)"), c("B1", "C6+1"), c("C6", "TODAY()")]))],
             names=end)
    v = run(p, 80)
    expect(v, "fail", ["Cover!C6"])
    assert v["stats"]["date_stamps_undecided"] == [], v["stats"]
    # ... and alone it still raises
    p = book("u_er5c.xlsx", [("Cover", sheet([c("A1", "SUM(A2:End)"), c("C6", "TODAY()")]))], names=end)
    raises(lambda: run(p, 80), "cannot place reference", "not one fixed reference")


# ------------------------------------------------------------------ finding 80-stamp-unary-plus-not-alone
def test_80_stamp_noop_wrappers():
    """=+TODAY(), =(NOW()), =@TODAY() are TODAY()/NOW() alone (review po3)."""
    for t in ["+TODAY()", "(NOW())", "@TODAY()", "+(+TODAY())", " ( ( NOW( ) ) ) ", "=+NOW()"]:
        assert stamp_function(t) is not None, t
    for t in ["-TODAY()", "+TODAY()+0", "(TODAY())*1", "((TODAY())", "(TODAY()))", "TODAY()()", "+-TODAY()",
              "TEXT(TODAY(),\"d\")"]:
        assert stamp_function(t) is None, t
    p = book("w_ok.xlsx", [("Cover", sheet([c("C6", "+TODAY()"), c("C7", "(NOW())"), c("C8", "@TODAY()")]))])
    v = run(p, 80)
    expect(v, "pass", [])
    assert v["stats"]["date_stamps_exempt"] == ["Cover!C6", "Cover!C7", "Cover!C8"], v["stats"]
    # referenced: still a mistake; not alone: an ordinary volatile call
    p = book("w_ref.xlsx", [("Cover", sheet([c("C6", "+TODAY()"), c("D6", "C6+30")]))])
    v = run(p, 80)
    expect(v, "fail", ["Cover!C6"])
    assert "referenced by" in v["mistakes"][0]["description"]
    for i, t in enumerate(["-TODAY()", "+TODAY()+0", "(NOW())*1"]):
        p = book(f"w_not_{i}.xlsx", [("Cover", sheet([c("C6", t)]))])
        v = run(p, 80)
        expect(v, "fail", ["Cover!C6"])
        assert "(volatile)" in v["mistakes"][0]["description"], (t, v["mistakes"])


# ------------------------------------------------------------------ finding 80-space-before-paren
def test_80_space_before_paren_undecidable():
    """'OFFSET (A1,1,1)' is not a call to the parser; how Excel reads it is not measured: no
    guess (review fp2/fp2b; core parser change deferred, question 11 in docs/checks/80.md)."""
    for i, t in enumerate(["OFFSET (A1,1,1)", "RANDBETWEEN\n(1,9)", "SUM(B1, NOW\t())", "_xlfn.RANDBETWEEN (1,9)",
                           "offset (a1,1,1)"]):
        p = book(f"sp_{i}.xlsx", [("S", sheet([c("A1", t)]))])
        raises(lambda: run(p, 80), "cannot tell whether S!A1 calls", "whitespace before '('")
    # in a CF rule
    p = book("sp_cf.xlsx", [("S", sheet([c("A1", v=1)], tail=cf("A1:A9", "OFFSET ($A$1,0,0)>1")))])
    raises(lambda: run(p, 80), "cannot tell whether Conditional format on S!A1:A9 calls OFFSET")
    # another mistake already fails the file: recorded, not raised
    p = book("sp_fail.xlsx", [("S", sheet([c("A1", "OFFSET (A1,1,1)"), c("A2", "RAND()")]))])
    v = run(p, 80)
    expect(v, "fail", ["S!A2"])
    assert v["stats"]["n_space_calls_undecided"] == 1 and "S!A1" in v["stats"]["space_calls_undecided"][0], v["stats"]
    # not a bare function name: sheet-qualified, a table column, a sheet called 'Now (2)', a column NOW
    p = book("sp_not.xlsx", [("S", sheet([c("A1", "S!OFFSET (A2)"), c("A2", "SUM(Tbl[Today (x)])"),
                                          c("A3", "'Now (2)'!A1+1"), c("A4", "SUM(NOW:NOW (A1:B2))"),
                                          c("A5", 'LEN("OFFSET (A1)")')])),
                             ("Now (2)", sheet([c("A1", v=1)]))])
    v = run(p, 80)
    expect(v, "pass", [])
    assert v["stats"]["n_space_calls_undecided"] == 0, v["stats"]
    # a defined name: unused -> does not decide (stats); used -> undecidable
    nm = [("Dyn", "OFFSET (S!$A$1,0,0,3,1)", None)]
    p = book("sp_name_unused.xlsx", [("S", sheet([c("A1", "1+1")]))], names=nm)
    v = run(p, 80)
    expect(v, "pass", [])
    assert [x.split("=")[0] for x in v["stats"]["space_call_names_unused"]] == ["Dyn"], v["stats"]
    p = book("sp_name_used.xlsx", [("S", sheet([c("A1", "SUM(Dyn)")]))], names=nm)
    raises(lambda: run(p, 80), "cannot tell whether Name Manager: Dyn calls OFFSET", "S!A1")
    # a 'TODAY ()' stamp: unreferenced it passes whatever Excel reads; referenced it is undecidable
    p = book("sp_stamp.xlsx", [("Cover", sheet([c("C6", "TODAY ()")]))])
    v = run(p, 80)
    expect(v, "pass", [])
    assert v["stats"]["date_stamps_exempt"] == ["Cover!C6"], v["stats"]
    p = book("sp_stamp_ref.xlsx", [("Cover", sheet([c("C6", "TODAY ()"), c("D6", "C6+1")]))])
    raises(lambda: run(p, 80), "cannot tell whether Cover!C6", "referenced by 1 formula site")
    p = book("sp_stamp_ref2.xlsx", [("Cover", sheet([c("C6", "TODAY ()"), c("D6", "C6+1"), c("E6", "NOW()+1")]))])
    v = run(p, 80)
    expect(v, "fail", ["Cover!E6"])
    assert v["stats"]["date_stamps_undecided"] == ["Cover!C6"], v["stats"]


TESTS = [test_80_undecidable_site_when_fail_certain, test_80_stamp_noop_wrappers, test_80_space_before_paren_undecidable]


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
        if T._TMP and os.path.isdir(T._TMP):
            shutil.rmtree(T._TMP, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
