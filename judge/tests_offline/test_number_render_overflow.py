"""Offline: the Excel-style number renderer must never sink an extraction.

A cell holding +inf (or a value near the float ceiling) under a fixed-decimal
format used to raise OverflowError from math.floor inside
_round_half_away_from_zero and abort the whole workbook (coding-fable task 89,
attempt 1014, 2026-09-07). Such cells now fall back to the legacy
'<raw> [FORMAT:<pattern>]' form; ordinary numbers render exactly as before.

Run from judge/:  python tests_offline/test_number_render_overflow.py
"""
import math
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.excel_utils import _get_formatted_value, _render_number_format  # noqa: E402

CFG = (True, 6, 8)  # do_rounding, float_rounding, percentage_rounding


def _pair(value, fmt="#,##0.00"):
    cell = SimpleNamespace(number_format=fmt, value=value, data_type="n")
    data = SimpleNamespace(value=value)
    return cell, data


def test_ordinary_number_still_renders():
    assert _render_number_format(1234.567, "#,##0.00") == "1,234.57"
    cell, data = _pair(-179166.67, r'\$#,##0;"($"#,##0\);\-')
    assert _get_formatted_value(cell, data, _cached_config=CFG) == "($179,167)"


def test_infinity_defers_to_raw_form():
    assert _render_number_format(math.inf, "#,##0.00") is None
    assert _render_number_format(-math.inf, "0") is None
    assert _render_number_format(math.nan, "0.00%") is None
    cell, data = _pair(math.inf)
    out = _get_formatted_value(cell, data, _cached_config=CFG)
    assert "inf" in out and "FORMAT:" in out


def test_float_ceiling_does_not_raise():
    for v in (1e308, -1.7e308, 1e305):
        cell, data = _pair(v, "0.00%")
        out = _get_formatted_value(cell, data, _cached_config=CFG)
        assert "FORMAT:" in out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all number-render overflow tests passed")
