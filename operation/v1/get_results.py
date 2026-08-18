#!/usr/bin/env python
"""Build the finished-run results.

One row per (cohort x task): the valid attempt that represents the cohort on
that task, plus its clean grading from the configured judge. Statuses:
complete (attempt + grading), needs_grading (attempt, no grading),
needs_attempt (no valid attempt).

Cohort means are imputed: every cell without a clean grading (needs_attempt or
needs_grading) scores 0, and the denominator is at least the version's task count.

A second file, <version>_results_difficulty_breakdown.json, holds the difficulty
breakdown: per model_label, per tasks.human_difficulty_measure bucket, the task
count and the mean accuracy / formula / format / composite, imputed the same way
(ungraded cells score 0 over the full bucket). Each bucket names the tasks it had
to impute under `missing`.

Every run writes both files to its own directory,
operation/results/<version>/<timestamp>/, so runs never overwrite each other;
the timestamp is the run's UTC generated_at as YYYYMMDDTHHMMSSZ. Pass
--run-dir to put them somewhere else, or --out / --difficulty-out to place
either file individually.

Task count and composite weights depend on which version you ask for via
--database; they live in the VERSIONS table. Only v1 is implemented
(composite = 0.5*accuracy + 0.35*formula + 0.15*format over 206 tasks) --
--database v2 exits with a not-implemented message.

Re-run any time; it reads live DB state.

Usage:
    python operation/v1/get_results.py
    python operation/v1/get_results.py --grader anthropic/claude-opus-4-8
    python operation/v1/get_results.py --cohorts claude_web chatgpt_agent
    python operation/v1/get_results.py --run-dir /tmp/run
    python operation/v1/get_results.py --out /tmp/results.json
"""

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

# This file lives at <repo>/operation/v1/get_results.py, so the root is three
# levels up; RESULTS_DIR stays the shared operation/results/ directory, under
# which each run gets its own <version>/<timestamp>/ directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = REPO_ROOT / "operation" / "results"
RUN_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, and safe as a directory name
DEFAULT_GRADER = "google/gemini-2.5-pro"
DEFAULT_DATABASE = "v1"
TASK_SOURCES = ("fmwc", "modeloff", "wsp")

# tasks.human_difficulty_measure buckets, easiest first. Buckets the DB never
# returns are dropped from the breakdown; any unexpected value sorts last.
DIFFICULTY_ORDER = ("VERY-EASY", "EASY", "MEDIUM", "MEDIUM-HARD", "HARD", "VERY-HARD")

# Per-version scoring settings, selected by --database.
#
# expected_tasks  floor on the mean's denominator: every cohort is scored over
#                 the full task set, so a cohort returning fewer cells is
#                 padded with zeros and stays comparable to the rest
# weights         composite rubric weights; must sum to 1
#
# v2 is NOT IMPLEMENTED. Its task set, rubric weights, and cohort roster are
# still being decided, so the unset entries below are deliberate and main()
# refuses to run against it. Filling these in (plus a v2 COHORTS roster and a
# v2 TASK_SOURCES list) is what "implementing v2" means.
VERSIONS: dict[str, dict] = {
    "v1": {
        "expected_tasks": 206,
        "weights": {"accuracy": 0.50, "formula": 0.35, "format": 0.15},
    },
    "v2": {
        "expected_tasks": None,  # TODO(v2): size of the v2 task set
        "weights": None,  # TODO(v2): v2 rubric weights
    },
}


# (label, agent_model_name, prompt_version, pipeline)
#
# label      short name used in the summary + `cohort` field
# identity   task_attempts.agent_model_name
# pv         task_attempts.prompt_version (pinned; see module docstring)
# pipeline   how the agent drove Excel: api | gui | coding_cli
COHORTS: list[tuple[str, str, int, str]] = [
    # --- 2026-07 frontier wave (from make_v1_run_manifest.py) ---------------
    (
        "claude_code_fable_5",
        "claudecode_anthropic/claude-fable-5-max",
        107,
        "coding_cli",
    ),
    ("codex_sol_5.6", "codex_openai/gpt-5.6-sol-xhigh", 107, "coding_cli"),
    ("fable_5_gui", "claude_web_cowork_fable5_max", 9, "gui"),
    ("sol_5.6_gui", "chatgpt_web_work_gpt5.6_sol_ultra", 9, "gui"),
    ("fable_5_cli", "openpyxl_anthropic/claude-fable-5-max", 1105, "api"),
    ("sol_5.6_cli", "openpyxl_openai/gpt-5.6-sol-xhigh", 1105, "api"),
    # --- GUI cohorts (from raw_grade_data_corrected.json) -------------------
    ("claude_web", "claude_web", 9, "gui"),
    ("claude_excel", "claude_excel_agent", 8, "gui"),
    ("chatgpt_excel", "chatgpt_excel_agent", 8, "gui"),
    ("chatgpt_pro", "chatgpt_web_pro", 9, "gui"),
    ("chatgpt_agent", "chatgpt_agent", 9, "gui"),
    # --- API cohorts (from raw_grade_data_corrected.json) -------------------
    ("opus_4_6", "openpyxl_anthropic/claude-opus-4-6", 1105, "api"),
    ("gpt_5_4", "openpyxl_openai/gpt-5.4", 1105, "api"),
    ("gemini_3_1_pro", "openpyxl_google/gemini-3.1-pro-preview", 1105, "api"),
    ("grok_4_2", "openpyxl_x-ai/grok-4.20", 1105, "api"),
    ("kimi_k2_5", "openpyxl_moonshotai/kimi-k2.5", 1105, "api"),
    ("qwen_3_6", "openpyxl_qwen/qwen3.6-plus", 1105, "api"),
    ("olmo_3_1", "openpyxl_allenai/olmo-3.1-32b-instruct", 1105, "api"),
]


# For each task: the highest-id attempt for this (identity, prompt_version)
# that is neither failed nor deprecated, then that attempt's highest-id clean
# grading from the requested grader. Tasks with no valid attempt still come
# back (LEFT JOIN) as needs_attempt rows.
SQL = """
    WITH latest AS (
        SELECT a.task_id, MAX(a.id) AS attempt_id
        FROM task_attempts a
        WHERE a.agent_model_name = %(identity)s
          AND a.prompt_version = %(prompt_version)s
          AND a.agent_failed IS NOT TRUE AND a.deprecated IS NOT TRUE
        GROUP BY a.task_id
    )
    SELECT t.id, t.task_name, t.task_source, t.human_difficulty_measure,
           l.attempt_id, g.id,
           g.accuracy_grade, g.formula_grade, g.format_grade
    FROM tasks t
    LEFT JOIN latest l ON l.task_id = t.id
    LEFT JOIN LATERAL (
        SELECT g.id, g.accuracy_grade, g.formula_grade, g.format_grade
        FROM gradings g
        WHERE g.attempt_id = l.attempt_id AND g.grader_model = %(grader)s
          AND g.failed IS NOT TRUE AND g.deprecated IS NOT TRUE
        ORDER BY g.id DESC LIMIT 1
    ) g ON TRUE
    WHERE t.deprecated IS NOT TRUE
      AND t.task_source = ANY(%(task_sources)s)
    ORDER BY t.id
"""


def load_config() -> dict:
    """Load the merged config via the vendored loader in config/python."""
    config_dir = REPO_ROOT / "config"
    spec = importlib.util.spec_from_file_location(
        "_mbabench_config", config_dir / "python" / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config.load(config_dir).as_dict()


def database_url(args) -> str:
    """Resolve the connection string for the requested version's database."""
    if args.database_url:
        return args.database_url
    key = f"{args.database}_url"
    url = load_config().get("database", {}).get(key)
    if not url:
        sys.exit(f"config/config.yaml has no database.{key} — pass --database-url.")
    return url


def composite(acc, form, fmt, weights) -> float | None:
    if None in (acc, form, fmt):
        return None
    return round(
        weights["accuracy"] * acc + weights["formula"] * form + weights["format"] * fmt,
        5,
    )


def formula_text(weights) -> str:
    """Human-readable composite formula, e.g. '0.5*accuracy + 0.35*formula'."""
    return " + ".join(f"{w:g}*{name}" for name, w in weights.items())


def fetch_cohort(conn, identity: str, prompt_version: int, grader: str):
    with conn.cursor() as cur:
        cur.execute(
            SQL,
            {
                "identity": identity,
                "prompt_version": prompt_version,
                "grader": grader,
                "task_sources": list(TASK_SOURCES),
            },
        )
        return cur.fetchall()


def build(conn, cohorts, grader: str, weights: dict, expected_tasks: int):
    cells, summary = [], {}
    for label, identity, prompt_version, pipeline in cohorts:
        counts = {"complete": 0, "needs_grading": 0, "needs_attempt": 0}
        scores = []
        for (
            task_id,
            task_name,
            task_source,
            difficulty,
            attempt_id,
            grading_id,
            acc,
            form,
            fmt,
        ) in fetch_cohort(conn, identity, prompt_version, grader):
            acc, form, fmt = (
                float(x) if x is not None else None for x in (acc, form, fmt)
            )
            if attempt_id is None:
                status = "needs_attempt"
            elif grading_id is None:
                status = "needs_grading"
            else:
                status = "complete"
            counts[status] += 1
            total = (
                composite(acc, form, fmt, weights) if grading_id is not None else None
            )
            if total is not None:
                scores.append(total)
            cells.append(
                {
                    "cohort": label,
                    "model_label": f"{identity}@{prompt_version}",
                    "agent_model_name": identity,
                    "prompt_version": prompt_version,
                    "pipeline": pipeline,
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_source": task_source,
                    "difficulty": difficulty,
                    "attempt_id": attempt_id,
                    "grading_id": grading_id,
                    "accuracy": acc,
                    "formula": form,
                    "format": fmt,
                    "total": total,
                    "status": status,
                }
            )
        # Ungraded cells count as 0, and a cohort that came back with fewer than
        # expected_tasks cells is padded out to that many zeros.
        cell_count = (
            counts["complete"] + counts["needs_grading"] + counts["needs_attempt"]
        )
        denominator = max(cell_count, expected_tasks)
        counts["scored"] = len(scores)
        counts["imputed_zeros"] = denominator - len(scores)
        counts["mean_total"] = round(sum(scores) / denominator, 2)
        summary[label] = counts
    return cells, summary


def ranking(cohorts, summary):
    """Cohorts ordered by imputed mean, best first (ties broken by label)."""
    pipelines = {label: pipeline for label, _, _, pipeline in cohorts}
    rows = [
        {
            "cohort": label,
            "pipeline": pipelines[label],
            "mean_total": counts["mean_total"],
            "scored": counts["scored"],
            "imputed_zeros": counts["imputed_zeros"],
        }
        for label, counts in summary.items()
    ]
    rows.sort(key=lambda r: (-r["mean_total"], r["cohort"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def difficulty_breakdown(cells) -> dict[str, dict[str, dict]]:
    """Per model_label, per human_difficulty_measure bucket: n and mean grades.

    Imputed the same way as the cohort mean: `n` is every task in the bucket and
    cells without a clean grading (needs_attempt or needs_grading) score 0 on all
    four metrics. Those cells are listed under `missing`, so a bucket dragged
    down by absent work can be traced to the tasks that caused it.
    """
    buckets: dict[str, dict[str, list]] = {}
    for cell in cells:
        label = buckets.setdefault(cell["model_label"], {})
        label.setdefault(cell["difficulty"], []).append(cell)

    def rank(name) -> tuple[int, str]:
        order = (
            DIFFICULTY_ORDER.index(name)
            if name in DIFFICULTY_ORDER
            else len(DIFFICULTY_ORDER)
        )
        return (order, str(name))

    def mean(rows, key) -> float:
        graded = [r[key] for r in rows if r["status"] == "complete"]
        return round(sum(graded) / len(rows), 1)

    def bucket(rows) -> dict:
        missing = [
            {"task_id": r["task_id"], "task_name": r["task_name"], "status": r["status"]}
            for r in rows
            if r["status"] != "complete"
        ]
        out = {
            "n": len(rows),
            "graded": len(rows) - len(missing),
            "imputed_zeros": len(missing),
            "accuracy": mean(rows, "accuracy"),
            "formula": mean(rows, "formula"),
            "format": mean(rows, "format"),
            "total": mean(rows, "total"),
        }
        if missing:
            out["missing"] = missing
        return out

    return {
        model_label: {
            str(name): bucket(rows)
            for name, rows in sorted(by_difficulty.items(), key=lambda kv: rank(kv[0]))
        }
        for model_label, by_difficulty in sorted(buckets.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--grader",
        default=DEFAULT_GRADER,
        help=f"gradings.grader_model to read (default: {DEFAULT_GRADER})",
    )
    parser.add_argument(
        "--cohorts",
        nargs="+",
        metavar="LABEL",
        help="restrict to these cohort labels (default: all)",
    )
    parser.add_argument(
        "--database",
        choices=("v1", "v2"),
        default=DEFAULT_DATABASE,
        help=f"which database.<v1|v2>_url from the config to connect to "
        f"(default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--database-url",
        help="full connection string; bypasses config/config.yaml",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="directory both output files are written to (default: "
        f"{RESULTS_DIR}/<version>/<timestamp>, a fresh directory per run)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="results JSON path; overrides --run-dir for this file "
        "(default: <run-dir>/<version>_results.json)",
    )
    parser.add_argument(
        "--difficulty-out",
        type=Path,
        default=None,
        help="difficulty-breakdown JSON path; overrides --run-dir for this file "
        "(default: <run-dir>/<version>_results_difficulty_breakdown.json)",
    )
    args = parser.parse_args()

    version = VERSIONS[args.database]
    if version["weights"] is None or version["expected_tasks"] is None:
        sys.exit(
            f"--database {args.database} is not implemented yet: {args.database} "
            "still needs its task-set size, rubric weights, and cohort roster. "
            f"See the VERSIONS table in {Path(__file__).name}. Only "
            f"--database {DEFAULT_DATABASE} is supported today."
        )
    # One directory per run, stamped with the same instant the manifests report
    # as generated_at, so a run's files are never overwritten by the next one.
    started = datetime.now(timezone.utc)
    run_dir = args.run_dir or (
        RESULTS_DIR / args.database / started.strftime(RUN_STAMP_FORMAT)
    )
    if args.out is None:
        args.out = run_dir / f"{args.database}_results.json"
    if args.difficulty_out is None:
        args.difficulty_out = (
            run_dir / f"{args.database}_results_difficulty_breakdown.json"
        )

    cohorts = COHORTS
    if args.cohorts:
        known = {c[0] for c in COHORTS}
        unknown = sorted(set(args.cohorts) - known)
        if unknown:
            parser.error(
                f"unknown cohort label(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(known))}"
            )
        cohorts = [c for c in COHORTS if c[0] in set(args.cohorts)]

    conn = psycopg2.connect(database_url(args))
    try:
        cells, summary = build(
            conn, cohorts, args.grader, version["weights"], version["expected_tasks"]
        )
    finally:
        conn.close()

    ranked = ranking(cohorts, summary)
    by_difficulty = difficulty_breakdown(cells)

    generated_at = started.isoformat(timespec="seconds")
    database = args.database if not args.database_url else "(--database-url)"

    def pointer(path: Path, sibling_of: Path) -> str:
        """Bare filename when the two files share a directory, else full path."""
        return path.name if path.parent == sibling_of.parent else str(path)

    manifest = {
        "generated_at": generated_at,
        "database": database,
        "grader_model": args.grader,
        "version": args.database,
        "composite_formula": formula_text(version["weights"]),
        "mean_denominator": (
            f"max(cells, {version['expected_tasks']}); ungraded cells impute 0"
        ),
        "task_sources": list(TASK_SOURCES),
        "cohorts": [
            {
                "cohort": label,
                "agent_model_name": identity,
                "prompt_version": pv,
                "pipeline": pipeline,
            }
            for label, identity, pv, pipeline in cohorts
        ],
        "total_cells": len(cells),
        "summary": summary,
        "ranking": ranked,
        "difficulty_breakdown_file": pointer(args.difficulty_out, args.out),
        "cells": cells,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=1))

    breakdown = {
        "generated_at": generated_at,
        "database": database,
        "grader_model": args.grader,
        "version": args.database,
        "composite_formula": formula_text(version["weights"]),
        "bucket_denominator": "every task in the bucket; ungraded cells impute 0",
        "results_file": pointer(args.out, args.difficulty_out),
        "difficulty_breakdown": by_difficulty,
    }
    args.difficulty_out.parent.mkdir(parents=True, exist_ok=True)
    args.difficulty_out.write_text(json.dumps(breakdown, indent=1))

    print(f"wrote {args.out} ({len(cells)} cells, grader={args.grader})")
    print(f"wrote {args.difficulty_out} (difficulty breakdown)")
    for label, counts in summary.items():
        print(
            f"  {label:22} complete={counts['complete']:3d} "
            f"needs_grading={counts['needs_grading']:3d} "
            f"needs_attempt={counts['needs_attempt']:3d} "
            f"zeros={counts['imputed_zeros']:3d} "
            f"mean={counts['mean_total']:5.1f}"
        )

    print(
        f"\nmean composite (0 imputed for ungraded cells, "
        f"n={version['expected_tasks']}):"
    )
    print(f"  {'#':>2}  {'agent':22} {'pipeline':10} {'mean':>6}")
    for row in ranked:
        print(
            f"  {row['rank']:2d}  {row['cohort']:22} {row['pipeline']:10} "
            f"{row['mean_total']:6.1f}"
        )

    seen = [d for d in DIFFICULTY_ORDER if any(d in b for b in by_difficulty.values())]
    print("\nmean composite by human difficulty (0 imputed for ungraded cells):")
    print(f"  {'agent':22} " + " ".join(f"{d:>14}" for d in seen))
    for label, identity, pv, _ in cohorts:
        row = by_difficulty.get(f"{identity}@{pv}", {})
        cells_txt = " ".join(
            f"{row[d]['total']:7.1f} (n={row[d]['n']:>3})" if d in row else f"{'-':>14}"
            for d in seen
        )
        print(f"  {label:22} {cells_txt}")

    imputed = [
        (label, d, b["missing"])
        for label, identity, pv, _ in cohorts
        for d, b in by_difficulty.get(f"{identity}@{pv}", {}).items()
        if "missing" in b
    ]
    if imputed:
        print("\nimputed cells (scored 0):")
        for label, d, missing in imputed:
            for m in missing:
                print(
                    f"  {label:22} {d:12} task {m['task_id']:>4} "
                    f"{m['task_name']:40} {m['status']}"
                )


if __name__ == "__main__":
    main()
