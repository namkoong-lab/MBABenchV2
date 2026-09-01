"""Offline tests for the uncached-formula pre-flight (utils/formula_cache.py).

Run: python tests_offline/test_formula_cache.py   (from judge/)

The encoding assertions are round-tripped through the real encoder
(`create_enhanced_cell_variants`) with stub cells rather than hand-written
strings, so a change to the cell format breaks these tests instead of silently
breaking the census.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.misc_utils import load_project_configs

load_project_configs()

from utils import formula_cache
from utils.excel_utils import create_enhanced_cell_variants

failures = []


def ok(cond, msg):
    print(f"{'OK ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------- stub cells
class _Color:
    rgb = None
    indexed = None


class _Font:
    name = None
    size = None
    bold = False
    italic = False
    underline = False
    color = None


class _Align:
    horizontal = None
    vertical = None


class _Cell:
    """Minimal stand-in for an openpyxl cell (formula side)."""

    def __init__(self, value, number_format="General"):
        self.value = value
        self.number_format = number_format
        self.font = _Font()
        self.fill = None
        self.alignment = _Align()
        self.border = None


class _DataCell:
    """Minimal stand-in for the data_only side."""

    def __init__(self, value):
        self.value = value


def encode(formula, cached_value):
    """Return the *_full.csv field the real encoder produces for this cell."""
    full, _data = create_enhanced_cell_variants(
        _Cell(formula), _DataCell(cached_value), 4, 2
    )
    return full


# ------------------------------------------------------- encoding round-trip
print("\n[1] The census classifies what the real encoder emits")

cached_field = encode("=SUM(B1:B3)", 1234)
uncached_field = encode("=SUM(B1:B3)", None)
literal_field = encode(1234, 1234)
empty_field = encode(None, None)

ok(
    formula_cache._census_field(cached_field) == "cached",
    f"calculated formula -> cached   ({cached_field!r})",
)
ok(
    formula_cache._census_field(uncached_field) == "uncached",
    f"uncalculated formula -> uncached ({uncached_field!r})",
)
ok(
    formula_cache._census_field(literal_field) is None,
    f"literal value is not a formula cell ({literal_field!r})",
)
ok(
    formula_cache._census_field(empty_field) is None,
    f"empty cell is not a formula cell ({empty_field!r})",
)

# A cached error value is a real result, not a missing one.
err_field = encode("=1/0", "#DIV/0!")
ok(
    formula_cache._census_field(err_field) == "cached",
    f"cached error value counts as cached ({err_field!r})",
)

# Display text containing '|' must not be mistaken for a blank display half.
piped = "[B4]a|b|FORMULA:=T(1)"
ok(
    formula_cache._census_field(piped) == "cached",
    "display text containing '|' still counts as cached",
)
ok(
    formula_cache._census_field("   |FORMULA:=T(1)") == "uncached",
    "whitespace-only display half counts as uncached",
)


# ------------------------------------------------------------------- census
print("\n[2] census_csv_dir counts per sheet and in total")


def write_dir(root, sheets):
    d = Path(root)
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in sheets.items():
        (d / f"{name}_full.csv").write_text(
            "\n".join(",".join(f'"{c}"' for c in row) for row in rows),
            encoding="utf-8",
        )
    return str(d)


with tempfile.TemporaryDirectory() as tmp:
    good = write_dir(
        Path(tmp) / "good",
        {"Calcs": [[encode("=A1+1", 5), encode(3, 3)], [encode("=A2*2", 9), ""]]},
    )
    c = formula_cache.census_csv_dir(good)
    ok(c["formula_cells"] == 2, f"counted 2 formula cells (got {c['formula_cells']})")
    ok(c["uncached_formula_cells"] == 0, "none uncached")
    ok(c["ratio"] == 0.0, "ratio 0.0")

    bad = write_dir(
        Path(tmp) / "bad",
        {"Calcs": [[encode("=A1+1", None), encode("=A2*2", None)]]},
    )
    c = formula_cache.census_csv_dir(bad)
    ok(c["ratio"] == 1.0, f"fully uncalculated workbook -> ratio 1.0 (got {c['ratio']})")
    ok("Calcs_full.csv" in c["sheets"], "per-sheet detail recorded")

    # A workbook with no formulas at all must not trip anything.
    literal = write_dir(Path(tmp) / "lit", {"Data": [[encode(1, 1), encode(2, 2)]]})
    c = formula_cache.census_csv_dir(literal)
    ok(c["formula_cells"] == 0 and c["ratio"] == 0.0, "no formulas -> no signal")

    ok(
        formula_cache.census_csv_dir(None)["formula_cells"] == 0,
        "missing directory censuses to zero rather than raising",
    )

    # ------------------------------------------------------------- check_case
    print("\n[3] check_case refuses, and the escape hatch is recorded")

    prov = formula_cache.check_case(good, good)
    ok(prov["offenders"] == [], "clean case passes")
    ok(prov["checked"] is True and "max_ratio" in prov, "provenance shape")

    raised = None
    try:
        formula_cache.check_case(bad, good)
    except formula_cache.FormulaCacheError as e:
        raised = str(e)
    ok(raised is not None, "uncalculated attempt refuses the grading")
    ok(
        raised and "--run-calculation" in raised,
        "refusal names the fix (--run-calculation)",
    )

    raised = None
    try:
        formula_cache.check_case(good, bad)
    except formula_cache.FormulaCacheError as e:
        raised = str(e)
    ok(raised is not None, "uncalculated SOLUTION also refuses")
    ok(raised and "solution" in raised, "refusal names which workbook failed")

    os.environ[formula_cache.SKIP_ENV] = "1"
    try:
        prov = formula_cache.check_case(bad, good)
        ok(prov.get("skipped_via_env") is True, "escape hatch records skipped_via_env")
        ok(prov["offenders"] == ["attempt"], "offenders recorded even when skipped")
    finally:
        del os.environ[formula_cache.SKIP_ENV]

print("\n" + ("ALL FORMULA-CACHE CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)"))
sys.exit(1 if failures else 0)
