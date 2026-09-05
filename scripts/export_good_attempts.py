#!/usr/bin/env python3
"""Export the banked "good attempt" manifest for the MBABenchV2 corpus.

One entry per (pipeline, model, task) for the eight study cohorts:
4 pipelines (gui, api, coding_cli, excel) x 2 models (Claude Fable 5, GPT-5.6 Sol).
A good attempt = task_attempts row that is not deprecated and not agent_failed
(a run that hit the iteration cap is recorded agent_failed=False by design and
counts). Smoke rows (prompt_version 0) are excluded.

Selection policy, per (pipeline, model, task):
  1. same pipeline type and exact agent_model_name (the cohort label);
  2. prompt_version in the cohort's approved set (below) - smoke rows (pv 0)
     and stray old-prompt runs never qualify;
  3. not deprecated and not agent_failed (a run that hit the iteration cap is
     recorded agent_failed=False by the pipelines and counts);
  4. if more than one row survives, the most recent (highest id) wins and the
     task is listed under tasks_with_multiple_valid_rows for review.

Scope defaults to every non-deprecated jp task in the table (101 as of
2026-09-05: the original 68 plus 33 added 2026-09-04). Pass --max-task-id 68
to restrict to the original study corpus.

Read-only. Writes good_attempts_v2.json next to this script (or --out).

    uv run python scripts/export_good_attempts.py [--out PATH] [--max-task-id N]
"""
import argparse, json, sys, os
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import Config  # noqa: E402
import psycopg2  # noqa: E402

COHORTS = [
    # (pipeline, model, agent_model_type, agent_model_name, approved prompt versions)
    ("gui",        "fable", "gui",        "claude_fable_5_cowork_max",              (201, 202, 203)),
    ("gui",        "sol",   "gui",        "chatgpt_gpt_5_6_sol_work_ultra",         (201, 202, 203)),
    ("api",        "fable", "api",        "openpyxl_anthropic/claude-fable-5-max",   (1307,)),
    ("api",        "sol",   "api",        "openpyxl_openai/gpt-5.6-sol-xhigh",      (1307,)),
    ("coding_cli", "fable", "coding_cli", "claudecode_anthropic/claude-fable-5-max", (109,)),
    ("coding_cli", "sol",   "coding_cli", "codex_openai/gpt-5.6-sol-xhigh",         (109,)),
    ("excel",      "fable", "excel",      "claude_excel_fable_5",                   (203,)),
    ("excel",      "sol",   "excel",      "chatgpt_excel_gpt_5_6_sol_xhigh",        (203,)),
]

def jsonable(v):
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, (datetime, date)): return v.isoformat()
    return v

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "good_attempts_v2.json"))
    ap.add_argument("--max-task-id", type=int, default=None, help="restrict to task ids 1..N (default: all jp tasks)")
    args = ap.parse_args()

    cfg = Config.load()
    url = cfg.get("database.v2_url") if hasattr(cfg, "get") else cfg["database"]["v2_url"]
    con = psycopg2.connect(url); cur = con.cursor()
    cur.execute("select current_database()"); dbname = cur.fetchone()[0]
    assert "MBABenchV2" in dbname, f"expected the MBABenchV2 database, got {dbname}"

    cur.execute("select id, task_name from tasks where not deprecated and task_source = 'jp' order by id")
    all_tasks = dict(cur.fetchall())
    max_id = args.max_task_id or max(all_tasks)
    tasks = {k: v for k, v in all_tasks.items() if k <= max_id}
    outside = [{"task_id": k, "task_name": v} for k, v in all_tasks.items() if k > max_id]

    attempts, cohort_summary = [], []
    for pipeline, model, mtype, mname, pvs in COHORTS:
        cur.execute("""
            select id, task_id, prompt_version, start_time, end_time, time_taken_min, cost,
                   agent_failed_reason, attempt_files, extra_configs
              from task_attempts
             where agent_model_type = %s and agent_model_name = %s
               and prompt_version = any(%s)
               and not deprecated and not agent_failed
             order by task_id, id""", (mtype, mname, list(pvs)))
        by_task = {}
        for row in cur.fetchall():
            by_task.setdefault(row[1], []).append(row)
        covered, missing, duplicates = [], [], []
        for tid, tname in tasks.items():
            rows = by_task.get(tid)
            if not rows:
                missing.append({"task_id": tid, "task_name": tname}); continue
            if len(rows) > 1:
                duplicates.append({"task_id": tid, "attempt_ids": [r[0] for r in rows]})
            r = rows[-1]  # latest valid row if more than one
            files = r[8] if isinstance(r[8], list) else json.loads(r[8] or "[]")
            solution = next((f for f in files if str(f).endswith("solution.xlsx")), None) \
                    or next((f for f in files if str(f).endswith(".xlsx")), None)
            attempts.append({
                "pipeline": pipeline, "model": model, "agent_model_name": mname,
                "task_id": tid, "task_name": tname, "attempt_id": r[0],
                "prompt_version": r[2], "start_time": jsonable(r[3]), "end_time": jsonable(r[4]),
                "time_taken_min": jsonable(r[5]), "cost_usd": jsonable(r[6]),
                "note": r[7], "solution_file": solution, "attempt_files": [str(f) for f in files],
                "extra_configs": r[9],
            })
            covered.append(tid)
        cohort_summary.append({
            "pipeline": pipeline, "model": model, "agent_model_name": mname,
            "approved_prompt_versions": list(pvs),
            "good_attempts": len(covered), "tasks": len(tasks),
            "missing_tasks": missing, "tasks_with_multiple_valid_rows": duplicates,
        })

    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": dbname,
        "design": {"pipelines": 4, "models": 2, "tasks": len(tasks), "expected_attempts": 8 * len(tasks),
                   "task_id_range": [1, max_id]},
        "tasks_outside_scope": outside,
        "total_good_attempts": len(attempts),
        "cohorts": cohort_summary,
        "attempts": attempts,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=jsonable)
    print(f"{dbname}: {len(attempts)} good attempts of {8*len(tasks)} expected (tasks 1-{max_id}) -> {args.out}")
    if outside:
        print(f"  NOTE: {len(outside)} jp tasks outside scope (ids {outside[0]['task_id']}-{outside[-1]['task_id']}) listed under tasks_outside_scope")
    for c in cohort_summary:
        miss = ", ".join(f"{m['task_id']} {m['task_name']}" for m in c["missing_tasks"][:6]) or "-"
        if len(c["missing_tasks"]) > 6: miss += f" ... ({len(c['missing_tasks'])} missing)"
        dup = f"  DUPLICATES {c['tasks_with_multiple_valid_rows']}" if c["tasks_with_multiple_valid_rows"] else ""
        print(f"  {c['pipeline']:10s} {c['model']:5s} {c['good_attempts']:2d}/{c['tasks']}  missing: {miss}{dup}")

if __name__ == "__main__":
    main()
