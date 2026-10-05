"""Tests for detchecks.core.numfmt (plain asserts, no test framework).

Run:  /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tests.test_numfmt

The TOY_FORMATS table lists every custom <numFmt> found in the styles.xml of the toy
workbooks (all 22 check folders, read once with detchecks/scratch/extract_numfmts.py),
with the toys they occur in.  The built-in ids those toys reference without a custom
definition were 0 1 3 4 9 10 14 16 17 40 43 44 49.  No workbook is opened here.
"""
from __future__ import annotations

import sys
import traceback

from detchecks.core import numfmt as N
from detchecks.errors import GradingError

R = N.render

# --------------------------------------------------------------------------- toy formats
TOY_FORMATS = {
    # code: toys (check numbers)
    '#,###_);\\(#,###\\);\\-': "69 73",
    '#,##0" €"': "49 50",
    '#,##0.0': "22 23 29 49 50 51 61 62 70 73 77 80 87 92 95",
    '#,##0.000': "93",
    '#,##0.0000': "65 80",
    '#,##0.00000': "29 61 65 87 93 94",
    '#,##0.00000000': "47 93",
    '#,##0.00;\\(#,##0.00\\)': "22 29 61 62 65 69 77 87 93",
    '#,##0.00;\\(#,##0.00\\);\\-': "22 23 29 47 49 50 51 65 66 69 70 77 80 87 93 94 95",
    '#,##0.00\\ "€";[Red]\\-#,##0.00\\ "€"': "22 29 47 51 62 69 80 92 94 (94/T5 as numFmtId 8)",
    '#,##0.00\\x': "23 51 61 73 95",
    '#,##0.00\\x;\\(#,##0.00\\)': "29 50 51 80 87",
    '#,##0.00\\x;\\(#,##0.00\\x\\);\\-': "51",
    '#,##0.00_);\\(#,##0.00\\);\\-': "22 47 51 61 62 65 66 69 73 74 80 87 92 93 94",
    '#,##0.0;\\(#,##0.0\\);\\-': "65 66 70 77 87 94",
    '#,##0;[Red]#,##0': "65",
    '#,##0;[Red](#,##0)': "65",
    '#,##0;[Red]\\(#,##0\\)': "65",
    '#,##0;\\(#,##0\\)': "65",
    '#,##0;\\(#,##0\\);\\-': "47 49 50 51 69 73 94",
    '#,##0_);\\(#,##0\\)': "49",
    '#,##0_);\\(#,##0\\);\\-': "22 23 29 47 49 50 51 61 62 65 66 69 70 73 74 77 80 87 92 93 94 95",
    '0.0%': "66",
    '0.0%;\\(0.0%\\)': "65",
    '0.0%;\\(0.0%\\);\\-': "22 51 65 87 94",
    '0.0%_);\\(0.0%\\)': "80",
    '0.00%;\\(0.00%\\)': "65",
    '0.0000%': "22 29 73 93",
    '0.00\\x;\\(0.00"x)";\\-': "65 87",
    '0.0;\\(0.0\\);\\-': "87",
    '0.0\\%': "47 92",
    '0;\\(0\\)': "65",
    '0;\\(0\\);\\-': "66",
    ';;;': "94 (T3 Fail)",
    '[>=0.005]#,##0.00;[<=-0.005]\\(#,##0.00\\);\\-': "66 (T1 Pass)",
    '\\$#,##0.00;"($"#,##0.00\\);\\-': "65 87",
    '\\$#,##0;"($"#,##0\\);\\-': "22 87",
    '_-* #,##0.00_-;\\-* #,##0.00_-;_-* "-"??_-;_-@_-': "50",
    'mm\\/dd\\/yyyy': "22 47 51 61 62 65 69 80 92",
    'mmm\\-yy': "49 50",
}
TOY_BUILTIN_IDS = (0, 1, 3, 4, 9, 10, 14, 16, 17, 40, 43, 44, 49)


def test_toy_table_matches_extract_when_available():
    """The embedded TOY_FORMATS must list exactly the codes extracted from the toys
    (detchecks/scratch/toy_numfmts.json, written by scratch/extract_numfmts.py via
    heavy_run).  Skipped silently when the scratch extract is absent."""
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "scratch", "toy_numfmts.json")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        d = json.load(fh)
    codes = {c for x in d.values() for c in x["numfmts"].values()}
    assert codes == set(TOY_FORMATS), codes ^ set(TOY_FORMATS)
    ids = {int(i) for x in d.values() for i in x["xf_ids"] if i is not None and int(i) < 164
           and i not in x["numfmts"]}
    assert ids == set(TOY_BUILTIN_IDS), ids


def test_every_toy_format_parses_and_renders():
    for code in TOY_FORMATS:
        f = N.parse_format(code)
        assert f.sections, code
        for v in (0, 1234.5678, -1234.5678, 0.5, -0.004, 1e9, 45000):
            r = R(v, code)
            assert isinstance(r.text, str) and r.format_code == code, (code, v)
        r = R("label", code)
        assert r.kind == "text"
    for fid in TOY_BUILTIN_IDS:
        for v in (0, 1234.5, -1234.5, "label"):
            R(v, fid)


def test_toy_formats_zero_display():
    """What an exact 0 shows under every toy format (checks 66 and 94)."""
    dash = {c for c in TOY_FORMATS if c.endswith("\\-") or c.endswith('"-"??_-;_-@_-')}
    blank = {";;;"}
    for code in TOY_FORMATS:
        r = R(0, code)
        if code in dash:
            assert r.zero_as_dash and not r.is_blank, (code, r.text)
        elif code in blank:
            assert r.is_blank and not r.zero_as_dash, code
        elif code.startswith("mm"):
            assert r.kind == "date" and not r.zero_as_dash, code
        else:
            assert not r.zero_as_dash and not r.is_blank and any(ch.isdigit() for ch in r.text), (code, r.text)
    assert len(dash) == 15
    # '#,###_);\(#,###\);\-' (73): zero is the third section, a dash
    assert R(0, '#,###_);\\(#,###\\);\\-').text == "-"


def test_toy_66_t1_dashed_zeros_and_residues():
    code = '[>=0.005]#,##0.00;[<=-0.005]\\(#,##0.00\\);\\-'
    r = R(0, code)
    assert r.text == "-" and r.zero_as_dash and r.section_index == 2
    r = R(2.33e-10, code)                      # AL9 / AL79 floating residue (spill of SCAN)
    assert r.text == "-" and r.shows_dash and not r.zero_as_dash and r.section_index == 2
    r = R(-2.33e-10, code)                     # negative residue: fallback section, no minus (Excel 2026-10-03)
    assert r.section_index == 2 and r.shows_dash and r.certain and r.text == "-"
    assert R(5, code).text == "5.00" and R(5, code).certain
    r = R(-5, code)
    assert r.section_index == 1 and r.parens and r.certain and r.text == "(5.00)"   # no auto minus (Excel 2026-10-03)
    assert R(0.004999, code).text == "-"
    assert R(1e11, '[>=1E11]"big";#,##0').text == "big"           # E-notation threshold
    r = R(0, '[>=1E11]"big";#,##0')
    assert r.text == "0" and not r.zero_as_dash                   # zero falls to the 2nd section


def test_toy_66_t2_general_zero_is_not_a_dash():
    # OnlyOperational CQV45 lost its number format: General shows '0'
    r = R(0, 0)
    assert r.text == "0" and not r.zero_as_dash
    assert R(0, "#,##0.00;\\(#,##0.00\\);\\-").zero_as_dash        # its neighbours


def test_toy_66_t3_builtin_accounting_ids():
    for fid in (41, 42, 43, 44):
        r = R(0, fid)
        assert r.zero_as_dash and not r.is_blank, (fid, r.text)
    assert R(0, 43).text == " -   " and R(0, 44).text == " $-   "
    for fid in (37, 38, 39, 40):
        r = R(0, fid)
        assert not r.zero_as_dash and r.text.strip() in ("0", "0.00"), (fid, r.text)
    assert R(-1234.5, 40).parens and R(-1234.5, 40).color_rgb == "FF0000"
    assert R(-1234.5, 43).parens and not R(-1234.5, 43).minus


def test_toy_94_number_format_hiding():
    # T3 Fail: SingleForever Z412 under ';;;'
    r = R(1568455.5167732567, ";;;")
    assert r.is_blank and r.text == "" and r.section_index == 0
    assert R("text", ";;;").is_blank and R(-5, ";;;").is_blank and R(0, ";;;").is_blank
    # T3 Pass: the same value under the original format is visible
    assert R(1568455.5167732567, "#,##0_);\\(#,##0\\);\\-").text == "1,568,456 "
    # other ways a number format prints nothing
    assert R(0, "#,##0;(#,##0);").is_blank                        # empty zero section
    assert R(0, "0;-0;;@").is_blank and not R(5, "0;-0;;@").is_blank and R("x", "0;-0;;@").text == "x"
    assert R(0, "#,###").is_blank and R(0.3, "#,###").is_blank and R(12, "#,###").text == "12"
    assert R(5, ";;").is_blank and not R("abc", ";;").is_blank    # 3 sections: text stays visible
    assert R(-5, "0;").is_blank                                    # empty negative section
    assert R(5, "_)").is_blank                                     # padding only
    assert R(5, "* ").is_blank                                     # space fill only
    assert not R(5, "*-").is_blank and R(5, "*-").shows_dash       # a visible fill
    assert R("x", '0;0;0;"hidden"').text == "hidden"               # text replaced by a literal
    # white via the format's colour tag: not blank, the colour is reported for the check
    r = R(5, "[White]0")
    assert not r.is_blank and r.color_tag == "White" and r.color_rgb == "FFFFFF"
    # T5: the toy's own numFmtId 8 redefinition beats the built-in table
    custom = {8: '#,##0.00\\ "€";[Red]\\-#,##0.00\\ "€"'}
    assert N.resolve_format(8, custom) == custom[8]
    assert R(-5, 8, custom_formats=custom).text == "-5.00 €"
    assert R(-5, 8).text == "($5.00)" and R(-5, 8).locale_dependent
    assert not R(-5, 8, custom_formats=custom).locale_dependent
    assert N.resolve_format("8", {"8": "0.0"}) == "0.0"


# --------------------------------------------------------------------------- built-in table
def test_builtin_table():
    for fid in list(range(0, 23)) + list(range(27, 50)):
        assert N.builtin_format(fid) is not None, fid
    for fid in (23, 24, 25, 26, 63, 64, 65, 66, 82, 163, 164):
        assert N.builtin_format(fid) is None
        try:
            N.resolve_format(fid)
            raise AssertionError(f"id {fid} resolved")
        except N.NumFmtError:
            pass
    assert issubclass(N.NumFmtError, GradingError)
    # id 44: the corrected 4-section accounting code (openpyxl's copy lacks the ';')
    assert len(N.parse_format(N.builtin_format(44)).sections) == 4
    assert N.builtin_format(44) == '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
    assert N.builtin_format(14) == "m/d/yyyy" and R(45000, 14).text == "3/15/2023" and R(45000, 14).locale_dependent
    assert R(45000, 22).text == "3/15/2023 0:00"
    assert R(0.5, 9).text == "50%" and R(0.12345, 10).text == "12.35%"
    assert R(1234.5, 11).text == "1.23E+03" and R(12345, 48).text == "12.3E+3"
    assert R(1.5, 12).text == "1 1/2" and R(0.25, 13).text == "  1/4 "
    assert R(45000, 15).text == "15-Mar-23" and R(45000, 16).text == "15-Mar" and R(45000, 17).text == "Mar-23"
    assert R(0.75, 18).text == "6:00 PM" and R(0.75, 19).text == "6:00:00 PM" and R(0.75, 20).text == "18:00"
    assert R(1.5, 46).text == "36:00:00" and R(0.5 / 24, 45).text == "30:00"
    assert R(1234.5, 3).text == "1,235" and R(1234.5, 4).text == "1,234.50" and R(2.5, 1).text == "3"
    assert R("t", 49).text == "t" and R(5, 49).text == "5"
    for fid in list(range(27, 37)) + list(range(50, 59)) + list(range(71, 82)):
        assert N.is_date_format(fid), fid
        assert R(45000, fid).kind in ("date", "elapsed") and R(45000, fid).locale_dependent
    assert R(1234.5, 61).text == "1,235" and R(0.5, 67).text == "50%"   # Thai ids -> Western forms


# --------------------------------------------------------------------------- sections
def test_split_sections_respects_quotes_brackets_escapes():
    assert N.split_sections('0;"a;b";\\;;[<0]0') == ["0", '"a;b"', "\\;", "[<0]0"]
    assert N.split_sections("_;0;*;0") == ["_;0", "*;0"]
    assert N.split_sections(";;;") == ["", "", "", ""]
    for bad in ('"abc', "[Red0", "0;0;0;0;0"):
        try:
            N.parse_format(bad)
            raise AssertionError(bad)
        except N.NumFmtError:
            pass


def test_section_selection():
    s = N.select_section
    assert s("0", 5)[:2] == (0, False) and s("0", -5)[:2] == (0, True) and s("0", 0)[:2] == (0, False)
    assert s("0;(0)", 5)[:2] == (0, False) and s("0;(0)", -5)[:2] == (1, False) and s("0;(0)", 0)[:2] == (0, False)
    assert s("0;(0);-", 0)[:2] == (2, False) and s("0;(0);-;@", -1)[:2] == (1, False)
    assert s("0.00;@", -5)[:2] == (0, True)                        # '@' last section = text
    assert s("0;-0;@", 0)[:2] == (0, False)                        # zero uses the first section
    assert s("@", 5)[0] is None                                    # numbers shown as General
    assert s("[>100]0;[<-100]0;0.0", 5)[:2] == (2, False)
    assert s("[>100]0;[<-100]0;0.0", -500)[:2] == (1, False)
    assert s("[>=1000]#,##0,\"K\";0", -5) == (1, False, True)      # fallback section: NO minus (Excel 2026-10-03)
    idx, _, certain = s('[=1]"Yes";[=0]"No"', 2)
    assert idx is None and not certain                             # no section -> '#####'
    r = R(2, '[=1]"Yes";[=0]"No"')
    assert r.is_hash and r.text == "#####"
    assert R(1, '[=1]"Yes";[=0]"No"').text == "Yes" and R(0, '[=1]"Yes";[=0]"No"').text == "No"
    assert R(-0.0, "0;(0);\\-").text == "-"                        # negative zero is zero


def test_dash_detection_variants():
    for code in ('#,##0;(#,##0);"-"', "#,##0;-#,##0;\\-", "0.0%;(0.0%);\\-", '#,##0_);(#,##0);–',
                 '#,##0.00_);\\(#,##0.00\\);"–"_)', '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-',
                 '[=0]"-";#,##0', "0.00E+00;(0.00E+00);\"-\"", "* \"-\"", '0;0;"- "'):
        r = R(0, code)
        assert r.zero_as_dash and not r.is_blank, (code, r.text)
    for code in ("General", "0", "#,##0.00", "0.0%", "$0", '#,##0;(#,##0);"n/a"', "0.00E+00", "#,##0_);(#,##0)",
                 "#,##0.0\"x\""):
        r = R(0, code)
        assert not r.zero_as_dash, (code, r.text)
    assert R(0, '#,##0;(#,##0);"n/a"').text == "n/a"
    assert R(-5, '0;"-"').shows_dash and not R(-5, '0;"-"').zero_as_dash


def test_minus_versus_parentheses():
    cases = [("#,##0;(#,##0)", -5, False, True, "(5)"), ("#,##0;-#,##0", -5, True, False, "-5"),
             ("#,##0", -5, True, False, "-5"), ("#,##0;-(#,##0)", -5, True, True, "-(5)"),
             ("0;[Red]0", -5, False, False, "5"), ("General", -5, True, False, "-5"),
             ("General;(General)", -5, False, True, "(5)"), ("#,##0_);\\(#,##0\\);\\-", -1234, False, True, "(1,234)"),
             ('\\$#,##0;"($"#,##0\\);\\-', -5, False, True, "($5)"), ('"$"#,##0', -5, True, False, "-$5"),
             ('#,##0.00\\ "€";[Red]\\-#,##0.00\\ "€"', -5, True, False, "-5.00 €"),
             ("_-* #,##0.00_-;\\-* #,##0.00_-;_-* \"-\"??_-;_-@_-", -3.5, True, False, "-3.50 "),
             ("0.0%;\\(0.0%\\)", -0.123, False, True, "(12.3%)"), ("000-00-0000", 123456789, False, False, "123-45-6789")]
    for code, v, minus, parens, text in cases:
        r = R(v, code)
        assert (r.minus, r.parens, r.text) == (minus, parens, text), (code, v, r)
    assert R(-0.001, "0").text == "-0"                              # Excel keeps the sign of a rounded negative
    assert not R(45000, "d-mmm-yy").minus                           # dashes in dates are not signs


def test_number_rendering_details():
    t = lambda v, c: R(v, c).text                                   # noqa: E731
    assert t(1234567.891, "#,##0.00") == "1,234,567.89"
    assert t(2.675, "0.00") == "2.68" and t(0.125, "0.00") == "0.13" and t(1.005, "0.00") == "1.01"
    assert t(0.5, "#.##") == ".5" and t(0.5, "0.00") == "0.50" and t(5, "0.#") == "5." and t(5.25, ".00") == "5.25"
    assert t(5, "000") == "005" and t(5, "??0") == "  5" and t(1.5, "0.0?") == "1.5 "
    assert t(1500000, '#,##0.0,,"M"') == "1.5M" and t(1500, "#,##0,") == "2" and t(-1234.5, "#,##0.0,") == "-1.2"
    assert t(0.25, "0.0\\%") == "0.3%"                               # escaped % does not scale
    assert t(0.25, "0%") == "25%" and t(0.123, "0.0%") == "12.3%"
    assert t(5, "#,##0.00\\x") == "5.00x" and t(5, '0.00\\x;\\(0.00"x)";\\-') == "5.00x"
    assert t(-5, '0.00\\x;\\(0.00"x)";\\-') == "(5.00x)"
    assert t(1234, '#,##0" €"') == "1,234 €" and t(5, "[$€-407]#,##0.00") == "€5.00"
    assert t(5, "[$-409]0.0") == "5.0" and t(5, "[DBNum1]0") == "5"
    assert t(1234.5, "0.00E+00") == "1.23E+03" and t(0.000123, "0.00E+00") == "1.23E-04"
    assert t(0, "0.00E+00") == "0.00E+00" and t(1234.5, "0.0E-0") == "1.2E3" and t(12345, "##0.0E+0") == "12.3E+3"
    assert t(1.5, "# ?/?") == "1 1/2" and t(0.75, "?/?") == "3/4" and t(1.25, "# ??/100") == "1 25/100"
    assert t(3.14159, "# ?/8") == "3 1/8" and t(2, "# ?/?") == "2    "
    assert t(5, '"Total: "General') == "Total: 5" and t(5, "[Red]") == "5"
    assert t(-1, '"Yes"') == "-Yes"
    assert t(5, "_($* #,##0_)") == " $5 "


def test_general():
    g = N.general_text
    assert [g(x) for x in (0, 1, -5, 100, 1 / 3, 2 / 3, 0.1 + 0.2)] == ["0", "1", "-5", "100", "0.333333333", "0.666666667", "0.3"]
    assert g(1234567.891) == "1234567.891" and g(12345678901) == "12345678901" and g(123456789012) == "1.23457E+11"
    assert g(1e15) == "1E+15" and g(0.0001) == "0.0001" and g(0.00001) == "1E-05" and g(2.33e-10) == "2.33E-10"
    assert g(0.000123456789) == "0.000123457"
    assert R(2.33e-10, "General").text == "2.33E-10"
    assert R(1234.5, "general").text == "1234.5" and R(1234.5, "").text == "1234.5" and R(1234.5, None).text == "1234.5"


def test_dates_and_times():
    t = lambda v, c, **k: R(v, c, **k).text                         # noqa: E731
    assert t(1, "m/d/yyyy") == "1/1/1900" and t(60, "m/d/yyyy") == "2/29/1900" and t(61, "m/d/yyyy") == "3/1/1900"
    assert t(0, "m/d/yyyy") == "1/0/1900" and t(0, "yyyy-mm-dd") == "1900-01-00"
    assert t(45000, "mm\\/dd\\/yyyy") == "03/15/2023" and t(45000, "mmm\\-yy") == "Mar-23"
    assert t(45000.5, "dddd, mmmm d, yyyy h:mm AM/PM") == "Wednesday, March 15, 2023 12:00 PM"
    assert t(45000, "ddd mmmmm yy") == "Wed M 23"
    assert t(0.75, "h:mm") == "18:00" and t(0.75, "hh:mm A/P") == "06:00 P"
    assert t(0.0204861, "h:mm:ss.00") == "0:29:30.00" and t(0.99999999, "h:mm:ss") == "0:00:00"
    assert t(45000.25, "m") == "3" and t(45000.25, "h:m") == "6:0" and t(45000.25, "mm:ss") == "00:00"
    assert t(1.5, "[h]:mm:ss") == "36:00:00" and t(1.5, "[mm]:ss") == "2160:00" and t(0.001, "[ss].0") == "86.4"
    assert t(1, "m/d/yyyy", date1904=True) == "1/2/1904"
    r = R(-1, "yyyy-mm-dd")
    assert r.is_hash and r.text == "#####" and not r.is_blank
    assert R(-1, "[h]:mm").is_hash and R(3e6, "yyyy").is_hash
    assert R(45000, "yyyy").kind == "date" and N.is_date_format("mmm\\-yy") and not N.is_date_format("#,##0")
    assert N.parse_format("[h]:mm:ss").is_date and N.is_date_format(14)


def test_text_values_and_text_sections():
    assert R("abc", "General").text == "abc" and R("abc", "0.00").text == "abc"
    assert R("abc", "@").text == "abc" and R("abc", '"Note: "@').text == "Note: abc"
    r = R("abc", "0;0;0;[Red]@")
    assert r.text == "abc" and r.color_rgb == "FF0000" and r.section_index == 3
    assert R("abc", "0;0;0;[Blue]").text == "abc" and R("abc", "0;0;0;[Blue]").color_tag == "Blue"
    r = R("abc", "_-* #,##0.00_-;\\-* #,##0.00_-;_-* \"-\"??_-;_-@_-")
    assert r.text == " abc " and not r.is_blank
    assert R("", "General").is_blank and R("   ", "General").is_blank
    r = R("abc", "[Red]0")
    assert r.color_tag is None and r.certain and r.text == "abc"    # numeric colour tag never colours text (Excel)
    assert R("abc", 49).text == "abc"


def test_booleans_errors_empty():
    r = R(True, "0")
    assert r.text == "TRUE" and r.kind == "bool" and r.certain
    r = R(False, ";;;")
    assert r.text == "" and r.is_blank and r.certain and r.kind == "bool"   # text section (Excel 2026-10-03)
    assert R(True, "0;0;0;@").text == "TRUE" and R(True, "0;0;0;@").certain
    r = R(N.ErrorValue("#DIV/0!"), ";;;")
    assert r.text == "#DIV/0!" and r.kind == "error" and not r.is_blank
    assert R("#N/A", ";;;", value_type="e").text == "#N/A"
    r = R(None, "0.00")
    assert r.is_blank and r.kind == "empty"


def test_value_types():
    assert R("5", "0.00", value_type="n").text == "5.00"
    assert R("1", "0", value_type="b").text == "TRUE" and R("0", "0", value_type="b").text == "FALSE"
    assert R(5, "0", value_type="s").text == "5" and R(5, "0", value_type="s").kind == "text"
    assert R("2023-03-15T00:00:00", "m/d/yyyy", value_type="d").text == "3/15/2023"
    assert R("1900-01-01", "m/d/yyyy", value_type="d").text == "1/1/1900"
    for bad in [("abc", "0", "n"), (5, "0", "x")]:
        try:
            R(bad[0], bad[1], value_type=bad[2])
            raise AssertionError(bad)
        except N.NumFmtError:
            pass
    for bad in (float("nan"), float("inf")):
        try:
            R(bad, "0")
            raise AssertionError(bad)
        except N.NumFmtError:
            pass


def test_colors():
    assert R(5, "[Red]0").color_rgb == "FF0000" and R(5, "[red]0").color_rgb == "FF0000"
    assert R(5, "[Color10]0").color_rgb == "008000" and R(5, "[COLOR 10]0").color_rgb == "008000"
    assert R(5, "[Color1]0").color_rgb == "000000" and R(5, "[Color56]0").color_rgb == "333333"
    pal = ["000000"] * 17 + ["123456"]
    assert R(5, "[Color10]0", indexed_palette=pal).color_rgb == "123456"
    assert R(-5, "0;[Red]0").color_tag == "Red" and R(5, "0;[Red]0").color_tag is None
    assert R(0, "0;0;[Blue]0").color_rgb == "0000FF"
    assert N.color_tag_rgb("Color99") is None and N.color_tag_rgb(None) is None


def test_display_length_for_widths():
    assert R(1234567.891, "#,##0.00").display_length == 12
    assert R(1234, "#,##0_);\\(#,##0\\)").display_length == 6          # padding counts
    assert R(5, "* #,##0").display_length == 1                       # fill does not
    assert R(0.5, "0.0?").display_length == 4                        # '?' blank counts
    assert R(45000.5, "dddd, mmmm d, yyyy").display_length == len("Wednesday, March 15, 2023")


def test_zero_display_helper():
    assert N.zero_display(43) == "dash" and N.zero_display(40) == "digit" and N.zero_display(";;;") == "blank"
    assert N.zero_display('#,##0;(#,##0);"n/a"') == "other" and N.zero_display('[=1]"Y";[=2]"N"') == "hash"



# --------------------------------------------------------------------------- review regressions (2026-10-02)
def test_review_f7_huge_magnitudes_render_15_digits_then_zeros():
    assert R(1e30, "#,##0").text == "1,000,000,000,000,000,000,000,000,000,000"
    assert R(1e26, "0.00").text == "1" + "0" * 26 + ".00"
    assert R(1e28, "0").text == "1" + "0" * 28
    assert R(1e30, '0;-0;"-"').text == "1" + "0" * 30
    assert R(-1e30, '0;-0;"-"').text == "-1" + "0" * 30
    assert R(1.2345678901234567e30, "0").text == "123456789012346" + "0" * 16      # 15 significant digits
    assert R(1e30, "0%").text == "1" + "0" * 32 + "%"
    assert R(1e307, "0%").text == "1" + "0" * 309 + "%"                          # float overflow -> exact
    assert R(1e300, "0.00E+00").text == "1.00E+300" and R(1e300, "0.0%E+0").text.startswith("1.0%E")
    assert R(1e30, "# ?/?").text.startswith("1" + "0" * 30) and R(1e30, "?/?").text == "1" + "0" * 30 + "/1"
    r = R(1e30, "[h]:mm")
    assert r.text == "24" + "0" * 30 + ":00" and r.certain                       # past 9999-12-31: Excel keeps counting
    assert R(1.5, "[h]:mm").certain
    for code in ("#,##0", "0.00", "0", "0%", "#,##0,", "[h]:mm:ss", "0.00E+00", "# ?/?", "General", 43, 44):
        R(1.7e308, code)                                                       # never decimal.InvalidOperation


def test_review_f8_empty_or_space_only_code_is_uncertain():
    """Since Excel 2026-10-03 these are certain: a spaces-only code prints nothing for numbers,
    an empty code is General."""
    r = R(5, " ")
    assert r.is_blank and r.kind == "literal" and r.certain
    assert R(0, "   ").certain and R(0, "   ").is_blank and R("x", " ").certain and R(True, " ").certain
    r = R(5, "")
    assert r.text == "5" and r.certain                                         # empty formatCode = General
    assert R(5, 164, custom_formats={164: ""}).certain
    assert R(5, None).certain and R(5, "General").certain and R(5, " general ").certain


def test_review_f10_minus_is_a_sign_of_negative_values_only():
    assert not R(5, '"Year -"0').minus and not R(5, '"FY"-0').minus
    assert R(-5, '"FY"-0').minus and R(-5, '"FY"-0').text == "-FY-5"
    assert R(-5, '0;"Year -"0').minus and R(-5, "0;-0").minus
    assert not R(0, '"FY"-0').minus and not R(5, "-0;0").minus
    assert R(5e-324, "General").text == "5E-324" and R(-5e-324, "General").text == "-5E-324"
    assert N.general_text(2.2250738585072014e-308) == "2.22507E-308"
    assert N.general_text(9.999995e-5) == "0.0001" or N.general_text(9.999995e-5) == "1E-04"

def test_excel_session_2026_10_03_q4_q8():
    """Excel measurements by Patrick (excel_session_answers.md Q4 / Q8), all certain."""
    assert R(True, ";;;").is_blank and R(True, ";;;").certain                  # TRUE under ;;; -> blank
    r = R(True, '0;-0;0;"txt"')
    assert r.text == "txt" and r.kind == "bool" and r.certain                    # booleans use the text section
    r = R(True, '0;-0;0;[Blue]@')
    assert r.text == "TRUE" and r.color_rgb == "0000FF" and r.certain           # ... and its colour
    r = R(True, "[Red]0")
    assert r.text == "TRUE" and r.color_rgb is None and r.certain               # numeric colour not on logicals
    r = R("abc", "[Red]0")
    assert r.color_rgb is None and r.certain                                    # nor on text
    r = R(-5, "[<0]0;0")
    assert r.text == "5" and r.certain                                          # condition chose it: no minus
    r = R(-5, "[>0]0;0")
    assert r.text == "5" and r.certain                                          # fallback: no minus
    r = R(-5, '[>100]0;[<-100]0;0.0')
    assert r.text == "5.0" and r.certain                                        # 3rd (fallback) section: no minus
    assert R(123, " ").is_blank and R(123, " ").certain                         # one-space code: blank
    assert R(123, "").text == "123" and R(123, "").certain                      # empty code: General
    r = R(50000, "[h]:mm")
    assert r.text == "1200000:00" and r.certain                                 # large elapsed hours
    r = R(N.ErrorValue("#N/A"), "[White]General")
    assert r.text == "#N/A" and r.color_rgb is None                             # errors unformatted (Q4)
    assert R(5, "[White]General").color_rgb == "FFFFFF"


def test_show_zeros_off_zero_section():
    """Excel 2026-10-03 (Q5): with showZeros=0, General and single-section formats hide a zero;
    a format with an explicit zero section still prints it; conditional formats unmeasured."""
    z = N.zero_hidden_by_show_zeros_off
    assert z("General") is True and z("0.00") is True and z(0) is True and z("@") is True
    assert z('#,##0;(#,##0);"-"') is False and z("0.00;-0.00;0.00") is False
    assert z('0;-0;"nil"') is False and z('General;General;"zero"') is False and z("0;-0;;@") is False
    assert z("0;(0)") is True                                                   # no zero section of its own
    assert z('[=0]"-";0') is None and z('[>0]0;[<0]-0;0') is None


# --------------------------------------------------------------------------- runner
def test_mixed_date_letters_unverified():
    """66-S3 in the core (2026-10-04): a section that mixes a number / text placeholder (0 # ? outside a
    fractional-seconds group, General, @) with unquoted date-time letters (y m d h s, e g b, AM/PM, A/P,
    [h] [m] [s]) makes the whole code unverified: every render under it is certain=False (Excel's reading is
    unmeasured; openpyxl writes such codes, Excel's dialog does not).  Genuine date / time codes, scientific
    notation and quoted / escaped units are unaffected."""
    mixed = {"0 bps": "0 with b, s", "0 days": "0 with d, y, s", "0 yrs": "0 with y, s", "0 mths": "0 with m, h, s",
             "#,##0 d": "#0 with d", "0.0 d": "0 with d", "? d": "? with d", "0 b": "0 with b", "0 e": "0 with e",
             "0.0E": "0 with E", "General d": "General with d", "@ days": "@ with d, y, s", "0 AM/PM": "0 with AM/PM",
             "[h] 0": "0 with [h]", "d.00": "0 with d", "#,##0 USD": "#0 with s, d", "0.0 km": "0 with m",
             "0 sec": "0 with s, e", "0;0 days": "0 with d, y, s", '"x"0 hh': "0 with hh"}
    for code, why in mixed.items():
        f = N.parse_format(code)
        assert not f.verified and why in (f.unverified_why or ""), (code, f.unverified_why)
        assert N.mixed_date_letters(f.sections[-1].tokens) is not None or code == "0;0 days", code
        for v in (0, 25, -3.5):
            assert not R(v, code).certain, (code, v)
        # text and logicals: shown as typed when the code has no text section, whatever Excel makes of it
        for v in ("txt", True):
            assert R(v, code).certain == (N.parse_format(code).text_index is None), (code, v)
        assert R(None, code).certain and R(N.ErrorValue("#N/A"), code).certain
    # a clean first section does not rescue the code: Excel may reject the whole code
    assert not R(5, "0;0 days").certain and R(5, "0;0 days").text == "5"
    assert R("abc", "0 days;@").certain is False and R("abc", "0 days").certain is True     # text section / none
    genuine = ["h:mm:ss.000", "[h]:mm:ss.00", "mm:ss.0", "[ss].00", "[mm]:ss.0", "d-mmm-yy", 'dd-mmm-yyyy" SUP"',
               '"FY"yyyy"E PV"', "mm/dd/yy\\E", "[h]:mm", "m/d/yyyy h:mm AM/PM", "0.00E+00", "##0.0E+0", "0.0e-0",
               '0" bps"', '0 "days"', "0.0x", '#,##0" d"', "0\\A", '"FY"0"E"', "General", "@", "0;-0;;@",
               "dd/mm/yyyy;@", "[$-409]mmmm d, yyyy;@", '_(* #,##0_);_(* \\(#,##0\\);_(* "-"_);_(@_)', ";;;", "",
               '[$€-407]#,##0.00', "#,##0.00\\ [$kr-41D]"]
    for code in genuine + list(TOY_FORMATS) + [N.builtin_format(i) for i in N.BUILTIN_FORMATS]:
        f = N.parse_format(code)
        assert f.verified and f.unverified_why is None, (code, f.unverified_why)
    assert R(0, "mm:ss.0").certain and R(0.5, "h:mm:ss.000").certain and R(1234.5, "0.00E+00").certain
    # the rendering itself is unchanged (still read as a date; only the certainty flag moves)
    assert R(25, "0 days").kind == "date" and R(25, "#,##0 d").kind == "date"


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
