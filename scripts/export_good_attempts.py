#!/usr/bin/env python3
"""Export the pointer manifests behind an SpreadsheetSmith leaderboard.

  good_attempts_v2.json   one entry per (cohort, task): the task_attempts row
                          that counts, with the paths of its files.
  good_gradings_v2.json   every gradings row that counts for those attempts,
                          with its staged-files folder.
  leaderboard_ids.json    one flat entry per attempt: cohort display name,
                          attempt id, grading id(s).
  good_attempts_stage5.json / good_gradings_stage5.json
                          the same two files for the stage-5 cohorts, the coding
                          prompt ablation. Separate because its arms run other
                          task prompts and are not comparable with the
                          leaderboard.

None of the files holds workbooks or scores - only ids and paths.

By default the manifests are built from the offline bundle (data/results/
task_attempts.jsonl and gradings.jsonl or gradings.jsonl.gz, see data/README.md) and written next
to it, so a checkout reproduces the leaderboard with no database. --from-db
builds them from the live v2 database instead (config/config.yaml
database.v2_url, plain read-only SELECTs) and writes them next to this script;
that output carries object-store paths and is not committed. Both paths run
the same selection policy below.

Cohorts come from data/results/cohorts.json: one entry per LLM + pipeline
combo on the leaderboard (pipeline/model name the row; agent_model_names are
the task_attempts labels that count; prompt_version null means the pipeline
type's latest_prompt_version_by_type). A cohort may list several
agent_model_names: one model, one benchmark row, more than one route (cli/astra
ran over two billing routes). The labels are never merged - every attempt and
grading entry records the one it ran under, and the cohort summary counts them
separately under good_attempts_by_agent_model_name. To fold in another
LLM + pipeline combo, add one entry to cohorts.json; a cohort still running
simply shows its open tasks under missing_tasks.

Attempt policy, per (cohort, task):
  1. same pipeline type and exact agent_model_name (the cohort label);
  2. prompt_version is the pipeline's LATEST (latest_prompt_version_by_type) -
     smoke rows (pv 0/1) and every earlier prompt generation never qualify;
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
  2. judge_version is --judge-version (default: leaderboard_judge_version in
     cohorts.json, which equals single_pass.version in judge/project_configs.yaml
     - one leaderboard, one judge);
  3. not deprecated and not failed;
  4. grader_model is one of --graders (default leaderboard_graders in
     cohorts.json: the two billing routes of the same grader). Rows from any
     other judge model (a second-judge bake-off, say) are counted under
     live_gradings_other_graders and left out;
  5. every surviving row is listed - the repeat pass over ~90 attempts is a
     consistency study, and its extra rows stay visible under
     tasks_with_multiple_gradings. Tasks with no row are listed under
     ungraded_tasks. Live gradings under any other judge version are only
     counted, under live_gradings_other_judge_versions.

Scope defaults to every non-deprecated v2 task (101). Pass --max-task-id 68 to
restrict to the original study corpus.

    uv run python scripts/export_good_attempts.py                # from data/results/
    uv run python scripts/export_good_attempts.py --from-db      # from the live database
    options: [--out-dir DIR] [--judge-version N] [--max-task-id N] [--graders NAME ... | --graders all]
"""
import argparse
import gzip
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
DATA_RESULTS = os.path.join(ROOT, os.environ.get("SPREADSHEETSMITH_DATA_ROOT", "data"), "results")
COHORTS_FILE = os.path.join(DATA_RESULTS, "cohorts.json")
EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}

ATTEMPT_COLS = ("id", "task_id", "prompt_version", "start_time", "end_time", "time_taken_min", "cost",
                "agent_failed_reason", "attempt_files", "prompt_files", "agent_failed", "agent_model_name")
GRADING_COLS = ("id", "attempt_id", "judge_version", "grader_model", "prompt_version", "rubric_version",
                "rubric_weight_version", "agentic_mode", "created_at", "raw_files_path", "raw_files")


def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def as_list(v):
    return v if isinstance(v, list) else json.loads(v or "[]")


def load_cohorts(path=COHORTS_FILE):
    with open(path) as f:
        doc = json.load(f)
    latest = doc["latest_prompt_version_by_type"]
    for c in doc["cohorts"] + doc["stage5_cohorts"]:
        c["_pv"] = c["prompt_version"] or latest[c["agent_model_type"]]
    # (pipeline, model) names a leaderboard row.
    keys = [(c["pipeline"], c["model"]) for c in doc["cohorts"]]
    assert len(set(keys)) == len(keys), "two cohorts share a (pipeline, model) pair"
    # One (label, prompt version) pair belongs to exactly one cohort.
    pairs = [(n, c["_pv"]) for c in doc["cohorts"] for n in c["agent_model_names"]]
    assert len(set(pairs)) == len(pairs), "a (label, prompt_version) pair belongs to two cohorts"
    return doc


class DbRows:
    """Read-only rows from the live v2 database."""

    def __init__(self):
        from config import Config
        import psycopg2
        cfg = Config.load()
        url = cfg.get("database.v2_url")
        # v2_url is the pooler host: plain SELECTs only, never set_session / SET.
        con = psycopg2.connect(url)
        con.autocommit = True
        self.cur = con.cursor()
        self.cur.execute("select current_database()")
        self.name = self.cur.fetchone()[0]
        assert "SpreadsheetSmith" in self.name, f"expected the SpreadsheetSmith database, got {self.name}"

    def tasks(self):
        self.cur.execute("select id, task_name from tasks where not deprecated and task_source = 'v2' order by id")
        return dict(self.cur.fetchall())

    def attempts(self, mtype, names, pv):
        self.cur.execute(f"""
            select {', '.join(ATTEMPT_COLS)} from task_attempts
             where agent_model_type = %s and agent_model_name = any(%s) and prompt_version = %s and not deprecated
             order by task_id, id""", (mtype, names, pv))
        return [dict(zip(ATTEMPT_COLS, r)) for r in self.cur.fetchall()]

    def gradings(self, attempt_ids):
        self.cur.execute(f"""
            select {', '.join(GRADING_COLS)} from gradings
             where attempt_id = any(%s) and not deprecated and not failed
             order by attempt_id, id""", (list(attempt_ids),))
        return [dict(zip(GRADING_COLS, r)) for r in self.cur.fetchall()]

    def unassigned(self, since, known, retired, latest):
        self.cur.execute("""
            select agent_model_type, agent_model_name, prompt_version, count(*), min(created_at), max(created_at)
              from task_attempts
             where not deprecated and not agent_failed and created_at >= %s
             group by 1, 2, 3 order by 6 desc""", (since,))
        return [{"agent_model_type": t, "agent_model_name": n, "prompt_version": pv,
                 "good_attempts": k, "first_seen": jsonable(lo), "last_seen": jsonable(hi)}
                for t, n, pv, k, lo, hi in self.cur.fetchall()
                if n not in known and n not in retired and latest.get(t) == pv]


class BundleRows:
    """The same rows from the offline bundle under data/results/."""

    def __init__(self, results_dir=DATA_RESULTS):
        self.name = f"{os.path.relpath(results_dir, ROOT)} (offline bundle)"
        with open(os.path.join(ROOT, "data", "tasks", "tasks.json")) as f:
            self._tasks = json.load(f)
        self._attempts = self._read(os.path.join(results_dir, "task_attempts.jsonl"))
        self._gradings = self._read(os.path.join(results_dir, "gradings.jsonl"))

    @staticmethod
    def _read(path):
        """Rows of <path>; of <path>.gz (gzip) when only the compressed copy is in place."""
        gz = path + ".gz"
        opener = (gzip.open(gz, "rt", encoding="utf-8") if not os.path.exists(path) and os.path.exists(gz)
                  else open(path, encoding="utf-8"))
        with opener as f:
            return [json.loads(line) for line in f if line.strip()]

    def tasks(self):
        return {t["id"]: t["task_name"] for t in self._tasks if not t["deprecated"] and t["task_source"] == "v2"}

    def attempts(self, mtype, names, pv):
        rows = [a for a in self._attempts
                if a["agent_model_type"] == mtype and a["agent_model_name"] in names
                and a["prompt_version"] == pv and not a["deprecated"]]
        return sorted(rows, key=lambda a: (a["task_id"], a["id"]))

    def gradings(self, attempt_ids):
        ids = set(attempt_ids)
        rows = [g for g in self._gradings if g["attempt_id"] in ids and not g["deprecated"] and not g["failed"]]
        return sorted(rows, key=lambda g: (g["attempt_id"], g["id"]))

    def unassigned(self, since, known, retired, latest):
        return []  # the bundle holds cohort rows only


def collect(rows, cohorts, tasks, judge_version, graders):
    """Walk a cohort table and return its attempts + gradings and the summaries."""
    attempts, cohort_summary = [], []
    gradings, grading_summary = [], []
    for c in cohorts:
        names, pv = c["agent_model_names"], c["_pv"]
        label = {"pipeline": c["pipeline"], "model": c["model"]}
        by_task, failed_by_task = {}, {}
        for row in rows.attempts(c["agent_model_type"], names, pv):
            (failed_by_task if row["agent_failed"] else by_task).setdefault(row["task_id"], []).append(row)
        chosen, missing, duplicates, multi_workbook, by_name, name_of = {}, [], [], [], {}, {}
        for tid, tname in tasks.items():
            trows = by_task.get(tid)
            if not trows:
                missing.append({"task_id": tid, "task_name": tname,
                                "failed_attempt_ids": [r["id"] for r in failed_by_task.get(tid, [])]})
                continue
            if len(trows) > 1:
                duplicates.append({"task_id": tid, "attempt_ids": [r["id"] for r in trows]})
            r = trows[-1]  # latest valid row if more than one
            files = [str(f) for f in as_list(r["attempt_files"])]
            # The judge's rule (grade_from_db.setup_task_folder): the first Excel file listed.
            solution = next((f for f in files if os.path.splitext(f)[1].lower() in EXCEL_EXTS), None)
            if sum(os.path.splitext(f)[1].lower() in EXCEL_EXTS for f in files) > 1:
                multi_workbook.append({"task_id": tid, "attempt_id": r["id"]})
            attempts.append({
                **label, "agent_model_name": r["agent_model_name"],
                "task_id": tid, "task_name": tname, "attempt_id": r["id"],
                "prompt_version": r["prompt_version"], "start_time": jsonable(r["start_time"]),
                "end_time": jsonable(r["end_time"]), "time_taken_min": jsonable(r["time_taken_min"]),
                "cost_usd": jsonable(r["cost"]), "note": r["agent_failed_reason"], "solution_file": solution,
                "attempt_files": files, "prompt_files": [str(f) for f in as_list(r["prompt_files"])],
            })
            chosen[r["id"]] = tid
            name_of[r["id"]] = r["agent_model_name"]
            by_name[r["agent_model_name"]] = by_name.get(r["agent_model_name"], 0) + 1
        cohort_summary.append({
            **label, "agent_model_names": names, "good_attempts_by_agent_model_name": by_name,
            "approved_prompt_versions": [pv],
            "good_attempts": len(chosen), "tasks": len(tasks),
            "missing_tasks": missing, "tasks_with_multiple_valid_rows": duplicates,
            "attempts_with_multiple_workbooks": multi_workbook,
        })

        graded, other_versions, other_graders = {}, {}, {}
        for g in rows.gradings(chosen):
            if g["judge_version"] != judge_version:
                other_versions[str(g["judge_version"])] = other_versions.get(str(g["judge_version"]), 0) + 1
                continue
            if graders is not None and g["grader_model"] not in graders:
                other_graders[g["grader_model"]] = other_graders.get(g["grader_model"], 0) + 1
                continue
            tid = chosen[g["attempt_id"]]
            graded.setdefault(tid, []).append(g["id"])
            gradings.append({
                **label, "agent_model_name": name_of[g["attempt_id"]],
                "task_id": tid, "task_name": tasks[tid], "attempt_id": g["attempt_id"],
                "grading_id": g["id"], "judge_version": g["judge_version"], "grader_model": g["grader_model"],
                "grader_prompt_version": g["prompt_version"], "rubric_version": g["rubric_version"],
                "rubric_weight_version": g["rubric_weight_version"],
                "agentic_mode": g["agentic_mode"], "created_at": jsonable(g["created_at"]),
                "raw_files_path": g["raw_files_path"], "raw_files_count": len(as_list(g["raw_files"])),
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
    return attempts, cohort_summary, gradings, grading_summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-db", action="store_true", help="read the live v2 database instead of data/results/")
    ap.add_argument("--out-dir", default=None,
                    help="where the five files go (default: data/results/, or next to this script with --from-db)")
    ap.add_argument("--judge-version", type=int, default=None,
                    help="judge version whose gradings count (default: leaderboard_judge_version in cohorts.json)")
    ap.add_argument("--max-task-id", type=int, default=None, help="restrict to task ids 1..N (default: all v2 tasks)")
    ap.add_argument("--graders", nargs="*", default=None,
                    help="grader_model values that count (default: leaderboard_graders in cohorts.json); 'all' keeps every grader")
    args = ap.parse_args()

    doc = load_cohorts()
    judge_version = args.judge_version or doc["leaderboard_judge_version"]
    graders = None if args.graders == ["all"] else set(args.graders or doc["leaderboard_graders"])
    out_dir = args.out_dir or (HERE if args.from_db else DATA_RESULTS)
    os.makedirs(out_dir, exist_ok=True)
    paths = {k: os.path.join(out_dir, v) for k, v in {
        "attempts": "good_attempts_v2.json", "gradings": "good_gradings_v2.json", "ids": "leaderboard_ids.json",
        "stage5": "good_attempts_stage5.json", "stage5_gradings": "good_gradings_stage5.json"}.items()}

    rows = DbRows() if args.from_db else BundleRows()
    all_tasks = rows.tasks()
    max_id = args.max_task_id or max(all_tasks)
    tasks = {k: v for k, v in all_tasks.items() if k <= max_id}
    outside = [{"task_id": k, "task_name": v} for k, v in all_tasks.items() if k > max_id]

    cohorts, stage5 = doc["cohorts"], doc["stage5_cohorts"]
    attempts, cohort_summary, gradings, grading_summary = collect(rows, cohorts, tasks, judge_version, graders)

    # Runs nobody has folded in yet: live rows on a pipeline's current prompt version under a label no cohort claims.
    known_names = {n for c in cohorts + stage5 for n in c["agent_model_names"]}
    unassigned = rows.unassigned(doc["unassigned_since"], known_names, set(doc["retired_labels"]),
                                 doc["latest_prompt_version_by_type"])

    def write_pair(cohort_table, attempts, cohort_summary, gradings, grading_summary, out, gradings_out, note=None):
        header = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": rows.name,
            "unassigned_identities": unassigned,
            "design": {"cohorts": len(cohort_table), "tasks": len(tasks),
                       "expected_attempts": len(cohort_table) * len(tasks), "task_id_range": [1, max_id]},
        }
        if note:
            header["note"] = note
        with open(out, "w") as f:
            json.dump({**header, "tasks_outside_scope": outside, "total_good_attempts": len(attempts),
                       "cohorts": cohort_summary, "attempts": attempts}, f, indent=2, default=jsonable)
        with open(gradings_out, "w") as f:
            json.dump({**header, "judge_version": judge_version,
                       "graders": sorted(graders) if graders else "all",
                       "attempts_manifest": os.path.basename(out),
                       "total_good_attempts": len(attempts),
                       "total_graded_attempts": sum(c["graded_attempts"] for c in grading_summary),
                       "total_gradings": len(gradings),
                       "cohorts": grading_summary, "gradings": gradings}, f, indent=2, default=jsonable)

    write_pair(cohorts, attempts, cohort_summary, gradings, grading_summary, paths["attempts"], paths["gradings"])

    # Flat pointer list for the leaderboard: one entry per attempt with the table's cohort label, the attempt
    # id and its grading id. grading_id is the FIRST live leaderboard-judge row (the production pass);
    # grading_ids lists every live one, so the stage-2 repeat rows stay visible without being mistaken for it.
    display = {(c["pipeline"], c["model"]): c["display_name"] for c in cohorts}
    by_attempt = {}
    for g in gradings:
        by_attempt.setdefault(g["attempt_id"], []).append(g["grading_id"])
    ids = [{"cohort": display.get((x["pipeline"], x["model"]), f"{x['pipeline']}/{x['model']}"),
            "task_id": x["task_id"], "attempt_id": x["attempt_id"],
            "grading_id": min(by_attempt[x["attempt_id"]]) if x["attempt_id"] in by_attempt else None,
            "grading_ids": sorted(by_attempt.get(x["attempt_id"], []))}
           for x in attempts]
    ids.sort(key=lambda r: (r["cohort"].lower(), r["task_id"]))
    with open(paths["ids"], "w") as f:
        json.dump({"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "database": rows.name, "judge_version": judge_version,
                   "graders": sorted(graders) if graders else "all",
                   "note": f"grading_id = first live leaderboard-judge v{judge_version} row per attempt; grading_ids = all live ones",
                   "attempts": len(ids), "graded": sum(r["grading_id"] is not None for r in ids),
                   "rows": ids}, f, indent=2)
    s_attempts, s_cohorts, s_gradings, s_grading_summary = collect(rows, stage5, tasks, judge_version, graders)
    write_pair(stage5, s_attempts, s_cohorts, s_gradings, s_grading_summary,
               paths["stage5"], paths["stage5_gradings"], note=doc["stage5_note"])

    print(f"{rows.name}: {len(attempts)} good attempts of {len(cohorts) * len(tasks)} expected (tasks 1-{max_id}) -> {paths['attempts']}")
    print(f"  {len(gradings)} gradings under judge v{judge_version} -> {paths['gradings']}")
    print(f"  {len(ids)} cohort/attempt/grading rows -> {paths['ids']}")
    print(f"  stage 5 ablation: {len(s_attempts)} attempts, {len(s_gradings)} gradings -> {paths['stage5']}")
    for c, g in zip(s_cohorts, s_grading_summary):
        print(f"    {c['pipeline']}/{c['model']:<13} pv{c['approved_prompt_versions'][0]}  "
              f"attempts {c['good_attempts']:3d}/{c['tasks']}  graded {g['graded_attempts']:3d}")
    for u in unassigned:
        print(f"  NEW IDENTITY (no cohort claims it): {u['agent_model_name']} [{u['agent_model_type']}"
              f" pv{u['prompt_version']}] {u['good_attempts']} good, last {u['last_seen'][:16]}")
    if outside:
        print(f"  NOTE: {len(outside)} v2 tasks outside scope (ids {outside[0]['task_id']}-{outside[-1]['task_id']}) listed under tasks_outside_scope")
    for c, g in zip(cohort_summary, grading_summary):
        miss = ", ".join(f"{m['task_id']} {m['task_name']}" for m in c["missing_tasks"][:6]) or "-"
        if len(c["missing_tasks"]) > 6:
            miss += f" ... ({len(c['missing_tasks'])} missing)"
        dup = f"  DUPLICATES {c['tasks_with_multiple_valid_rows']}" if c["tasks_with_multiple_valid_rows"] else ""
        other = f"  (other judge versions: {g['live_gradings_other_judge_versions']})" if g["live_gradings_other_judge_versions"] else ""
        print(f"  {c['pipeline']:10s} {c['model']:5s} attempts {c['good_attempts']:3d}/{c['tasks']}  "
              f"graded {g['graded_attempts']:3d}/{c['good_attempts']}{other}  missing: {miss}{dup}")


if __name__ == "__main__":
    main()
