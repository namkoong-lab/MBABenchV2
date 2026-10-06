"""Regression tests for Red font for external links (51): second review, 2026-10-04.

    cd /Users/patrick/MBABench-deterministic-checks
    python3 heavy_run.py -- python -m detchecks.tests.test_checks_51

Builders and the shared colour constants come from test_checks_colours (same synthetic
workbook format).  Findings covered:
  51-R1  a cell that certainly reads another workbook (directly or through a normal defined
         name) is judged, also when it ALSO uses a defined name whose scope is unknown;
  51-R2  a structured reference / bare table name whose table is not defined or whose table
         parts cannot be read never raises in 51 (a table without [n]! is this workbook's).
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
import zipfile

from detchecks.api import grade
from detchecks.checks import colour_rules as CR
from detchecks.core.package import Package
from detchecks.tests import test_checks_colours as TC
from detchecks.tests.test_checks_colours import BADTHEME, BLACK, BLUE, GREEN, GREY50, RED, K49, K50, K51, book, c, graded, locs, raises, run, sheet

EXT = "[1]Prices!$A$2"
INPUTS = ("Inputs", sheet(), None)


def _rezip(path: str, *, drop: tuple = (), replace: dict | None = None) -> str:
    """Copy of the workbook zip without the `drop` entries and with `replace` {name: bytes}."""
    replace = replace or {}
    out = path[:-5] + "_mod.xlsx"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            dst.writestr(info.filename, replace.get(info.filename, src.read(info.filename)))
    return out


# ------------------------------------------------------------------ 51-R1: EXTERNAL is certain
def test_51_external_certain_despite_unknown_scope_name():
    """=GoodExt+BadExt: GoodExt (global, [1]Prices!$A$1) makes the cell read another workbook
    whatever BadExt (localSheetId naming no sheet) turns out to be: fail in black, pass in red,
    and an unresolvable colour raises (the colour decides).  A cell whose only possible reading
    of another workbook goes through the unknown-scope name still raises."""
    names = [("GoodExt", "[1]Prices!$A$1", None), ("BadExt", "[1]Prices!$B$1", 7)]
    data = sheet((1, [c("A1", BLACK, "GoodExt+BadExt"), c("B1", RED, "GoodExt+BadExt"), c("C1", GREEN, "BadExt+GoodExt*2"),
                      c("D1", BLACK, "BadExt*2+[1]Prices!A2"), c("E1", RED, "BadExt*2")]))
    p = book("51_r1.xlsx", [("Calc", data, None), INPUTS], names=names, ext_links=1)
    v = run(p, 51)
    assert sorted(locs(v)) == ["Calc!A1", "Calc!C1", "Calc!D1"], locs(v)
    assert v["stats"]["classified_by_class"]["external"] == 3, v["stats"]
    # the unknown-scope name alone still cannot be decided (unchanged)
    p = book("51_r1_raise.xlsx", [("Calc", sheet((1, [c("A1", BLACK, "BadExt*2")])), None), INPUTS], names=names, ext_links=1)
    raises(lambda: run(p, 51), "may read another workbook", "cannot be decided")
    # the colour of a certain external cell decides, so an unresolvable one raises even with BadExt
    p = book("51_r1_colour.xlsx", [("Calc", sheet((1, [c("A1", BADTHEME, "GoodExt+BadExt")])), None), INPUTS],
             names=names, ext_links=1)
    v = graded("unresolved_colour", lambda: run(p, 51))         # read as black: an external link not in red
    assert v["decision"] == "fail", v
    # classifier contract: the kind is EXTERNAL and the doubt is recorded
    pkg = Package.open(book("51_r1_clf.xlsx", [("Calc", sheet(), None), INPUTS], names=names, ext_links=1))
    try:
        c_ = CR.Classifier(pkg).classify("GoodExt+BadExt", "Calc")
        assert c_.kind == CR.EXTERNAL and c_.unsure_ext and not c_.direct_ext, c_
    finally:
        pkg.close()


# ------------------------------------------------------------------ 51-R2: tables never matter
def _table_books():
    """(label, path, expected 51 locations) for the table-lookup shapes of the second review."""
    out = []
    calc = sheet((1, [c("A1", BLACK, "SUM(Tbl[Col])*2"), c("B1", BLUE, "Tbl[Col]"), c("C1", GREY50, "SUM(Tbl)")]))
    out.append(("undefined table", book("51_r2_undef.xlsx", [("Calc", calc, None), ("Data", sheet(), None)]), []))
    calc_ext = sheet((1, [c("A1", BLACK, "[1]Prices!A1+SUM(Tbl[Col])"), c("B1", RED, "[1]Prices!A1+SUM(Tbl[Col])"),
                          c("C1", BLACK, "SUM(Tbl[Col])*2")]))
    out.append(("undefined table + direct external", book("51_r2_undef_ext.xlsx", [("Calc", calc_ext, None)], ext_links=1),
                ["Calc!A1"]))
    defined = book("51_r2_defined.xlsx", [("Calc", calc, None), ("Data", sheet(), None)], tables={1: [("Tbl", "A1:A4", "Col")]})
    out.append(("defined table", defined, []))
    out.append(("tablePart relationship to a missing part", _rezip(defined, drop=("xl/tables/table1.xml",)), []))
    bad_xml = _rezip(book("51_r2_badxml.xlsx", [("Calc", calc, None), ("Data", sheet(), None)], tables={1: [("Tbl", "A1:A4", "Col")]}),
                     replace={"xl/tables/table1.xml": b'<?xml version="1.0"?><table xmlns="x" id="1" name="Tbl"><tableColumns count="1">'})
    out.append(("malformed table XML", bad_xml, []))
    return out


def test_51_table_lookup_never_needed():
    """A structured reference / bare table name without a [n]! qualifier can only be this
    workbook's table, so 51's verdict never depends on finding or reading the table: black
    =SUM(Tbl[Col])*2 passes, black =[1]Prices!A1+SUM(Tbl[Col]) fails, whether the table is
    defined, undefined, its part missing or its XML malformed."""
    for label, p, expect in _table_books():
        v = run(p, 51)
        assert locs(v) == expect, (label, locs(v), v["summary"])
        assert v["stats"]["classified_by_class"]["external"] == len(expect), (label, v["stats"])
    # the failure is counted (stats: A1 and B1; the bare name in C1 never passes the pre-filter),
    # and the doubt about an unknown-scope name survives it
    v = run(_table_books()[0][1], 51)
    assert v["stats"]["table_lookup_failures_not_needing_it"] == 2 and v["stats"]["cells_classified"] == 2, v["stats"]
    names = [("XB", "[1]Prices!$A$1", 7)]
    p = book("51_r2_unsure.xlsx", [("Calc", sheet((1, [c("A1", BLACK, "SUM(Nope[Col])+XB")])), None)], names=names, ext_links=1)
    raises(lambda: run(p, 51), "may read another workbook")
    # table parts unreadable: a bare name that may be a table is never another workbook, also inside INDIRECT
    calc = sheet((1, [c("A1", BLACK, 'INDIRECT("Foo")*2'), c("B1", BLACK, "SUM(Foo)+[1]Prices!A1"), c("C1", BLACK, "SUM(Foo)*2")]))
    p = _rezip(book("51_r2_badxml2.xlsx", [("Calc", calc, None), ("Data", sheet(), None)], tables={1: [("Tbl", "A1:A4", "Col")]}),
               replace={"xl/tables/table1.xml": b"<table"})
    v = run(p, 51)
    assert locs(v) == ["Calc!B1"], (locs(v), v["summary"])
    # A1 and C1 never pass the pre-filter (Foo is no defined name, so it cannot reach another workbook)
    assert v["stats"]["cells_classified"] == 1 and v["stats"]["table_lookup_failures_not_needing_it"] == 0, v["stats"]
    pkg = Package.open(p)
    try:
        clf = CR.Classifier(pkg)
        assert clf._tables_error is not None, clf._tables_error
        for t in ('INDIRECT("Foo")*2', "SUM(Foo)*2", "Foo"):
            raises(lambda: clf.classify(t, "Calc"), "may be a table")
            c_ = clf.classify(t, "Calc", sheets_needed=False)
            assert c_.kind == CR.UNCLASSIFIED and c_.sheets_unknown and "may be a table" in c_.note, (t, c_)
        c_ = clf.classify('INDIRECT("Foo")+[1]Prices!A1', "Calc")
        assert c_.kind == CR.EXTERNAL and "may be a table" in c_.note, c_
    finally:
        pkg.close()


def test_classifier_sheets_unknown_contract():
    """classify() still raises on an unplaceable table by default (49 / 50 need the sheets);
    with sheets_needed=False it returns UNCLASSIFIED + sheets_unknown, never EXTERNAL; a formula
    that also reads another workbook is EXTERNAL either way (the kind is certain)."""
    pkg = Package.open(book("51_r2_clf.xlsx", [("Calc", sheet(), None), INPUTS], ext_links=1,
                            names=[("ExtName", "[1]Prices!$A$1", None)]))
    try:
        clf = CR.Classifier(pkg)
        raises(lambda: clf.classify("SUM(Nope[Col])", "Calc"), "table 'Nope' is not defined")
        raises(lambda: clf.classify("Inputs!A1+SUM(Nope[Col])", "Calc"), "table 'Nope' is not defined")
        for t in ("SUM(Nope[Col])", "Nope[Col]", "Inputs!A1+SUM(Nope[Col])", 'INDIRECT("Nope[Col]")'):
            c_ = clf.classify(t, "Calc", sheets_needed=False)
            assert c_.kind == CR.UNCLASSIFIED and c_.sheets_unknown and "Nope" in c_.note, (t, c_)
        for t in ("[1]Prices!A1+SUM(Nope[Col])", "SUM(Nope[Col])+ExtName", 'INDIRECT("Nope[Col]")+[1]Prices!A1'):
            c_ = clf.classify(t, "Calc")
            assert c_.kind == CR.EXTERNAL and not c_.sheets_unknown and "Nope" in (c_.note or ""), (t, c_)
        c_ = clf.classify("Inputs!A1*2", "Calc")
        assert c_.kind == CR.CROSS and not c_.sheets_unknown and c_.note is None, c_
    finally:
        pkg.close()
    # 49 and 50 keep raising where the table decides their class (unchanged behaviour)
    p = book("51_r2_4950.xlsx", [("Calc", sheet((1, [c("A1", GREEN, "SUM(Nope[Col])"), c("B1", BLACK, "Nope[Col]")])), None), INPUTS])
    graded("unparsable_formula", lambda: run(p, 49))           # skipped since Patrick 2026-10-05
    graded("unparsable_formula", lambda: run(p, 50))
    assert run(p, 51)["decision"] == "pass"
    # ... but an external cell is left to 51 whatever the table (certain kind), in one grade() call
    p = book("51_r2_all.xlsx", [("Calc", sheet((1, [c("A1", BLUE, "[1]Prices!A1+SUM(Nope[Col])")])), None)], ext_links=1)
    v = grade(p, checks=[49, 50, 51])
    assert v[K49]["decision"] == "pass" and v[K50]["decision"] == "pass" and locs(v[K51]) == ["Calc!A1"], v


def test_51_malformed_text_still_raises_only_when_it_could_be_external():
    """Unchanged contract: malformed formula text raises only when the pre-filter let it through
    (a '[' outside strings) and the cell is not red."""
    p = book("51_malformed.xlsx", [("Calc", sheet((1, [c("A1", BLACK, "SUM(C1:C5"), c("B1", RED, "[1]Prices!A1+SUM(C1:C5")])), None)],
             ext_links=1)
    assert run(p, 51)["decision"] == "pass"
    p = book("51_malformed_black.xlsx", [("Calc", sheet((1, [c("A1", BLACK, "[1]Prices!A1+SUM(C1:C5")])), None)], ext_links=1)
    graded("unparsable_formula", lambda: run(p, 51))


TESTS = [test_51_external_certain_despite_unknown_scope_name, test_51_table_lookup_never_needed,
         test_classifier_sheets_unknown_contract, test_51_malformed_text_still_raises_only_when_it_could_be_external]


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
        if TC._TMP and os.path.isdir(TC._TMP):
            shutil.rmtree(TC._TMP, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
