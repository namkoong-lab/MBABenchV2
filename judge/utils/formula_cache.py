"""Uncached-formula pre-flight (Class E of the 2026-09-01 evidence sweep).

The judge reads cached formula *results*, never live ones: extraction loads the
workbook twice (`data_only=False` for formulas, `data_only=True` for values) and
`_get_formatted_value` returns the empty string whenever the cached value is
``None``. A workbook saved without calculation — the classic openpyxl-produced
attempt — therefore reaches the judge as formulas with no values anywhere:

    [B4]1,234|FORMULA:=SUM(B1:B3)     <- calculated, value visible
    |FORMULA:=SUM(B1:B3)              <- never calculated, value gone

Nothing downstream notices. The Accuracy category (19% of the score, including
the single 10.36-point `Final calculation accuracy` check) then has no evidence
behind it, and the grade that comes out looks exactly like a real one. Under a
strictness rule ("undecided is a fail") it becomes a mechanical Accuracy zero —
scoring the attempt's *save path* rather than its quality.

This module refuses that grading instead. It censuses the extracted CSVs rather
than the workbooks, which means it (a) measures precisely what the judge will
read, and (b) still works when the CSVs came from cache and no workbook was
opened at all.

Enforcement is wired in `judge._prepare_case`, so every mode inherits it —
classic, agentic, and any future single-pass path. Set
``JUDGE_SKIP_FORMULA_CACHE_CHECK=1`` to grade anyway; the skip is recorded in
``scored_results.formula_cache`` so such rows stay identifiable.

The fix, when it fires, is to re-extract with ``--run-calculation`` (a
LibreOffice recalculation pass), or to repair the source workbook.
"""

from pathlib import Path

try:
    from .logger import logger
    from .misc_utils import load_env_var
except ImportError:  # imported as a bare module (utils/ on sys.path)
    from logger import logger
    from misc_utils import load_env_var

SKIP_ENV = "JUDGE_SKIP_FORMULA_CACHE_CHECK"
DEFAULT_MAX_RATIO = 0.5

# A formula cell is uncached iff its display half is blank. Splitting on the
# first '|' is safe for that question specifically: display text may itself
# contain '|', but then the head is non-empty either way, which is the only
# thing being asked. Whitespace-only heads count as blank because
# `create_enhanced_cell_variants` tests `display_value.strip()`.
_FORMULA_MARKER = "FORMULA:"


class FormulaCacheError(Exception):
    """A workbook's formulas carry no cached values; grading it is meaningless."""


def skip_requested() -> bool:
    import os

    return os.environ.get(SKIP_ENV) == "1"


def max_ratio() -> float:
    """Uncached fraction at or above which a workbook is refused."""
    raw = load_env_var("JUDGE_UNCACHED_FORMULA_MAX_RATIO", default=DEFAULT_MAX_RATIO)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"  Invalid JUDGE_UNCACHED_FORMULA_MAX_RATIO {raw!r}; "
            f"using {DEFAULT_MAX_RATIO}"
        )
        return DEFAULT_MAX_RATIO


def _census_field(field: str) -> str | None:
    """Classify one CSV field: 'cached', 'uncached', or None (not a formula)."""
    if _FORMULA_MARKER not in field:
        return None
    head = field.split("|", 1)[0]
    return "cached" if head.strip() else "uncached"


def census_csv_dir(csv_dir) -> dict:
    """Count formula cells and uncached formula cells across a workbook's CSVs.

    Reads the `*_full.csv` views only — they are the canonical encoding and are
    present even in pre-revision caches that lack the `*_data.csv` siblings.
    """
    import csv as csv_mod

    result = {"sheets": {}, "formula_cells": 0, "uncached_formula_cells": 0}
    if not csv_dir:
        return result
    directory = Path(csv_dir)
    if not directory.is_dir():
        return result

    for path in sorted(directory.glob("*_full.csv")):
        formulas = uncached = 0
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                for row in csv_mod.reader(f):
                    for field in row:
                        kind = _census_field(field)
                        if kind is None:
                            continue
                        formulas += 1
                        if kind == "uncached":
                            uncached += 1
        except (OSError, UnicodeDecodeError, csv_mod.Error) as e:
            # A sheet we cannot read is not evidence of a cache problem; say so
            # rather than silently scoring it as clean.
            logger.warning(f"  [formula_cache] could not census {path.name}: {e}")
            result["sheets"][path.name] = {"unreadable": True}
            continue
        if formulas:
            result["sheets"][path.name] = {
                "formula_cells": formulas,
                "uncached_formula_cells": uncached,
            }
        result["formula_cells"] += formulas
        result["uncached_formula_cells"] += uncached

    result["ratio"] = (
        result["uncached_formula_cells"] / result["formula_cells"]
        if result["formula_cells"]
        else 0.0
    )
    return result


def _describe(label: str, census: dict) -> str:
    return (
        f"{label}: {census['uncached_formula_cells']} of "
        f"{census['formula_cells']} formula cells have no cached value "
        f"({census['ratio'] * 100:.0f}%)"
    )


def check_case(attempt_csv_dir, solution_csv_dir=None) -> dict:
    """Census both workbooks and refuse the grading if either is uncalculated.

    Returns provenance for `scored_results.formula_cache`. Raises
    FormulaCacheError when a census is at or above the configured ratio and the
    escape hatch is not set.
    """
    provenance = {
        "checked": True,
        "max_ratio": max_ratio(),
        "attempt": census_csv_dir(attempt_csv_dir),
        "solution": census_csv_dir(solution_csv_dir),
    }

    offenders = [
        (label, provenance[label])
        for label in ("attempt", "solution")
        if provenance[label]["formula_cells"]
        and provenance[label]["ratio"] >= provenance["max_ratio"]
    ]
    provenance["offenders"] = [label for label, _ in offenders]

    for label in ("attempt", "solution"):
        c = provenance[label]
        if c["formula_cells"]:
            logger.info(f"  [formula_cache] {_describe(label, c)}")

    if not offenders:
        return provenance

    detail = "; ".join(_describe(label, c) for label, c in offenders)
    if skip_requested():
        provenance["skipped_via_env"] = True
        logger.warning(
            f"  [formula_cache] {SKIP_ENV}=1 — grading anyway despite: {detail}. "
            f"Accuracy verdicts from this grading are not evidence-based."
        )
        return provenance

    raise FormulaCacheError(
        f"{detail}. The judge reads cached results, so these formulas reach it "
        f"with no values and Accuracy cannot be graded from evidence. "
        f"Re-run with --run-calculation to recalculate through LibreOffice, or "
        f"repair the source workbook. Set {SKIP_ENV}=1 to grade anyway "
        f"(recorded in scored_results.formula_cache)."
    )
