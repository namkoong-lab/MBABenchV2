#!/usr/bin/env python
"""Per-criterion controlled-validation stats (paper §4.1) from judge_reliability.* (READ-ONLY).

Keeps only the criteria the LLM judge still decides: drops the deterministic checks listed
live in judge/project_configs.yaml (det_checks.live) and Final calculation accuracy (harness
answer check). --all keeps every check. --exclude leaves out
checks by number (default: the retired checks 28, 37, 101). --latest takes the last --reps verdicts
per example across the selected runs, so retests supersede earlier verdicts. Deprecated toys and deprecated/failed gradings are
skipped; the first --reps valid verdicts per (check, variant) count, in grading order.

Prints: examples graded correctly in all reps / in at least reps-1; per-verdict accuracy,
FP rate (clean example judged fail), FN rate (violation judged pass), F1 (positive =
violation); per-category accuracy; and every miss.

    python operation/v2/paper_scripts/calc_toy_stat.py --run-label run2_forge_v8 [--run-label ...]
    python operation/v2/paper_scripts/calc_toy_stat.py --run-id <id> ... [--reps 3] [--all]
    python operation/v2/paper_scripts/calc_toy_stat.py --list

The paper's 2026-09 figures (256 examples, 768 verdicts, 98.3%) are reproduced by every run
combined, latest 3 per example, retired checks excluded, all checks kept:
    python operation/v2/paper_scripts/calc_toy_stat.py --run-label 'run*' --latest --all
"""
import argparse
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
JUDGE = REPO_ROOT / "judge"
FINAL_ACCURACY = "Final calculation accuracy"


def db_url() -> str:
    spec = importlib.util.spec_from_file_location("_cfg", REPO_ROOT / "config" / "python" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Config.load(REPO_ROOT / "config").as_dict()["database"]["v2_url"]


def live_det_checks() -> set[int]:
    cfg = yaml.safe_load((JUDGE / "project_configs.yaml").read_text())
    live = str(cfg["det_checks"]["live"])
    return {int(x) for x in live.split(",") if x.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", action="append", default=[])
    ap.add_argument("--run-label", action="append", default=[], help="exact run_label, or prefix with trailing *")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="keep deterministic checks too")
    ap.add_argument("--latest", action="store_true",
                    help="take the LAST --reps verdicts per example across the selected runs (retests supersede) instead of the first")
    ap.add_argument("--exclude", default="retired",
                    help="comma list of check numbers to leave out; 'retired' = judge.retired_checks from project_configs.yaml; '' = none")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(db_url())
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if args.list or not (args.run_id or args.run_label):
        cur.execute("select run_label, min(judge_version) jv, count(*) runs, sum(n_graded) n, min(started_at)::date d "
                    "from judge_reliability.toy_runs group by run_label order by d, run_label")
        for r in cur.fetchall():
            print(f"{r['run_label']:<32} j{r['jv']:<3} runs={r['runs']:<3} n={r['n']:<5} {r['d']}")
        sys.exit(0)

    if args.exclude == "retired":
        cfg = yaml.safe_load((JUDGE / "project_configs.yaml").read_text())
        excluded = {int(x) for x in str(cfg["judge"]["retired_checks"]).split(",") if x.strip()}
    else:
        excluded = {int(x) for x in args.exclude.split(",") if x.strip()}
    labels = [l for l in args.run_label if not l.endswith("*")]
    prefixes = [l[:-1] for l in args.run_label if l.endswith("*")]
    cur.execute(
        "select g.check_no, g.variant, g.expected, g.verdict, g.repeat_no, g.created_at, t.category, t.check_name, "
        "r.run_label, r.judge_version from judge_reliability.toy_gradings g "
        "join judge_reliability.toy_tasks t on t.id = g.toy_task_id "
        "join judge_reliability.toy_runs r on r.run_id = g.run_id "
        "where not g.deprecated and not t.deprecated and not g.failed and g.verdict is not null "
        "and (g.run_id = any(%s) or r.run_label = any(%s) or r.run_label like any(%s)) "
        "order by g.check_no, g.variant, g.created_at, g.repeat_no",
        (args.run_id, labels, [p + "%" for p in prefixes]))
    rows = cur.fetchall()
    if not rows:
        sys.exit("no gradings")

    drop = set(excluded) | (set() if args.all else live_det_checks())
    ex = defaultdict(list)  # (check_no, variant) -> verdicts (first or last --reps)
    meta = {}
    rows.sort(key=lambda g: (g["check_no"], g["variant"], g["created_at"]))
    for g in rows:
        if g["check_no"] in drop or (not args.all and g["check_name"] == FINAL_ACCURACY):
            continue
        k = (g["check_no"], g["variant"])
        ex[k].append((g["verdict"] == g["expected"], g["verdict"], g["run_label"]))
        meta[k] = (g["category"], g["check_name"], g["expected"])
    for k in ex:
        ex[k] = ex[k][-args.reps:] if args.latest else ex[k][: args.reps]

    checks = sorted({k[0] for k in ex})
    short = [k for k, v in ex.items() if len(v) < args.reps]
    print(f"judge versions: {sorted({r['judge_version'] for r in rows})}   runs: {sorted({r['run_label'] for r in rows})}")
    print(f"criteria kept: {len(checks)} (excluded {sorted(excluded)}; dropped {len(drop - excluded) + (0 if args.all else 1)} deterministic)   "
          f"examples: {len(ex)}   reps: {args.reps}" + (f"   WARNING {len(short)} examples have < {args.reps} verdicts" if short else ""))

    n_all = sum(all(c for c, _, _ in v) for v in ex.values())
    n_most = sum(sum(c for c, _, _ in v) >= args.reps - 1 for v in ex.values())
    print(f"correct in all {args.reps}: {n_all}/{len(ex)} ({100 * n_all / len(ex):.1f}%)   "
          f"in at least {args.reps - 1}: {n_most}/{len(ex)} ({100 * n_most / len(ex):.1f}%)")

    verdicts = [(k, c, v) for k, vs in ex.items() for c, v, _ in vs]
    n = len(verdicts)
    ok = sum(c for _, c, _ in verdicts)
    clean = [(k, c) for k, c, _ in verdicts if meta[k][2] == "pass"]
    viol = [(k, c) for k, c, _ in verdicts if meta[k][2] == "fail"]
    fp = sum(not c for _, c in clean)
    fn = sum(not c for _, c in viol)
    tp = len(viol) - fn
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / len(viol) if viol else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
    print(f"verdicts: {n}   accuracy {100 * ok / n:.1f}%   FP {fp}/{len(clean)} ({100 * fp / len(clean):.2f}%)   "
          f"FN {fn}/{len(viol)} ({100 * fn / len(viol):.1f}%)   precision {100 * prec:.1f}%  recall {100 * rec:.1f}%  F1 {100 * f1:.1f}%")

    by_cat = defaultdict(lambda: [0, 0])
    for k, c, _ in verdicts:
        by_cat[meta[k][0]][0] += c
        by_cat[meta[k][0]][1] += 1
    print("\nper-category accuracy:")
    for cat, (o, t) in sorted(by_cat.items(), key=lambda x: x[1][0] / x[1][1]):
        print(f"  {cat:<28} {o}/{t} ({100 * o / t:.1f}%)")

    print("\nmisses:")
    for k, vs in sorted(ex.items()):
        for i, (c, v, lab) in enumerate(vs):
            if not c:
                print(f"  #{k[0]} {meta[k][1]} [{k[1]}] rep {i + 1} ({lab}): expected {meta[k][2]}, judged {v}")


if __name__ == "__main__":
    main()
