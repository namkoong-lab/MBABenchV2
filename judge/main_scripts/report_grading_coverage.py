"""Report grading coverage and per-model average scores against existing
gradings, driven by a plan.yaml from
``judge/operation_scripts/generate_judge_plan.py``.

Read-only — does not call the judge or write to the DB. The plan supplies,
per attempt: the agent identity (model + prompt_version), the intended
judge config (judge_model, judge_mode → agentic flag, judge_version,
judge_prompt_version, rubric_version), and whether the planner thinks a
matching grading already exists. This script looks up the actual grading
in the DB using the plan's per-row config and reports two tables:
  - Coverage: per-(agent model + prompt_version) expected / found / missing
    / context-reduced / unknown.
  - Performance: per-(agent model + prompt_version) averages
    (accuracy / formula / format) and a weighted total on the 0-100 scale.

Matching predicate (mirrors ``is_already_graded`` in generate_judge_plan.py):
``(attempt_id, grader_model, agentic_mode, judge_version, prompt_version,
rubric_version, grader_reasoning)``.

Plan rows with ``judge_mode == 'unknown'`` cannot be matched (no version
info). They are surfaced in a separate ``unknown`` column.

Gradings where ``solution_context_reduced`` or ``attempt_context_reduced``
is true are excluded from averages by default and counted in ``ctx_red``;
pass ``--include-context-reduced`` to fold them in.

Usage:
    source judge/project_configs.sh

    python judge/main_scripts/report_grading_coverage.py \\
        --plan judge/operation_scripts/judge_plan/0007_.../plan.yaml

    # Different reasoning effort, dump per-attempt CSV.
    python judge/main_scripts/report_grading_coverage.py \\
        --plan path/to/plan.yaml \\
        --reasoning-effort minimal \\
        --output-csv /tmp/coverage.csv

    # Different scoring weights (must sum to 1.0).
    python judge/main_scripts/report_grading_coverage.py \\
        --plan path/to/plan.yaml \\
        --w-accuracy 0.6 --w-formula 0.3 --w-format 0.1
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

import psycopg2
import psycopg2.extras
import yaml

_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

from utils.logger import logger
from utils.misc_utils import load_project_configs

load_project_configs()

# Reuse the DB connection helper from grade_from_db.py without copying it.
import importlib.util


def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gfd = _import_module(
    "_gfd_module", _judge_root / "main_scripts" / "grade_from_db.py"
)
get_db_connection = _gfd.get_db_connection


# ---------------------------------------------------------------------------
# Defaults — overridable via CLI flags.
# ---------------------------------------------------------------------------

DEFAULT_REASONING_EFFORT: str = "minimal"
DEFAULT_WEIGHTS: dict[str, float] = {
    "accuracy": 0.50,
    "formula": 0.35,
    "format": 0.15,
}
DEFAULT_OUTPUT_JSON: Path = _judge_root.parent / "results" / "overall_grade.json"


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def load_plan_rows(plan_path: Path) -> list[dict]:
    """Return every plan row as a flat dict, preserving the attempt_id key.

    Unlike load_and_validate_plan() in grade_with_orchestration.py, this
    keeps graded=true rows (they are the ones we want to score) and
    unknown-mode rows (surfaced separately in the report).
    """
    with open(plan_path) as f:
        data = yaml.safe_load(f)
    entries = (data or {}).get("attempt_grading_plan") or []
    if not entries:
        raise SystemExit(f"Plan {plan_path} has no attempt_grading_plan entries.")

    rows: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise SystemExit(
                f"Plan entry #{i} is malformed (expected single-key dict): {entry!r}"
            )
        ((aid, info),) = entry.items()
        info = dict(info)
        info["attempt_id"] = int(aid)
        rows.append(info)
    return rows


# ---------------------------------------------------------------------------
# Gradings query + matching
# ---------------------------------------------------------------------------


def fetch_gradings_with_scores(conn, attempt_ids: list[int]) -> list[dict]:
    if not attempt_ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, attempt_id, grader_model, agentic_mode,
                   judge_version, prompt_version, rubric_version,
                   grader_reasoning,
                   accuracy_grade, formula_grade, format_grade,
                   solution_context_reduced, attempt_context_reduced,
                   created_at
            FROM gradings
            WHERE attempt_id = ANY(%s)
              AND failed = false
              AND deprecated = false
            """,
            (attempt_ids,),
        )
        return [dict(r) for r in cur.fetchall()]


def _norm_reasoning(v) -> str:
    return "" if v is None else str(v)


def find_latest_matching(
    rows: list[dict],
    attempt_id: int,
    judge_model: str,
    agentic: bool,
    judge_version: int,
    prompt_version: str,
    rubric_version: str,
    reasoning_effort: str,
) -> dict | None:
    target = _norm_reasoning(reasoning_effort)
    matches = [
        r for r in rows
        if r["attempt_id"] == attempt_id
        and r["grader_model"] == judge_model
        and bool(r["agentic_mode"]) == bool(agentic)
        and int(r["judge_version"]) == int(judge_version)
        and str(r["prompt_version"]) == str(prompt_version)
        and str(r["rubric_version"]) == str(rubric_version)
        and _norm_reasoning(r.get("grader_reasoning")) == target
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return matches[0]


def fetch_task_metadata(conn, task_ids: list[int]) -> dict[int, dict]:
    """Map task_id -> {task_source, human_difficulty_measure, fmwc_difficulty,
    case_classification}. Used for grouping performance by source / difficulty
    / category in the JSON report.
    """
    unique_ids = sorted({int(t) for t in task_ids if t is not None})
    if not unique_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_source, human_difficulty_measure, fmwc_difficulty,
                   case_classification
            FROM tasks
            WHERE id = ANY(%s)
            """,
            (unique_ids,),
        )
        return {int(r["id"]): dict(r) for r in cur.fetchall()}


def _normalize_involved_weights(involved) -> dict[str, float]:
    """Mirror plot_both_cat_difficulty._normalize_weights: dicts pass through;
    lists become equal-share dicts.
    """
    if isinstance(involved, dict):
        return {k: float(v or 0) for k, v in involved.items()}
    if isinstance(involved, list) and involved:
        share = 1.0 / len(involved)
        return {name: share for name in involved}
    return {}


def primary_category(case_classification) -> str | None:
    """Return the parent task category with the highest weight in
    case_classification.involved_tasks. Matches the parent-level taxonomy
    from judge/experiment_scripts/plot_both_cat_difficulty.py.
    """
    if not isinstance(case_classification, dict):
        return None
    weights = _normalize_involved_weights(case_classification.get("involved_tasks"))
    if not weights:
        return None
    return max(weights.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def model_label(plan_row: dict) -> str:
    info = plan_row.get("attempt_additional_info") or {}
    name = info.get("agent_model_name") or "<unknown_agent>"
    pv = info.get("prompt_version")
    return f"{name}@{pv}" if pv is not None else name


def plan_row_task_id(plan_row: dict):
    info = plan_row.get("attempt_additional_info") or {}
    return info.get("task_id")


def build_report(
    plan_rows: list[dict],
    gradings: list[dict],
    reasoning_effort: str,
    weights: dict[str, float],
    include_context_reduced: bool,
) -> dict:
    per_model: dict[str, dict] = defaultdict(
        lambda: {
            "found": [],
            "missing": [],
            "context_reduced": [],
            "unknown": [],
            "expected": 0,
        }
    )

    for r in plan_rows:
        label = model_label(r)
        bucket = per_model[label]
        bucket["expected"] += 1
        attempt_id = r["attempt_id"]
        mode = r.get("judge_mode")

        if mode not in ("standard", "agentic"):
            bucket["unknown"].append(attempt_id)
            continue

        agentic = mode == "agentic"
        match = find_latest_matching(
            gradings,
            attempt_id=attempt_id,
            judge_model=r["judge_model"],
            agentic=agentic,
            judge_version=int(r["judge_version"]),
            prompt_version=str(r["judge_prompt_version"]),
            rubric_version=str(r["rubric_version"]),
            reasoning_effort=reasoning_effort,
        )
        if match is None:
            bucket["missing"].append(attempt_id)
            continue
        is_reduced = bool(match["solution_context_reduced"]) or bool(
            match["attempt_context_reduced"]
        )
        if is_reduced and not include_context_reduced:
            bucket["context_reduced"].append(attempt_id)
            continue
        bucket["found"].append(
            {
                "attempt_id": attempt_id,
                "task_id": plan_row_task_id(r),
                "judge_mode": mode,
                "grading_id": match["id"],
                "accuracy": float(match["accuracy_grade"]),
                "formula": float(match["formula_grade"]),
                "format": float(match["format_grade"]),
                "context_reduced": is_reduced,
            }
        )

    summary: dict[str, dict] = {}
    for label, b in per_model.items():
        if b["found"]:
            avg_acc = mean(r["accuracy"] for r in b["found"])
            avg_formula = mean(r["formula"] for r in b["found"])
            avg_format = mean(r["format"] for r in b["found"])
            total = (
                weights["accuracy"] * avg_acc
                + weights["formula"] * avg_formula
                + weights["format"] * avg_format
            )
        else:
            avg_acc = avg_formula = avg_format = total = None
        summary[label] = {
            "expected": b["expected"],
            "n_found": len(b["found"]),
            "n_missing": len(b["missing"]),
            "n_context_reduced": len(b["context_reduced"]),
            "n_unknown": len(b["unknown"]),
            "missing_attempt_ids": b["missing"],
            "context_reduced_attempt_ids": b["context_reduced"],
            "unknown_attempt_ids": b["unknown"],
            "avg_accuracy": avg_acc,
            "avg_formula": avg_formula,
            "avg_format": avg_format,
            "total_score": total,
            "rows": b["found"],
        }
    return summary


def build_enriched_found_rows(
    plan_rows: list[dict],
    gradings: list[dict],
    task_metadata: dict[int, dict],
    reasoning_effort: str,
    include_context_reduced: bool,
) -> list[dict]:
    """Walk plan rows, find each row's matching grading, and attach per-task
    metadata (source / difficulty / primary category). Used by both the JSON
    report and the per-model x difficulty terminal table.
    """
    found_rows: list[dict] = []
    for r in plan_rows:
        attempt_id = r["attempt_id"]
        mode = r.get("judge_mode")
        if mode not in ("standard", "agentic"):
            continue
        agentic = mode == "agentic"
        match = find_latest_matching(
            gradings,
            attempt_id=attempt_id,
            judge_model=r["judge_model"],
            agentic=agentic,
            judge_version=int(r["judge_version"]),
            prompt_version=str(r["judge_prompt_version"]),
            rubric_version=str(r["rubric_version"]),
            reasoning_effort=reasoning_effort,
        )
        if match is None:
            continue
        is_reduced = bool(match["solution_context_reduced"]) or bool(
            match["attempt_context_reduced"]
        )
        if is_reduced and not include_context_reduced:
            continue
        task_id = plan_row_task_id(r)
        meta = task_metadata.get(int(task_id), {}) if task_id is not None else {}
        found_rows.append(
            {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "model_label": model_label(r),
                "task_source": meta.get("task_source"),
                "human_difficulty": meta.get("human_difficulty_measure"),
                "fmwc_difficulty": meta.get("fmwc_difficulty"),
                "primary_category": primary_category(meta.get("case_classification")),
                "accuracy": float(match["accuracy_grade"]),
                "formula": float(match["formula_grade"]),
                "format": float(match["format_grade"]),
            }
        )
    return found_rows


def build_json_report(
    found_rows: list[dict],
    weights: dict[str, float],
) -> dict:
    """JSON-friendly performance breakdowns.

    Top-level keys:
      - overall: aggregate over every found grading.
      - by_model / by_human_difficulty / by_fmwc_difficulty / by_source /
        by_category: single-axis breakdowns.
      - by_model_x_*: model crossed with each of the other dimensions.

    Each leaf bucket reports ``n`` (sample size) plus avg accuracy / formula /
    format and a weighted ``total_score`` on the same 0-100 scale as the text
    report. Categories are picked per-task as the highest-weight parent in
    ``case_classification.involved_tasks``.
    """

    def _aggregate(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        avg_acc = mean(r["accuracy"] for r in rows)
        avg_formula = mean(r["formula"] for r in rows)
        avg_format = mean(r["format"] for r in rows)
        total = (
            weights["accuracy"] * avg_acc
            + weights["formula"] * avg_formula
            + weights["format"] * avg_format
        )
        return {
            "n": len(rows),
            "avg_accuracy": avg_acc,
            "avg_formula": avg_formula,
            "avg_format": avg_format,
            "total_score": total,
        }

    def _bucket(rows: list[dict], key: str) -> dict:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            v = r.get(key)
            buckets["<unknown>" if v is None else str(v)].append(r)
        return {k: _aggregate(v) for k, v in sorted(buckets.items())}

    def _cross(rows: list[dict], outer: str, inner: str) -> dict:
        buckets: dict[str, dict[str, list[dict]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for r in rows:
            ko = r.get(outer)
            ki = r.get(inner)
            buckets["<unknown>" if ko is None else str(ko)][
                "<unknown>" if ki is None else str(ki)
            ].append(r)
        return {
            ko: {ki: _aggregate(v) for ki, v in sorted(inner_d.items())}
            for ko, inner_d in sorted(buckets.items())
        }

    return {
        "overall": _aggregate(found_rows),
        "by_model": _bucket(found_rows, "model_label"),
        "by_human_difficulty": _bucket(found_rows, "human_difficulty"),
        "by_fmwc_difficulty": _bucket(found_rows, "fmwc_difficulty"),
        "by_source": _bucket(found_rows, "task_source"),
        "by_category": _bucket(found_rows, "primary_category"),
        "by_model_x_human_difficulty": _cross(
            found_rows, "model_label", "human_difficulty"
        ),
        "by_model_x_fmwc_difficulty": _cross(
            found_rows, "model_label", "fmwc_difficulty"
        ),
        "by_model_x_source": _cross(found_rows, "model_label", "task_source"),
        "by_model_x_category": _cross(found_rows, "model_label", "primary_category"),
    }


def _fmt_pct(found: int, expected: int) -> str:
    if expected == 0:
        return "—"
    return f"{100 * found / expected:.1f}%"


def _fmt_score(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def plan_config_summary(plan_rows: list[dict]) -> dict:
    """Compact summary of which judge configs the plan represents."""
    judge_models = sorted({r.get("judge_model") for r in plan_rows if r.get("judge_model")})
    rubric_versions = sorted({str(r.get("rubric_version")) for r in plan_rows if r.get("rubric_version") is not None})
    modes = defaultdict(int)
    for r in plan_rows:
        modes[r.get("judge_mode") or "unknown"] += 1
    judge_versions: dict[str, set] = defaultdict(set)
    for r in plan_rows:
        m = r.get("judge_mode")
        if m in ("standard", "agentic"):
            judge_versions[m].add(
                (int(r["judge_version"]), str(r["judge_prompt_version"]))
            )
    return {
        "judge_models": judge_models,
        "rubric_versions": rubric_versions,
        "mode_counts": dict(modes),
        "version_pairs_per_mode": {
            m: sorted(list(v)) for m, v in judge_versions.items()
        },
    }


DIFFICULTY_ORDER: dict[str, list[str]] = {
    "human_difficulty": [
        "VERY-EASY", "EASY", "MEDIUM", "MEDIUM-HARD", "HARD", "VERY-HARD",
    ],
    "fmwc_difficulty": ["EASY", "MEDIUM", "HARD"],
}


def _bucket_total(
    rows: list[dict], weights: dict[str, float]
) -> tuple[float | None, int]:
    """Return (weighted_total_score, n) for a list of enriched found rows."""
    if not rows:
        return None, 0
    avg_acc = mean(r["accuracy"] for r in rows)
    avg_formula = mean(r["formula"] for r in rows)
    avg_format = mean(r["format"] for r in rows)
    total = (
        weights["accuracy"] * avg_acc
        + weights["formula"] * avg_formula
        + weights["format"] * avg_format
    )
    return total, len(rows)


def print_difficulty_breakdown(
    found_rows: list[dict],
    weights: dict[str, float],
    difficulty_field: str = "human_difficulty",
) -> None:
    """Per-model average total_score broken down by difficulty bucket. Each
    cell shows ``score (n)``; a header line above the table lists the count
    of distinct tasks per difficulty across all models.
    """
    if not found_rows:
        print(
            f"Per-model average total_score by {difficulty_field}: "
            "(no found gradings)"
        )
        return

    canonical = DIFFICULTY_ORDER.get(difficulty_field, [])
    appearing = {r.get(difficulty_field) for r in found_rows}
    appearing.discard(None)
    ordered = [d for d in canonical if d in appearing]
    extras = sorted(d for d in appearing if d not in canonical)
    diffs = ordered + extras
    if not diffs:
        print(
            f"Per-model average total_score by {difficulty_field}: "
            "(no difficulty data)"
        )
        return

    tasks_per_diff: dict[str, set] = {d: set() for d in diffs}
    for r in found_rows:
        d = r.get(difficulty_field)
        if d in tasks_per_diff and r.get("task_id") is not None:
            tasks_per_diff[d].add(r["task_id"])

    by_model_diff: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in found_rows:
        d = r.get(difficulty_field)
        if d in tasks_per_diff:
            by_model_diff[r["model_label"]][d].append(r)

    print(
        f"Per-model average total_score by {difficulty_field} (n in parens):"
    )
    counts_line = "  ".join(f"{d}={len(tasks_per_diff[d])}" for d in diffs)
    print(f"  Distinct tasks per difficulty (across models): {counts_line}")
    print()

    col_w = max(13, max(len(d) for d in diffs) + 2)
    model_col_w = max(44, max(len(m) for m in by_model_diff.keys()) + 2)
    header = f"  {'model':<{model_col_w}}" + "".join(
        f"{d:>{col_w}}" for d in diffs
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for model in sorted(by_model_diff.keys()):
        line = f"  {model:<{model_col_w}}"
        for d in diffs:
            score, n = _bucket_total(by_model_diff[model].get(d, []), weights)
            cell = "—" if score is None else f"{score:.2f} ({n})"
            line += f"{cell:>{col_w}}"
        print(line)
    print()


# Mirrors PARENT_DISPLAY_NAMES in plot_both_cat_difficulty.py. Categories not
# listed here keep their full name (already short enough for a column header).
CATEGORY_ABBREV: dict[str, str] = {
    "Business and Data Analytics": "Bus.&Data",
    "Deal / Transaction Models": "Deal/M&A",
    "3-Statements Models": "3-Stmt",
    "Debt & Equity Models": "Debt&Eq",
    "Derivatives Pricing": "Deriv.",
    "Wealth Management": "Wealth",
    "Valuation Models": "Valuation",
    "FP&A Models": "FP&A",
}


def _category_abbrev(name: str) -> str:
    return CATEGORY_ABBREV.get(name, name)


def print_category_breakdown(
    found_rows: list[dict], weights: dict[str, float]
) -> None:
    """Per-model average total_score broken down by task primary category
    (highest-weight parent in case_classification.involved_tasks).

    Layout matches the difficulty table — models as rows, abbreviated
    categories as columns. A legend below the title maps the abbreviations
    back to full names + distinct-task counts.
    """
    if not found_rows:
        print("Per-model average total_score by task category: (no found gradings)")
        return

    tasks_per_cat: dict[str, set] = defaultdict(set)
    by_model_cat: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    n_uncat = 0
    for r in found_rows:
        cat = r.get("primary_category")
        if cat is None:
            n_uncat += 1
            continue
        if r.get("task_id") is not None:
            tasks_per_cat[cat].add(r["task_id"])
        by_model_cat[r["model_label"]][cat].append(r)

    cats_present = {cat for d in by_model_cat.values() for cat in d}
    if not cats_present:
        print("Per-model average total_score by task category: (no category data)")
        return

    cats_sorted = sorted(
        cats_present, key=lambda c: (-len(tasks_per_cat[c]), c)
    )
    models = sorted(by_model_cat.keys())

    print("Per-model average total_score by task category (n in parens):")
    print(
        "  Category = highest-weight parent in "
        "case_classification.involved_tasks."
    )
    print("  Column legend (abbrev -> full name [distinct tasks]):")
    for cat in cats_sorted:
        print(
            f"    {_category_abbrev(cat):<10} -> {cat} "
            f"[{len(tasks_per_cat[cat])}]"
        )
    if n_uncat:
        print(f"  Uncategorized rows excluded from table: {n_uncat}")
    print()

    abbrevs = [_category_abbrev(c) for c in cats_sorted]
    col_w = max(13, max(len(a) for a in abbrevs) + 2)
    model_col_w = max(44, max(len(m) for m in models) + 2)
    header = f"  {'model':<{model_col_w}}" + "".join(
        f"{a:>{col_w}}" for a in abbrevs
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for model in models:
        line = f"  {model:<{model_col_w}}"
        for cat in cats_sorted:
            score, n = _bucket_total(by_model_cat[model].get(cat, []), weights)
            cell = "—" if score is None else f"{score:.2f} ({n})"
            line += f"{cell:>{col_w}}"
        print(line)
    print()


def print_report(summary: dict, config: dict, show_missing: bool) -> None:
    bar = "=" * 92
    print(bar)
    print("GRADING COVERAGE REPORT (plan-driven)")
    print(bar)
    print("Config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    print()

    labels = sorted(summary.keys())

    print("Coverage:")
    header = (
        f"  {'model':<44} {'expected':>9} {'found':>6} {'missing':>8} "
        f"{'ctx_red':>8} {'unknown':>8} {'pct_found':>10}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label in labels:
        s = summary[label]
        print(
            f"  {label:<44} {s['expected']:>9} {s['n_found']:>6} "
            f"{s['n_missing']:>8} {s['n_context_reduced']:>8} "
            f"{s['n_unknown']:>8} "
            f"{_fmt_pct(s['n_found'], s['expected']):>10}"
        )
    print()

    print("Performance (averaged over found gradings, scale 0-100):")
    header = (
        f"  {'model':<44} {'n':>4} {'total':>7} {'accuracy':>9} "
        f"{'formula':>8} {'format':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label in labels:
        s = summary[label]
        print(
            f"  {label:<44} {s['n_found']:>4} "
            f"{_fmt_score(s['total_score']):>7} "
            f"{_fmt_score(s['avg_accuracy']):>9} "
            f"{_fmt_score(s['avg_formula']):>8} "
            f"{_fmt_score(s['avg_format']):>7}"
        )
    print()

    if show_missing:
        print("Per-model attempt id breakdown:")
        for label in labels:
            s = summary[label]
            if s["missing_attempt_ids"]:
                print(f"  {label} missing: {s['missing_attempt_ids']}")
            if s["context_reduced_attempt_ids"]:
                print(
                    f"  {label} excluded (context reduced): "
                    f"{s['context_reduced_attempt_ids']}"
                )
            if s["unknown_attempt_ids"]:
                print(
                    f"  {label} unknown judge_mode (cache not warmed): "
                    f"{s['unknown_attempt_ids']}"
                )
        print()


def write_csv(path: Path, summary: dict) -> None:
    cols = [
        "model",
        "task_id",
        "attempt_id",
        "judge_mode",
        "grading_id",
        "accuracy",
        "formula",
        "format",
        "context_reduced",
    ]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for label in sorted(summary.keys()):
            for r in summary[label]["rows"]:
                w.writerow([
                    label,
                    r["task_id"],
                    r["attempt_id"],
                    r["judge_mode"],
                    r["grading_id"],
                    f"{r['accuracy']:.4f}",
                    f"{r['formula']:.4f}",
                    f"{r['format']:.4f}",
                    int(r["context_reduced"]),
                ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_project_configs()

    parser = argparse.ArgumentParser(
        description=(
            "Report grading coverage and per-model average scores against "
            "existing gradings, driven by a plan.yaml. Read-only — does not "
            "run the judge or write to the DB."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help=(
            "Path to a plan.yaml from generate_judge_plan.py. The plan "
            "supplies per-attempt agent identity and judge config "
            "(judge_model / judge_mode / judge_version / "
            "judge_prompt_version / rubric_version)."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high"],
        default=DEFAULT_REASONING_EFFORT,
        help=(
            f"Default: {DEFAULT_REASONING_EFFORT}. The plan does not record "
            f"reasoning effort, so it must be specified here to match the "
            f"gradings of interest."
        ),
    )
    parser.add_argument(
        "--w-accuracy", type=float, default=DEFAULT_WEIGHTS["accuracy"]
    )
    parser.add_argument(
        "--w-formula", type=float, default=DEFAULT_WEIGHTS["formula"]
    )
    parser.add_argument(
        "--w-format", type=float, default=DEFAULT_WEIGHTS["format"]
    )
    parser.add_argument(
        "--include-context-reduced",
        action="store_true",
        help=(
            "Include gradings where solution_context_reduced or "
            "attempt_context_reduced is true. Default: exclude (counted in "
            "the ctx_red column)."
        ),
    )
    parser.add_argument(
        "--show-missing",
        action="store_true",
        help=(
            "Print attempt_ids for missing, context-reduced, and "
            "unknown-mode rows."
        ),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional path to dump per-attempt scores as CSV.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_JSON,
        help=(
            f"Path to dump per-bucket performance breakdowns as JSON "
            f"(default: {DEFAULT_OUTPUT_JSON}). Includes overall plus "
            f"by_model, by_human_difficulty, by_fmwc_difficulty, by_source, "
            f"by_category, and the corresponding model x dimension "
            f"cross-tabulations. Each task's category is the highest-weight "
            f"parent in case_classification.involved_tasks. Pass an empty "
            f"string to skip the JSON write."
        ),
    )

    args = parser.parse_args()

    weights = {
        "accuracy": args.w_accuracy,
        "formula": args.w_formula,
        "format": args.w_format,
    }
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > 1e-6:
        logger.warning(
            f"Weights sum to {weight_sum:.4f}, not 1.0. Total scores will not "
            f"be on a 0-100 scale."
        )

    plan_rows = load_plan_rows(args.plan)
    plan_cfg = plan_config_summary(plan_rows)

    attempt_ids = [r["attempt_id"] for r in plan_rows]
    task_ids = [
        t for t in (plan_row_task_id(r) for r in plan_rows) if t is not None
    ]
    write_json = args.output_json is not None and str(args.output_json) != ""
    conn = get_db_connection()
    try:
        gradings = fetch_gradings_with_scores(conn, attempt_ids)
        task_metadata = fetch_task_metadata(conn, task_ids)
    finally:
        conn.close()

    enriched_found_rows = build_enriched_found_rows(
        plan_rows=plan_rows,
        gradings=gradings,
        task_metadata=task_metadata,
        reasoning_effort=args.reasoning_effort,
        include_context_reduced=args.include_context_reduced,
    )

    summary = build_report(
        plan_rows=plan_rows,
        gradings=gradings,
        reasoning_effort=args.reasoning_effort,
        weights=weights,
        include_context_reduced=args.include_context_reduced,
    )

    config = {
        "plan": str(args.plan),
        "reasoning_effort": args.reasoning_effort,
        "weights": (
            f"accuracy={weights['accuracy']:.2f} "
            f"formula={weights['formula']:.2f} "
            f"format={weights['format']:.2f}"
        ),
        "include_context_reduced": args.include_context_reduced,
        "n_plan_rows": len(plan_rows),
        "plan.judge_models": plan_cfg["judge_models"],
        "plan.rubric_versions": plan_cfg["rubric_versions"],
        "plan.mode_counts": plan_cfg["mode_counts"],
        "plan.version_pairs_per_mode": plan_cfg["version_pairs_per_mode"],
    }
    print_report(summary, config, show_missing=args.show_missing)
    print_difficulty_breakdown(enriched_found_rows, weights)
    print_category_breakdown(enriched_found_rows, weights)

    if args.output_csv:
        write_csv(args.output_csv, summary)
        print(f"Wrote per-attempt CSV to {args.output_csv}")

    if write_json:
        json_report = build_json_report(enriched_found_rows, weights)
        json_report["config"] = config
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w") as f:
            json.dump(json_report, f, indent=2, default=str)
        print(f"Wrote JSON breakdown to {args.output_json}")


if __name__ == "__main__":
    main()
