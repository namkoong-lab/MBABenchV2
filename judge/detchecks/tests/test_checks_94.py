"""Regression tests for No white-on-white hiding (94), second review (2026-10-04).  The first-round
tests live in test_checks_fills.py (test_94_*); this module reuses its workbook builder.

    cd judge && python -m detchecks.tests.test_checks_94

Plain asserts; also collectable by pytest.  Temporary files go to detchecks/scratch/fills/.
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
from xml.sax.saxutils import escape

import detchecks.tests.test_checks_fills as F
from detchecks.checks.c94 import C94
from detchecks.tests.test_checks_fills import Styles, book, c, locs, run, sheet, tmp


def _w(cells, st, name, tail):
    return book(tmp(name), [("S", sheet(cells, tail=tail))], st)


def _cf(sqref, *rules):
    return f'<conditionalFormatting sqref="{sqref}">' + "".join(rules) + "</conditionalFormatting>"


def _cellis(dxf, prio, op, f, stop=False):
    dx = f' dxfId="{dxf}"' if dxf is not None else ""
    return (f'<cfRule type="cellIs"{dx} priority="{prio}" operator="{op}"'
            f'{" stopIfTrue=\"1\"" if stop else ""}><formula>{escape(f)}</formula></cfRule>')


def _grade(cells, st, name, tail):
    v = run(_w(cells, st, name, tail), C94)
    return v["decision"], locs(v), v["stats"]["cf_second_pass_sheets"]


# ============================================================================ 94-cf-pretest-coarse-format-flag
def test_94_rereview_cf_format_over_risky_base():
    """A CF number format that blanks / whitens THIS value over a base format that is risky for OTHER
    values (an empty zero section) was never evaluated: the pre-test compared a coarse 'risky' flag."""
    st = Styles()
    s_zero = st.xf(numfmt="0;-0;;@")
    s_acct = st.xf(numfmt="#,##0;(#,##0);")
    s_plain = st.xf(numfmt="#,##0")
    s_hash = st.xf(numfmt="#,###")
    d_blank, d_white_tag = st.dxf(numfmt=";;;"), st.dxf(numfmt="[White]0")
    d_reveal, d_zero = st.dxf(numfmt='[>5]"";0'), st.dxf(numfmt="0")
    # reviewer q01: base '0;-0;;@' shows 5; CF > 1 applies ';;;' -> nothing shown
    assert _grade([c("A1", s_zero, 5.0)], st, "q01.xlsx",
                  _cf("A1", _cellis(d_blank, 1, "greaterThan", "1")))[:2] == ("fail", ["S!A1"])
    # q01c: the common finance format with an empty zero section; CF > 1000 applies ';;;' to 1200 only
    assert _grade([c("A1", s_acct, 1200.0), c("A2", s_acct, 7.0)], st, "q01c.xlsx",
                  _cf("A1:A2", _cellis(d_blank, 1, "greaterThan", "1000")))[:2] == ("fail", ["S!A1"])
    # q04: CF applies '[White]0' over the risky base -> white digits on white
    assert _grade([c("A1", s_zero, 5.0)], st, "q04.xlsx",
                  _cf("A1", _cellis(d_white_tag, 1, "greaterThan", "1")))[:2] == ("fail", ["S!A1"])
    # q02 (the reverse, a false fail): base '#,###' blanks 0; CF = 0 applies '[>5]"";0', which prints '0'
    assert _grade([c("A1", s_hash, 0.0)], st, "q02.xlsx",
                  _cf("A1", _cellis(d_reveal, 1, "equal", "0")))[:2] == ("pass", [])
    # controls that were already right: plain base under ';;;'; plain CF '0' revealing the zero
    assert _grade([c("A1", s_plain, 5.0)], st, "q01b.xlsx",
                  _cf("A1", _cellis(d_blank, 1, "greaterThan", "1")))[:2] == ("fail", ["S!A1"])
    assert _grade([c("A1", s_hash, 0.0)], st, "q02b.xlsx",
                  _cf("A1", _cellis(d_zero, 1, "equal", "0")))[:2] == ("pass", [])


def test_94_rereview_cf_pretest_exact_signature():
    """More flips the old pre-test missed (found while fixing): a CF format without the revealing
    colour tag, a CF font / fill over a tagged risky base, a priority order lost when two rules
    share a dxf, a colour-scale fill combined with a dxf font.  Harmless CF still skips the pass."""
    st = Styles()
    s_red_white = st.xf(font=st.font("FFFFFF"), numfmt="[Red]0")
    s_white_tag_zero = st.xf(numfmt="[White]0;-0;;@")
    s_acct = st.xf(numfmt="#,##0;(#,##0);")
    d_plain, d_black, d_navy = st.dxf(numfmt="0"), st.dxf(font="000000"), st.dxf(fill="1F4E78")
    # white font under '[Red]0' (5 shows red); CF applies '0' -> the white font shows
    assert _grade([c("A1", s_red_white, 5.0)], st, "x1.xlsx",
                  _cf("A1", _cellis(d_plain, 1, "greaterThan", "0")))[:2] == ("fail", ["S!A1"])
    # base '[White]0;-0;;@': a CF black font beats the tag (Excel 2026-10-03); a CF navy fill shows white digits
    assert _grade([c("A1", s_white_tag_zero, 5.0)], st, "x2.xlsx",
                  _cf("A1", _cellis(d_black, 1, "greaterThan", "0")))[:2] == ("pass", [])
    assert _grade([c("A1", s_white_tag_zero, 5.0)], st, "x3.xlsx",
                  _cf("A1", _cellis(d_navy, 1, "greaterThan", "0")))[:2] == ("pass", [])
    # r1 (> 100) white + navy; r2 (> 0) F2F2F2 fill; r3 (> 0) reuses r1's dxf: on 5 -> white on F2F2F2
    e1, e2 = st.dxf(font="FFFFFF", fill="1F4E78"), st.dxf(fill="F2F2F2")
    tail = _cf("A1", _cellis(e1, 1, "greaterThan", "100"), _cellis(e2, 2, "greaterThan", "0"),
               _cellis(e1, 3, "greaterThan", "0"))
    assert _grade([c("A1", 0, 5.0)], st, "x4.xlsx", tail)[:2] == ("fail", ["S!A1"])
    # colour scale 808080 -> 909090 plus a CF grey 808080 font: grey on grey - but a colour scale is a built-in
    # conditional format and never counts as hiding text (Patrick 2026-10-05; failed until then); the pre-test
    # still sends the sheet to the second pass, where the cells are recorded as kept visible
    d_grey = st.dxf(font="808080")
    scale = ('<cfRule type="colorScale" priority="1"><colorScale><cfvo type="min"/><cfvo type="max"/>'
             '<color rgb="FF808080"/><color rgb="FF909090"/></colorScale></cfRule>')
    v = run(_w([c("A1", 0, 1.0), c("A2", 0, 5.0)], st, "x5.xlsx", _cf("A1:A2", scale, _cellis(d_grey, 2, "greaterThan", "0"))),
            C94)
    assert v["decision"] == "pass" and v["stats"]["cf_second_pass_sheets"] == ["S"], v["stats"]
    assert v["stats"]["cf_builtin_visible"]["cells"] == 2, v["stats"]["cf_builtin_visible"]
    # the same grey font over a plain dxf fill 909090 (not built-in): grey on grey, concealed
    d_fill = st.dxf(fill="909090")
    assert _grade([c("A1", 0, 1.0), c("A2", 0, 5.0)], st, "x5b.xlsx",
                  _cf("A1:A2", _cellis(d_fill, 1, "greaterThan", "0"), _cellis(d_grey, 2, "greaterThan", "0")))[:2] \
        == ("fail", ["S!A1:A2"])
    # controls: a red error-check fill under black text needs no second pass; a light CF fill cannot
    # reveal a zero blanked by its format (A1), and 7 stays visible (A2)
    d_red, d_pale = st.dxf(fill="FF0000"), st.dxf(fill="FFF2CC")
    unk = '<cfRule type="expression" dxfId="{}" priority="1"><formula>$B1="x"</formula></cfRule>'
    assert _grade([c("A1", 0, 5.0), c("A2", 0, 1.0, f="1")], st, "x6.xlsx",
                  _cf("A1:A3", unk.format(d_red))) == ("pass", [], [])
    assert _grade([c("A1", s_acct, 0.0), c("A2", s_acct, 7.0)], st, "x7.xlsx",
                  _cf("A1:A2", unk.format(d_pale))) == ("fail", ["S!A1"], [])
    # a PASS/FAIL checks block (corpus 1285 pattern): white text only ever comes with its own green
    # fill, FAIL gets a red fill; no reachable outcome is white on white -> no second pass
    d_pass, d_fail = st.dxf(font="FFFFFF", fill="00B050"), st.dxf(fill="FF0000")
    tail = _cf("A1:A2", f'<cfRule type="containsText" dxfId="{d_pass}" priority="1" operator="containsText" text="PASS">'
                        '<formula>NOT(ISERROR(SEARCH("PASS",A1)))</formula></cfRule>',
               f'<cfRule type="containsText" dxfId="{d_fail}" priority="2" operator="containsText" text="FAIL">'
               '<formula>NOT(ISERROR(SEARCH("FAIL",A1)))</formula></cfRule>')
    assert _grade([c("A1", 0, "PASS"), c("A2", 0, "FAIL")], st, "x8.xlsx", tail) == ("pass", [], [])
    # ... also over a number format with a (visible) [Red] tag, which a CF font switches off: the
    # verdict cannot change, so still no second pass (corpus 1285 'Checks': dark font + pale fill)
    s_red_neg = st.xf(numfmt="#,##0.00;[Red]\\(#,##0.00\\);\\-")
    d_ok, d_bad = st.dxf(font="006100", fill="E2F0D9"), st.dxf(font="C00000", fill="F4CCCC")
    tail_1285 = _cf("A1:A2", _cellis(d_ok, 1, "equal", '"PASS"'), _cellis(d_bad, 2, "equal", '"FAIL"'))
    assert _grade([c("A1", s_red_neg, 5.0), c("A2", 0, "PASS")], st, "y1.xlsx", tail_1285) == ("pass", [], [])
    # but a CF red FILL under that format can hide a negative number's red digits: second pass, decided
    # per value (-5 under the red FAIL fill is concealed; 5 is not)
    tail_red = _cf("A1:A2", f'<cfRule type="cellIs" dxfId="{d_fail}" priority="1" operator="notEqual">'
                            '<formula>0</formula></cfRule>')
    assert _grade([c("A1", s_red_neg, -5.0), c("A2", s_red_neg, 5.0)], st, "y1b.xlsx", tail_red) == \
        ("fail", ["S!A1"], ["S"])


# ============================================================================ 94-stopiftrue-rules-without-dxf-dropped
def test_94_rereview_stop_if_true_without_format():
    """A 'Stop If True' rule with no format (no dxfId) or a border-only dxf still stops lower rules."""
    st = Styles()
    s_w = st.xf(font=st.font("FFFFFF"))
    d_white, d_black, d_red = st.dxf(font="FFFFFF"), st.dxf(font="000000"), st.dxf(fill="FF0000")
    st.dxfs.append('<dxf><border><left style="thin"><color rgb="FF000000"/></left></border></dxf>')
    d_border = len(st.dxfs) - 1
    # s01: the blocker (> 0, no dxfId, stop) fires on 5 -> the white-font rule below is never applied
    assert _grade([c("A1", 0, 5.0)], st, "s01.xlsx",
                  _cf("A1", _cellis(None, 1, "greaterThan", "0", stop=True),
                      _cellis(d_white, 2, "greaterThan", "0")))[:2] == ("pass", [])
    # s01b (control, already right): the same blocker with a red fill
    assert _grade([c("A1", 0, 5.0)], st, "s01b.xlsx",
                  _cf("A1", _cellis(d_red, 1, "greaterThan", "0", stop=True),
                      _cellis(d_white, 2, "greaterThan", "0")))[:2] == ("pass", [])
    # s02: a border-only dxf with stopIfTrue blocks too
    assert _grade([c("A1", 0, 5.0)], st, "s02.xlsx",
                  _cf("A1", _cellis(d_border, 1, "greaterThan", "0", stop=True),
                      _cellis(d_white, 2, "greaterThan", "0")))[:2] == ("pass", [])
    # s03 (control): the blocker does not fire on 5 -> the white rule applies
    assert _grade([c("A1", 0, 5.0)], st, "s03.xlsx",
                  _cf("A1", _cellis(None, 1, "greaterThan", "100", stop=True),
                      _cellis(d_white, 2, "greaterThan", "0")))[:2] == ("fail", ["S!A1"])
    # s05: white on white; the blocker fires, so the revealing black-font rule is never applied
    assert _grade([c("A1", s_w, 5.0)], st, "s05.xlsx",
                  _cf("A1", _cellis(None, 1, "greaterThan", "0", stop=True),
                      _cellis(d_black, 2, "greaterThan", "0")))[:2] == ("fail", ["S!A1"])
    # s04: a blocker that reads another cell is evaluated through the value source (Patrick 2026-10-05; it
    # raised 'cannot decide' until then): B1 = 1 > 0 -> it fires and blocks the white rule; B1 = -1 -> white
    tail = _cf("A1", '<cfRule type="expression" priority="1" stopIfTrue="1"><formula>$B$1&gt;0</formula></cfRule>',
               _cellis(d_white, 2, "greaterThan", "0"))
    assert _grade([c("A1", 0, 5.0), c("B1", 0, 1.0)], st, "s04.xlsx", tail)[:2] == ("pass", [])
    assert _grade([c("A1", 0, 5.0), c("B1", 0, -1.0)], st, "s04b.xlsx", tail)[:2] == ("fail", ["S!A1"])
    # ... and a blocker that cannot be evaluated: the outcome is open, the text assumed visible and recorded
    tail = _cf("A1", '<cfRule type="expression" priority="1" stopIfTrue="1"><formula>COUNTA($B:$B)&gt;0</formula>'
                     '</cfRule>', _cellis(d_white, 2, "greaterThan", "0"))
    v = run(_w([c("A1", 0, 5.0)], st, "s04c.xlsx", tail), C94)
    assert v["decision"] == "pass" and v["stats"]["cf_assumptions"]["cells"] == 1, v["stats"]
    # a format-less rule WITHOUT stopIfTrue changes nothing (still dropped): the white rule applies
    assert _grade([c("A1", 0, 5.0)], st, "s06.xlsx",
                  _cf("A1", _cellis(None, 1, "greaterThan", "0"),
                      _cellis(d_white, 2, "greaterThan", "0")))[:2] == ("fail", ["S!A1"])


TESTS = [test_94_rereview_cf_format_over_risky_base, test_94_rereview_cf_pretest_exact_signature,
         test_94_rereview_stop_if_true_without_format]


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
        if F._TMP and os.path.isdir(F._TMP):
            shutil.rmtree(F._TMP, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
