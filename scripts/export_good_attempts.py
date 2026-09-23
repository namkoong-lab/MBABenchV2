#!/usr/bin/env python3
"""Export the two pointer manifests behind an MBABenchV2 leaderboard.

  good_attempts_v2.json   one entry per (cohort, task): the task_attempts row
                          that counts, with the S3 paths of its files.
  good_gradings_v2.json   every gradings row that counts for those attempts,
                          with its S3 folder.

Neither file holds workbooks or scores - only the ids and paths that say where
to look in Neon (task_attempts.id, gradings.id) and in S3.

Cohorts = COHORTS below: the eight production cohorts of the 101-task rerun -
GUI (Fable 5.1 cowork/max, GPT-6 Astra work/ultra), Excel add-in (Fable 5.1,
GPT-5.6 Sol xhigh), CLI harness (Fable 5.1 max, GPT-6 Astra xhigh) and coding
agents (Claude Code Fable 5.1 max, Codex GPT-6 Astra xhigh) - plus the six
added 2026-09-21: GUI chat mode (GPT-6 Pro), Excel add-in Opus 5, CLI Grok 4.6
and Kimi K3, and Codex on Gemini 3.8 Flash and Grok 4.6 - plus Codex on Kimi K3
(2026-09-22). To fold in another LLM + pipeline combo, add one line to COHORTS;
a cohort still running simply shows its open tasks under missing_tasks.

A cohort may list several agent_model_names: one model, one benchmark row, more
than one route. cli/astra is both openpyxl_openai/gpt-6-astra-xhigh and the
openpyxl_tensorblock/... label its outage reruns carry. The labels are never
merged - every attempt and grading entry records the one it ran under, and the
cohort summary counts them separately under good_attempts_by_agent_model_name.

Attempt policy, per (cohort, task):
  1. same pipeline type and exact agent_model_name (the cohort label);
  2. prompt_version is the pipeline's LATEST (LATEST_PV below) - smoke rows
     (pv 0/1) and every earlier prompt generation never qualify;
  3. not deprecated and not agent_failed (a run that hit the iteration cap is
     recorded agent_failed=False by the pipelines and counts);
  4. if more than one row survives, the most recent (highest id) wins and the
     task is listed under tasks_with_multiple_valid_rows for review.
  A task with no good attempt is listed under missing_tasks with the ids of
  its live failed rows, if any. solution_file is the workbook the judge grades
  (the first Excel file in attempt_files); attempts that uploaded more than one
  are listed under attempts_with_multiple_workbooks for review.

Grading policy, per good attempt:
  1. gradings.attempt_id is that attempt;
  2. judge_version is --judge-version (default: single_pass.version in
     judge/project_configs.yaml - one leaderboard, one judge);
  3. not deprecated and not failed;
  4. grader_model is one of --graders (default SOL_GRADERS: the two billing
     routes of the same Sol grader). Patrick 2026-09-22: the leaderboard is
     Sol-judged only, so rows from any other judge model (a Fable-judge
     bake-off, say) are counted under live_gradings_other_graders and left out;
  5. every surviving row is listed - the repeat Sol pass over ~90 attempts is
     a consistency study, and its extra rows stay visible under
     tasks_with_multiple_gradings. Tasks with no row are listed under
     ungraded_tasks. Live gradings under any other judge version are only
     counted, under live_gradings_other_judge_versions.

Scope defaults to every non-deprecated jp task in the table (101 as of
2026-09-05: the original 68 plus 33 added 2026-09-04). Pass --max-task-id 68
to restrict to the original study corpus.

Read-only. Writes both files next to this script (or --out / --gradings-out).

    uv run python scripts/export_good_attempts.py [--out PATH] [--gradings-out PATH]
                                                  [--judge-version N] [--max-task-id N]
                                                  [--graders NAME ... | --graders all]
"""
import argparse, json, sys, os
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import Config  # noqa: E402
import psycopg2  # noqa: E402
import yaml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE_CONFIGS = os.path.join(HERE, "..", "judge", "project_configs.yaml")
EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}

# The leaderboard's grader: GPT-5.6 Sol at effort none, under the two labels its
# billing routes wrote (TensorBlock until 2026-09-22 20:30, OpenAI direct after).
# Same model and effort, so rows under either count; any other judge model does not.
SOL_GRADERS = ("openai/gpt-5.6-sol", "tensorblock/gpt-5.6-sol")

# Latest prompt generation per pipeline type (2026-09-10, House Standards set):
# GUI/Excel 205 (single-pass + Questions sheet + house standards), CLI 1609
# (system v16 + template v9, rubric-scrubbed), coding 113 (template v13, rubric-scrubbed). ONLY these count —
# every earlier prompt version is ignored by this manifest, so a task is
# "missing" until it has a good attempt on the current prompts. Same numbers
# as judge/utils/misc_utils.py LATEST_PROMPT_VERSION_BY_TYPE (a test pins it).
LATEST_PV = {"gui": 205, "excel": 205, "api": 1609, "coding_cli": 113}

COHORTS = [
    # (pipeline, model, agent_model_type, agent_model_name(s)) - one line per
    # LLM + pipeline combo on the leaderboard; the prompt version follows the type.
    # A cohort may list SEVERAL agent_model_names when the same model ran over
    # more than one route (see cli/astra). The labels stay separate in the DB
    # and on every attempt entry; only the benchmark row is shared.
    ("gui",    "fable", "gui",        "claude_fable_5_1_cowork_max"),
    ("gui",    "astra", "gui",        "chatgpt_gpt_6_astra_work_ultra"),
    ("excel",  "fable", "excel",      "claude_excel_fable_5_1"),
    ("excel",  "sol",   "excel",      "chatgpt_excel_gpt_5_6_sol_xhigh"),
    ("cli",    "fable", "api",        "openpyxl_anthropic/claude-fable-5-1-max"),
    # Patrick 2026-09-22: the 11 tasks the OpenAI credit outage killed were re-run
    # through TensorBlock Forge under their own label; both routes count as one
    # CLI Astra row for the benchmark, and each attempt keeps the label it ran under.
    ("cli",    "astra", "api",        ("openpyxl_openai/gpt-6-astra-xhigh",
                                       "openpyxl_tensorblock/gpt-6-astra-xhigh")),
    ("coding", "fable", "coding_cli", "claudecode_anthropic/claude-fable-5-1-max"),
    ("coding", "astra", "coding_cli", "codex_openai/gpt-6-astra-xhigh"),
    # Added 2026-09-21 while their runs were in flight. gui_chat = ChatGPT chat mode, "Latest" at
    # the Pro stop: the chat UI never names Astra, hence gpt6_pro (the gui cohorts above are work mode).
    ("gui_chat", "gpt6_pro", "gui",      "chatgpt_gpt_6_pro"),
    ("excel",  "opus",   "excel",      "claude_excel_opus_5"),
    ("cli",    "grok",   "api",        "openpyxl_tensorblock/grok-4.6-xhigh"),
    ("cli",    "kimi",   "api",        "openpyxl_tensorblock/kimi-k3-max"),
    ("coding", "gemini", "coding_cli", "codex_tensorblock/gemini-3.8-flash-high"),
    ("coding", "grok",   "coding_cli", "codex_tensorblock/grok-4.6-xhigh"),
    ("coding", "kimi",   "coding_cli", "codex_tensorblock/kimi-k3-max"),  # added 2026-09-22
    ("cli",    "gemini", "api",        "openpyxl_tensorblock/gemini-3.8-flash-high"),  # added 2026-09-23
    # GUI cowork on Opus 5.5, four disjoint lanes on four accounts (2026-09-23).
    # One cohort, one label — the lane split is an operational detail, not an axis.
    ("gui",    "opus",   "gui",        "claude_opus_5_5_cowork_max"),
]
# (pipeline, model) names a leaderboard row, and the v12 grading driver keys on it too.
assert len({c[:2] for c in COHORTS}) == len(COHORTS), "two cohorts share a (pipeline, model) pair"
_names = [n for c in COHORTS for n in ((c[3],) if isinstance(c[3], str) else c[3])]
assert len(set(_names)) == len(_names), "an agent_model_name belongs to two cohorts"

def jsonable(v):
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, (datetime, date)): return v.isoformat()
    return v

def as_list(v):
    return v if isinstance(v, list) else json.loads(v or "[]")

def current_judge_version():
    with open(JUDGE_CONFIGS) as f:
        return int(yaml.safe_load(f)["single_pass"]["version"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "good_attempts_v2.json"))
    ap.add_argument("--gradings-out", default=os.path.join(HERE, "good_gradings_v2.json"))
    ap.add_argument("--judge-version", type=int, default=None,
                    help="judge version whose gradings count (default: single_pass.version in judge/project_configs.yaml)")
    ap.add_argument("--max-task-id", type=int, default=None, help="restrict to task ids 1..N (default: all jp tasks)")
    ap.add_argument("--graders", nargs="*", default=list(SOL_GRADERS),
                    help=f"grader_model values that count (default: {' '.join(SOL_GRADERS)}); 'all' keeps every grader")
    args = ap.parse_args()
    judge_version = args.judge_version or current_judge_version()
    graders = None if args.graders == ["all"] else set(args.graders)

    cfg = Config.load()
    url = cfg.get("database.v2_url") if hasattr(cfg, "get") else cfg["database"]["v2_url"]
    # v2_url is Neon's pooler host: plain SELECTs only, never set_session / SET.
    con = psycopg2.connect(url); con.autocommit = True; cur = con.cursor()
    cur.execute("select current_database()"); dbname = cur.fetchone()[0]
    assert "MBABenchV2" in dbname, f"expected the MBABenchV2 database, got {dbname}"

    cur.execute("select id, task_name from tasks where not deprecated and task_source = 'jp' order by id")
    all_tasks = dict(cur.fetchall())
    max_id = args.max_task_id or max(all_tasks)
    tasks = {k: v for k, v in all_tasks.items() if k <= max_id}
    outside = [{"task_id": k, "task_name": v} for k, v in all_tasks.items() if k > max_id]

    attempts, cohort_summary = [], []
    gradings, grading_summary = [], []
    for pipeline, model, mtype, mname in COHORTS:
        pv = LATEST_PV[mtype]
        names = [mname] if isinstance(mname, str) else list(mname)
        label = {"pipeline": pipeline, "model": model}
        cur.execute("""
            select id, task_id, prompt_version, start_time, end_time, time_taken_min, cost,
                   agent_failed_reason, attempt_files, prompt_files, agent_failed,
                   agent_model_name
              from task_attempts
             where agent_model_type = %s and agent_model_name = any(%s)
               and prompt_version = %s and not deprecated
             order by task_id, id""", (mtype, names, pv))
        by_task, failed_by_task = {}, {}
        for row in cur.fetchall():
            (failed_by_task if row[10] else by_task).setdefault(row[1], []).append(row)
        chosen, missing, duplicates, multi_workbook, by_name, name_of = {}, [], [], [], {}, {}
        for tid, tname in tasks.items():
            rows = by_task.get(tid)
            if not rows:
                missing.append({"task_id": tid, "task_name": tname,
                                "failed_attempt_ids": [r[0] for r in failed_by_task.get(tid, [])]}); continue
            if len(rows) > 1:
                duplicates.append({"task_id": tid, "attempt_ids": [r[0] for r in rows]})
            r = rows[-1]  # latest valid row if more than one
            files = [str(f) for f in as_list(r[8])]
            # The judge's rule (grade_from_db.setup_task_folder): the first Excel file listed.
            solution = next((f for f in files if os.path.splitext(f)[1].lower() in EXCEL_EXTS), None)
            if sum(os.path.splitext(f)[1].lower() in EXCEL_EXTS for f in files) > 1:
                multi_workbook.append({"task_id": tid, "attempt_id": r[0]})
            attempts.append({
                **label, "agent_model_name": r[11],
                "task_id": tid, "task_name": tname, "attempt_id": r[0],
                "prompt_version": r[2], "start_time": jsonable(r[3]), "end_time": jsonable(r[4]),
                "time_taken_min": jsonable(r[5]), "cost_usd": jsonable(r[6]),
                "note": r[7], "solution_file": solution, "attempt_files": files,
                "prompt_files": [str(f) for f in as_list(r[9])],
            })
            chosen[r[0]] = tid
            name_of[r[0]] = r[11]
            by_name[r[11]] = by_name.get(r[11], 0) + 1
        cohort_summary.append({
            **label, "agent_model_names": names, "good_attempts_by_agent_model_name": by_name,
            "approved_prompt_versions": [pv],
            "good_attempts": len(chosen), "tasks": len(tasks),
            "missing_tasks": missing, "tasks_with_multiple_valid_rows": duplicates,
            "attempts_with_multiple_workbooks": multi_workbook,
        })

        cur.execute("""
            select id, attempt_id, judge_version, grader_model, prompt_version, rubric_version,
                   rubric_weight_version, agentic_mode, created_at, raw_files_path, raw_files
              from gradings
             where attempt_id = any(%s) and not deprecated and not failed
             order by attempt_id, id""", (list(chosen),))
        graded, other_versions, other_graders = {}, {}, {}
        for g in cur.fetchall():
            if g[2] != judge_version:
                other_versions[str(g[2])] = other_versions.get(str(g[2]), 0) + 1; continue
            if graders is not None and g[3] not in graders:
                other_graders[g[3]] = other_graders.get(g[3], 0) + 1; continue
            tid = chosen[g[1]]
            graded.setdefault(tid, []).append(g[0])
            gradings.append({
                **label, "agent_model_name": name_of[g[1]],
                "task_id": tid, "task_name": tasks[tid], "attempt_id": g[1],
                "grading_id": g[0], "judge_version": g[2], "grader_model": g[3],
                "grader_prompt_version": g[4], "rubric_version": g[5], "rubric_weight_version": g[6],
                "agentic_mode": g[7], "created_at": jsonable(g[8]),
                "raw_files_path": g[9], "raw_files_count": len(as_list(g[10])),
            })
        grading_summary.append({
            **label, "agent_model_names": names,
            "good_attempts": len(chosen), "graded_attempts": len(graded),
            "gradings": sum(len(v) for v in graded.values()),
            "ungraded_tasks": [{"task_id": t, "attempt_id": a} for a, t in chosen.items() if t not in graded],
            "tasks_with_multiple_gradings": [{"task_id": t, "grading_ids": v} for t, v in graded.items() if len(v) > 1],
            "live_gradings_other_judge_versions": other_versions,
            "live_gradings_other_graders": other_graders,
        })

    header = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": dbname,
        "design": {"cohorts": len(COHORTS), "tasks": len(tasks), "expected_attempts": len(COHORTS) * len(tasks),
                   "task_id_range": [1, max_id]},
    }
    with open(args.out, "w") as f:
        json.dump({**header, "tasks_outside_scope": outside, "total_good_attempts": len(attempts),
                   "cohorts": cohort_summary, "attempts": attempts}, f, indent=2, default=jsonable)
    with open(args.gradings_out, "w") as f:
        json.dump({**header, "judge_version": judge_version,
                   "graders": sorted(graders) if graders else "all",
                   "attempts_manifest": os.path.basename(args.out),
                   "total_good_attempts": len(attempts),
                   "total_graded_attempts": sum(c["graded_attempts"] for c in grading_summary),
                   "total_gradings": len(gradings),
                   "cohorts": grading_summary, "gradings": gradings}, f, indent=2, default=jsonable)

    print(f"{dbname}: {len(attempts)} good attempts of {len(COHORTS)*len(tasks)} expected (tasks 1-{max_id}) -> {args.out}")
    print(f"  {len(gradings)} gradings under judge v{judge_version} -> {args.gradings_out}")
    if outside:
        print(f"  NOTE: {len(outside)} jp tasks outside scope (ids {outside[0]['task_id']}-{outside[-1]['task_id']}) listed under tasks_outside_scope")
    for c, g in zip(cohort_summary, grading_summary):
        miss = ", ".join(f"{m['task_id']} {m['task_name']}" for m in c["missing_tasks"][:6]) or "-"
        if len(c["missing_tasks"]) > 6: miss += f" ... ({len(c['missing_tasks'])} missing)"
        dup = f"  DUPLICATES {c['tasks_with_multiple_valid_rows']}" if c["tasks_with_multiple_valid_rows"] else ""
        other = f"  (other judge versions: {g['live_gradings_other_judge_versions']})" if g["live_gradings_other_judge_versions"] else ""
        print(f"  {c['pipeline']:10s} {c['model']:5s} attempts {c['good_attempts']:3d}/{c['tasks']}  "
              f"graded {g['graded_attempts']:3d}/{c['good_attempts']}{other}  missing: {miss}{dup}")

if __name__ == "__main__":
    main()
