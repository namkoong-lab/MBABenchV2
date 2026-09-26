"""Offline: the `[actual …]` tag on zero-looking cells (judge v11, 2026-09-18).

Run from judge/:  python tests_offline/test_actual_value_tag.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from utils.misc_utils import load_project_configs  # noqa: E402

load_project_configs(benchmark="v2")

from utils import excel_utils, workbook_properties as wp  # noqa: E402

DASH = '#,##0.00;(#,##0.00);"-"'


def test_tag_helper():
    t = excel_utils._actual_value_tag
    # leftovers are tagged under any format
    assert t("0.00", 3.64e-12) == " [actual 3.64e-12]"
    assert t("(0.00)", -9.154064e-10, DASH) == " [actual -9.15e-10]"
    assert t("-0.0", -2.56113708e-08, "General") == " [actual -2.56e-08]"
    assert t("0.00%", 1e-9, "0.00%") == " [actual 1e-09]"
    assert t("$0.00", 1e-7) == " [actual 1e-07]" and t(" 0 ", 1e-7) == " [actual 1e-07]"
    # a small real number shown as zero: tagged only under a dash format (where a zero would be a dash)
    assert t("0.00", 0.0025, DASH) == " [actual 0.0025]"
    assert t("0.00", 0.0025, '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)') == " [actual 0.0025]"
    assert t("0.00", 0.0025, "#,##0.00") == "" and t("0", 0.4, "0") == "" and t("0.00%", 7.72e-06, "0.00%") == ""
    # untouched: exact zeros, non-zero-looking displays, text, booleans, dashes
    assert t("0.00", 0, DASH) == "" and t("-", 0, DASH) == "" and t("-", 1e-9, DASH) == ""
    assert t("10.00", 10) == "" and t("0.05", 0.05) == "" and t("0.00E+00", 0) == ""
    assert t("0", True) == "" and t("0", "0") == "" and t("", 1e-9) == ""


def test_dash_format_detection():
    z = excel_utils._zero_section_is_dash
    assert z(DASH) and z('#,##0.00;\\(#,##0.00\\);\\-') and z('_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)')
    assert z('0.0%;-0.0%;"–"')
    assert not z("#,##0.00") and not z("0") and not z("General") and not z('#,##0;(#,##0)') and not z('#,##0.00;(#,##0.00);0.00')
    assert not z(None) and not z('"1 = On";;"0 = Off"')


def test_end_to_end_views_and_width_fit_unaffected():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Checks"
    ws["A1"], ws["A2"], ws["A3"], ws["A4"], ws["A5"], ws["A6"], ws["A7"], ws["A8"] = 3.64e-12, 0, -2.56113708e-08, 0.0025, 1e-9, 0, 5, 0.0025
    for c in ("A1", "A2", "A7", "A8"):
        ws[c].number_format = DASH
    ws["A3"].number_format = "General"
    ws["A4"].number_format = "0.00"
    ws["A5"].number_format = "0.00%"
    ws["A6"].number_format = "0.00"
    ws["B1"] = "=A1"
    ws["B1"].number_format = DASH
    ws.column_dimensions["A"].width = 5.5      # "0.00" fits; "0.00 [actual 3.64e-12]" would not
    d = Path(tempfile.mkdtemp())
    path = d / "t.xlsx"
    wb.save(path)
    # give the formula a cached value like a recalculated file would
    wbv = openpyxl.load_workbook(path)
    wbv.save(path)
    excel_utils.process_all_worksheets(str(path), d / "out", quiet=True)
    full = (d / "out" / "Checks_full.csv").read_text()
    data = (d / "out" / "Checks_data.csv").read_text()
    for view in (full, data):
        assert "[A1]0.00 [actual 3.64e-12]" in view, view[:300]
        assert "[A2]-" in view and "[A2]- [actual" not in view
        assert "[A3]-0.0 [actual -2.56e-08]" in view or "[A3]-2.56113708e-08" in view, view[:400]
        assert "[A4]0.00|" in view or "[A4]0.00\n" in view or "[A4]0.00," in view     # 0.0025 under #,##0.00: ordinary rounding, no tag
        assert "[A4]0.00 [actual" not in view
        assert "[A8]0.00 [actual 0.0025]" in view                                          # the same value under a dash format: tagged
        assert "[A5]0.00% [actual 1e-09]" in view
        assert "[A6]0.00|" in view or "[A6]0.00\n" in view or "[A6]0.00," in view
        assert "[A6]0.00 [actual" not in view
        assert "[A7]5.00" in view and "[A7]5.00 [actual" not in view
    props = wp.load_properties(d / "out")
    cf = next(s for s in props["sheets"] if s["name"] == "Checks")["column_fit"]
    assert not (cf.get("numeric_overflow") or {}).get("count"), cf   # the tag never counts toward width


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all actual-value tag tests passed")
