"""Harness vs LLM on the answer-accuracy checks, per grading (judge v6).

Answers "what did the LLM judge say about answer accuracy, and what did the
harness's Python checker say?" for gradings already in the database, or for
local run folders that were not written to the DB (smoke tests).

    # latest 20 single-pass v6 gradings in the DB
    python operation_scripts/report_accuracy_engine.py --benchmark v2 --latest 20

    # specific grading ids
    python operation_scripts/report_accuracy_engine.py --benchmark v2 --grading-ids 205 206

    # local, un-written runs (each dir is a grade_runs/<run_id> folder or a
    # judge_results folder containing scores.json)
    python operation_scripts/report_accuracy_engine.py --local scratch/grade_runs/20260902_1530*

Read-only. One row per grading:
  id | attempt | grader (effort) | harness | LLM | agree | answers | total LLM | total harness | in DB
"harness"/"LLM" are the pass/fail decisions on `Accuracy / Final calculation
accuracy`; "answers" is matched/total Questions-sheet answers; the two totals
are the 0-100 grades under each engine; "in DB" says which engine's total the
row's total_score is.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

_judge_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_judge_root))

from utils.misc_utils import add_benchmark_arg, get_db_url, load_project_configs  # noqa: E402

FA = "Accuracy/Final calculation accuracy"


def _row_from_scored(gid, attempt_id, grader, effort, scored, in_db=True):
    scored = scored or {}
    eng = scored.get("accuracy_engine") or {}
    fa = (eng.get("checks") or {}).get(FA) or {}
    ac = scored.get("answer_check") or {}
    n_q = fa.get("n_questions", ac.get("n_questions"))
    n_m = fa.get("n_match", ac.get("n_match"))
    harness = fa.get("decision") if fa.get("engine") == "harness" else f"n/a ({(fa.get('fallback_reason') or 'not measured')[:28]})"
    return {
        "id": gid,
        "attempt": attempt_id,
        "grader": f"{grader} ({effort})" if effort else str(grader),
        "harness": harness,
        "llm": fa.get("llm_decision") or "-",
        "agree": {True: "yes", False: "NO", None: "-"}.get(fa.get("agreed")),
        "answers": f"{n_m}/{n_q}" if n_q is not None else "-",
        "total_llm": eng.get("total_score_llm"),
        "total_harness": eng.get("total_score_harness"),
        "in_db": (eng.get("effective") or "-") if in_db else "not written",
        "total": scored.get("total_score"),
    }


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _print(rows):
    cols = ["id", "attempt", "grader", "harness", "llm", "agree", "answers",
            "total_llm", "total_harness", "in_db"]
    heads = ["id", "attempt", "grader (effort)", "harness", "LLM", "agree", "answers",
             "total LLM", "total harness", "in DB"]
    widths = [max(len(h), *(len(_fmt(r[c])) for r in rows)) if rows else len(h)
              for c, h in zip(cols, heads)]
    print(" | ".join(h.ljust(w) for h, w in zip(heads, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(_fmt(r[c]).ljust(w) for c, w in zip(cols, widths)))
    if rows:
        agree = sum(1 for r in rows if r["agree"] == "yes")
        disagree = sum(1 for r in rows if r["agree"] == "NO")
        print(f"\n{len(rows)} grading(s): harness and LLM agree on {agree}, disagree on {disagree}, "
              f"harness could not measure {len(rows) - agree - disagree}.")


def from_db(args):
    import psycopg2
    import psycopg2.extras

    load_project_configs(benchmark=args.benchmark)
    conn = psycopg2.connect(get_db_url())
    conn.set_session(readonly=True)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            has_reasoning = True
            try:
                cur.execute("SELECT grader_reasoning FROM gradings LIMIT 0")
            except Exception:
                conn.rollback()
                has_reasoning = False
            reasoning_col = "grader_reasoning" if has_reasoning else "NULL AS grader_reasoning"
            if args.grading_ids:
                cur.execute(
                    f"SELECT id, attempt_id, grader_model, {reasoning_col}, scored_results, "
                    f"judge_version, deprecated FROM gradings WHERE id = ANY(%s) ORDER BY id",
                    (args.grading_ids,),
                )
            else:
                cur.execute(
                    f"SELECT id, attempt_id, grader_model, {reasoning_col}, scored_results, "
                    f"judge_version, deprecated FROM gradings "
                    f"WHERE judge_version >= 6 AND NOT deprecated ORDER BY id DESC LIMIT %s",
                    (args.latest,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        scored = r["scored_results"]
        if isinstance(scored, str):
            scored = json.loads(scored)
        out.append(_row_from_scored(r["id"], r["attempt_id"], r["grader_model"],
                                    r.get("grader_reasoning"), scored))
    return list(reversed(out)) if not args.grading_ids else out


def from_local(paths):
    out = []
    for pattern in paths:
        for p in sorted(glob.glob(pattern)):
            for scores in sorted(Path(p).rglob("scores.json")):
                meta_p = scores.parent / "_metadata.json"
                meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
                scored = json.loads(scores.read_text())
                folder = scores.parent.parent.name
                attempt = next((seg.split("_")[-1] for seg in folder.split("__") if seg.startswith("attempt_")), folder)
                out.append(_row_from_scored(
                    scores.parent.parent.parent.name[:22], attempt, meta.get("grader_model", "?"),
                    meta.get("judge_reasoning"), scored, in_db=False,
                ))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_benchmark_arg(ap)
    ap.add_argument("--grading-ids", type=int, nargs="+")
    ap.add_argument("--latest", type=int, default=20)
    ap.add_argument("--local", nargs="+", help="run folders / globs with scores.json (no DB)")
    args = ap.parse_args()
    rows = from_local(args.local) if args.local else from_db(args)
    _print(rows)


if __name__ == "__main__":
    main()
