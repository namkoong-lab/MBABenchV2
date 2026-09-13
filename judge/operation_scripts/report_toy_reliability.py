"""Per-check reliability report for toy runs (READ-ONLY on judge_reliability.*).

For each rubric check in the run(s): how often the Pass file passed, how often the Fail file
failed, how many repeats flipped, cost. Prints a markdown table; --csv writes the rows.

    python judge/operation_scripts/report_toy_reliability.py --run-id <id> [--run-id <id2> ...]
    python judge/operation_scripts/report_toy_reliability.py --run-label run1_targeted --csv out.csv
    python judge/operation_scripts/report_toy_reliability.py --list          # runs on file
"""
import argparse, csv, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg2, psycopg2.extras  # noqa: E402
from utils import repo_config  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", action="append", default=[])
ap.add_argument("--run-label", action="append", default=[])
ap.add_argument("--list", action="store_true")
ap.add_argument("--csv")
ap.add_argument("--show-misses", action="store_true", help="print the LLM summary for every wrong verdict")
args = ap.parse_args()

url, _ = repo_config.resolve_db_url("v2")
conn = psycopg2.connect(url)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

if args.list or not (args.run_id or args.run_label):
    cur.execute("select run_id, run_label, purpose, grader_model, judge_version, prompt_version, started_at, finished_at, n_graded, total_cost "
                "from judge_reliability.toy_runs order by started_at")
    for r in cur.fetchall():
        print(f"{r['run_id']}  {r['run_label']:<24} {r['purpose']:<8} {r['grader_model']:<28} j{r['judge_version']}/p{r['prompt_version']}  "
              f"n={r['n_graded']:<4} ${r['total_cost']:<8} {r['started_at']:%Y-%m-%d %H:%M} -> {r['finished_at'] or 'running'}")
    if not (args.run_id or args.run_label):
        sys.exit(0)

cur.execute(
    "select g.*, t.category, t.check_name, r.run_label from judge_reliability.toy_gradings g "
    "join judge_reliability.toy_tasks t on t.id = g.toy_task_id join judge_reliability.toy_runs r on r.run_id = g.run_id "
    "where not g.deprecated and (g.run_id = any(%s) or r.run_label = any(%s)) order by g.check_no, g.variant, g.repeat_no",
    (args.run_id, args.run_label))
rows = cur.fetchall()
if not rows:
    print("no gradings"); sys.exit(0)

by = defaultdict(lambda: {"pass": [], "fail": [], "cost": 0.0, "failed": 0, "cat": "", "name": ""})
for g in rows:
    b = by[g["check_no"]]; b["cat"], b["name"] = g["category"], g["check_name"]
    b["cost"] += float(g["cost"] or 0)
    if g["failed"] or g["verdict"] is None:
        b["failed"] += 1; continue
    b[g["variant"]].append(g["verdict"])


def rate(vs, want):
    return f"{sum(v == want for v in vs)}/{len(vs)}" if vs else "-"


def flips(vs):
    return "flip" if len(set(vs)) > 1 else ""


out = []
print(f"\n| # | category | check | Pass file → pass | Fail file → fail | flips | errors | $ |")
print("|---|---|---|---|---|---|---|---|")
tot_p = tot_pn = tot_f = tot_fn = 0
for n in sorted(by):
    b = by[n]
    p_ok, f_ok = sum(v == "pass" for v in b["pass"]), sum(v == "fail" for v in b["fail"])
    tot_p += p_ok; tot_pn += len(b["pass"]); tot_f += f_ok; tot_fn += len(b["fail"])
    fl = " ".join(x for x in (flips(b["pass"]), flips(b["fail"])) if x)
    flag = "" if (p_ok == len(b["pass"]) and f_ok == len(b["fail"])) else " **MISS**"
    print(f"| {n} | {b['cat']} | {b['name']}{flag} | {rate(b['pass'], 'pass')} | {rate(b['fail'], 'fail')} | {fl} | {b['failed'] or ''} | {b['cost']:.2f} |")
    out.append({"check_no": n, "category": b["cat"], "check": b["name"], "pass_file_pass": rate(b["pass"], "pass"),
                "fail_file_fail": rate(b["fail"], "fail"), "flips": fl, "errors": b["failed"], "cost": round(b["cost"], 3)})
print(f"\nPass files judged pass: {tot_p}/{tot_pn}   Fail files judged fail: {tot_f}/{tot_fn}   "
      f"total ${sum(float(g['cost'] or 0) for g in rows):.2f}   gradings {len(rows)}")
both = [n for n, b in by.items() if b["pass"] and b["fail"] and all(v == 'pass' for v in b['pass']) and all(v == 'fail' for v in b['fail'])]
print(f"Checks fully reliable in this run: {len(both)}/{len(by)}")
if args.show_misses:
    print("\nMisses:")
    for g in rows:
        if g["verdict"] and g["verdict"] != g["expected"]:
            print(f"  #{g['check_no']} {g['variant']} r{g['repeat_no']}: judged {g['verdict']} — {(g['llm_summary'] or '')[:300]}")
if args.csv:
    with open(args.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    print("csv ->", args.csv)
