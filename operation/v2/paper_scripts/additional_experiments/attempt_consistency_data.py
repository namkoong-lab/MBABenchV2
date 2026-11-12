#!/usr/bin/env python
"""Frozen snapshot for the attempt-consistency test (read by attempt_consistency.py).

The test: GPT-6 Astra on the 30 Stage 2 tasks, on two harnesses (Codex, the in-house openpyxl
agent), three attempts per task and harness:
  orig   the attempt the leaderboard scores (plot_leaderboard.py: newest clean grading per
         cohort and task, judge_version >= MIN_JUDGE_VERSION; cohort labels pooled)
  r2,r3  the two reruns of 2026-11-11, labels <harness>_tensorblock/gpt-6-astra-xhigh-r2 / -r3
Every attempt carries the one grading that counts (judge v14, GPT-5.6 Sol). The Codex originals
were graded three times in Stage 2; the other two gradings of the same file are kept under
`repeat_gradings` as the judge-only reference (same workbook, three judge passes).

Per grading the snapshot keeps: total score, per-check verdicts (mistakes, grader = llm /
deterministic, weight, score), per-category scores, the answer check's counts, and the
automatic checks' own verdicts (scored_results.det_checks.checks).

The script refuses to write unless the set is complete: 30 tasks x 2 harnesses x 3 attempts,
the four rerun labels on the same 30 tasks with no failed attempt, exactly one live grading
per rerun, every counted grading at judge v14.

Database: select only, one plain transaction, no session-level SET (the URL is a pooler).

Writes operation/results/v2/attempt_consistency/gradings.json (gitignored).

Run from the repo root (psycopg2 only):
    uv run --project judge python operation/v2/paper_scripts/additional_experiments/attempt_consistency_data.py
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plot_difficulty_and_types import database_url  # noqa: E402

OUT = REPO_ROOT / "operation" / "results" / "v2" / "attempt_consistency" / "gradings.json"

JUDGE_VERSION = 14  # the paper's judge; every counted grading must be at this version
MIN_JUDGE_VERSION = 12  # plot_leaderboard.MIN_JUDGE_VERSION, for picking the originals as it does
SOL = "%/gpt-5.6-sol"  # the paper's judge model, through either provider
N_TASKS = 30

# harness -> the leaderboard cohort's labels (style_guide.yaml agents: Astra-Code, Astra) and the rerun labels
HARNESSES = {
    "codex": {
        "orig": ["codex_openai/gpt-6-astra-xhigh"],
        "r2": "codex_tensorblock/gpt-6-astra-xhigh-r2",
        "r3": "codex_tensorblock/gpt-6-astra-xhigh-r3",
    },
    "inhouse": {
        "orig": ["openpyxl_openai/gpt-6-astra-xhigh", "openpyxl_tensorblock/gpt-6-astra-xhigh"],
        "r2": "openpyxl_tensorblock/gpt-6-astra-xhigh-r2",
        "r3": "openpyxl_tensorblock/gpt-6-astra-xhigh-r3",
    },
}

ATTEMPT_COLS = "ta.id, ta.task_id, ta.agent_model_name, ta.agent_failed, ta.time_taken_min, ta.created_at"
GRADING_COLS = "g.id, g.attempt_id, g.judge_version, g.grader_model, g.created_at, g.scored_results"
LIVE = "g.failed is not true and g.deprecated is not true"

SQL_RERUNS = f"""
    select {ATTEMPT_COLS} from task_attempts ta
    where ta.agent_model_name = %(label)s and ta.deprecated is not true
"""
# The leaderboard's row per (label, task): plot_leaderboard.SQL restricted to these labels and tasks.
SQL_ORIGINALS = f"""
    select distinct on (ta.agent_model_name, ta.task_id) {ATTEMPT_COLS}, g.id as grading_id, g.created_at as graded_at
    from gradings g
    join task_attempts ta on ta.id = g.attempt_id
    join tasks t on t.id = ta.task_id
    where g.judge_version >= %(judge)s and {LIVE}
      and ta.deprecated is not true and t.deprecated is not true
      and g.scored_results->>'total_score' is not null
      and ta.agent_model_name = any(%(labels)s) and ta.task_id = any(%(tasks)s)
    order by ta.agent_model_name, ta.task_id, g.created_at desc
"""
SQL_GRADINGS = f"""
    select {GRADING_COLS} from gradings g
    where g.attempt_id = any(%(attempts)s) and {LIVE}
      and g.judge_version = %(judge)s and g.grader_model like %(sol)s
    order by g.attempt_id, g.created_at
"""


def stamp(t) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def attempt_row(r, harness: str, run: str) -> dict:
    aid, task, label, failed, minutes, created = r[:6]
    return {
        "harness": harness,
        "run": run,
        "task_id": task,
        "attempt_id": aid,
        "agent_model_name": label,
        "agent_failed": bool(failed),
        "time_taken_min": minutes,
        "attempt_created_at": stamp(created),
    }


def compact(row) -> dict:
    """One grading -> what the analysis reads."""
    gid, aid, jv, grader, created, sr = row
    if isinstance(sr, str):
        sr = json.loads(sr)
    ac, det = sr.get("answer_check") or {}, sr.get("det_checks") or {}
    return {
        "grading_id": gid,
        "attempt_id": aid,
        "judge_version": jv,
        "grader_model": grader,
        "graded_at": stamp(created),
        "total_score": float(sr["total_score"]),
        "llm_reuse_source": (sr.get("llm_reuse") or {}).get("source_grading_id"),
        "check_scores": {
            cat: {
                name: {k: v.get(k) for k in ("mistakes", "unscored", "grader", "weight", "score")}
                for name, v in checks.items()
            }
            for cat, checks in sr["check_scores"].items()
        },
        "criteria_scores": {
            cat: {k: v.get(k) for k in ("normalized_score", "category_weight", "total_weight", "weighted_sum")}
            for cat, v in (sr.get("criteria_scores") or {}).items()
        },
        "answer_check": {
            k: ac.get(k)
            for k in (
                "status", "n_questions", "n_match", "n_match_alternate", "n_mismatch", "n_missing",
                "n_unlocated", "n_answered", "fraction_correct", "recalc_used", "rules_version",
            )
        },
        "det_checks": {
            "status": det.get("status"),
            "mode": det.get("mode"),
            "code_sha": det.get("code_sha"),
            "lo_version": (det.get("values") or {}).get("lo_version"),
            "not_applicable": det.get("not_applicable"),
            "checks": {
                key: {k: v.get(k) for k in ("check_no", "live", "decision", "n_mistakes")}
                for key, v in (det.get("checks") or {}).items()
            },
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--database-url", default=None, help="default: config database.v2_url")
    args = ap.parse_args()

    conn = psycopg2.connect(database_url(args))
    try:
        cur = conn.cursor()
        attempts: list[dict] = []
        tasks: list[int] | None = None
        for harness, labels in HARNESSES.items():
            for run in ("r2", "r3"):
                cur.execute(SQL_RERUNS, {"label": labels[run]})
                rows = [attempt_row(r, harness, run) for r in cur.fetchall()]
                ids = sorted(a["task_id"] for a in rows)
                assert len(rows) == N_TASKS and len(set(ids)) == N_TASKS, f"{labels[run]}: {len(rows)} attempts on {len(set(ids))} tasks"
                assert not any(a["agent_failed"] for a in rows), f"{labels[run]}: failed attempt"
                assert tasks is None or ids == tasks, f"{labels[run]}: task set differs from the other rerun labels"
                tasks = ids
                attempts += rows
        for harness, labels in HARNESSES.items():
            cur.execute(SQL_ORIGINALS, {"judge": MIN_JUDGE_VERSION, "labels": labels["orig"], "tasks": tasks})
            newest: dict[int, tuple] = {}  # pooled labels: the newest grading wins (plot_leaderboard.fold_cohorts)
            for r in cur.fetchall():
                if r[1] not in newest or r[7] > newest[r[1]][7]:
                    newest[r[1]] = r
            assert sorted(newest) == tasks, f"{harness} originals: {len(newest)} of {N_TASKS} tasks on the leaderboard"
            for r in newest.values():
                attempts.append({**attempt_row(r, harness, "orig"), "leaderboard_grading_id": r[6]})

        cur.execute(SQL_GRADINGS, {"attempts": [a["attempt_id"] for a in attempts], "judge": JUDGE_VERSION, "sol": SOL})
        by_attempt: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            by_attempt.setdefault(row[1], []).append(compact(row))
    finally:
        conn.rollback()
        conn.close()

    for a in attempts:
        gs = by_attempt.get(a["attempt_id"], [])
        if a["run"] == "orig":
            leaderboard_id = a.pop("leaderboard_grading_id")
            counted = [g for g in gs if g["grading_id"] == leaderboard_id]
            assert len(counted) == 1, f"attempt {a['attempt_id']}: the leaderboard's grading is not a live v{JUDGE_VERSION} Sol grading"
        else:
            assert len(gs) == 1, f"attempt {a['attempt_id']}: {len(gs)} live gradings, expected 1"
            counted = gs
        a["grading"] = counted[0]
        a["repeat_gradings"] = [g for g in gs if g is not counted[0]]
    assert len(attempts) == N_TASKS * len(HARNESSES) * 3
    assert len({a["attempt_id"] for a in attempts}) == len(attempts), "an attempt appears twice"

    attempts.sort(key=lambda a: (a["harness"], a["task_id"], a["run"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "meta": {
                    "created": stamp(dt.datetime.now(dt.timezone.utc)),
                    "judge_version": JUDGE_VERSION,
                    "tasks": tasks,
                    "labels": HARNESSES,
                    "source": "v2 database, select only",
                },
                "attempts": attempts,
            },
            indent=1,
        )
    )
    n_rep = sum(len(a["repeat_gradings"]) for a in attempts)
    print(f"{len(attempts)} attempts ({N_TASKS} tasks x {len(HARNESSES)} harnesses x 3), {n_rep} repeat gradings -> {OUT.relative_to(REPO_ROOT)}")
    for harness in HARNESSES:
        for run in ("orig", "r2", "r3"):
            sel = [a for a in attempts if a["harness"] == harness and a["run"] == run]
            graders = sorted({a["grading"]["grader_model"] for a in sel})
            reuse = sum(a["grading"]["llm_reuse_source"] is not None for a in sel)
            print(f"  {harness:8s} {run:4s} n={len(sel)}  reuse rows {reuse:2d}  repeats {sum(len(a['repeat_gradings']) for a in sel):2d}  graders {graders}")


if __name__ == "__main__":
    main()
