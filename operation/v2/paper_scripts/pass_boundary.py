#!/usr/bin/env python
"""How far the leaderboard's workbooks sit from the pass boundary.

Answers the reviewer's "how are violation counts distributed near the pass boundary?" two ways,
on exactly the leaderboard's gradings (plot_leaderboard.fetch: judge >= v12, newest clean grading
per cohort and task, style_guide.yaml cohorts, BACKFILL_ZERO, --min-tasks):

1. Sensitivity. Every tier limit k_t (pass_rule.TIER_TOLERANCE) is moved by -1, 0 and +1 and the
   pass rate recomputed. If the three are close, passes and fails are well separated; if they
   swing, many workbooks sit at the boundary. Tighter limits show what a missed violation would
   cost the headline, looser ones what a false flag would. A workbook that fails the
   deterministic accuracy check fails at every setting.

2. Flags to the boundary. One number per workbook: how many flags would have to change for its
   verdict to flip. Slack does not pool across tiers, so this is per tier, not total flags vs 8:
       fail  sum_t max(0, flags_t - k_t)   flags over the limit: false flags it would take to pass
       pass  1 + min_t (k_t - flags_t)     extra flags in its tightest tier that would fail it
   Tier 0 allows one flag, so no pass is more than 2 away. Workbooks that fail the accuracy check
   (and BACKFILL_ZERO tasks with no workbook) are counted apart: no flag count changes them.

For scale, P(pass | perfect workbook) at each setting: how often a flawless workbook passes on
false flags alone (pass_rule.FPR, every rubric check applicable).

Writes under operation/results/v2/pass_boundary/ (gitignored):
  pass_boundary.md    both tables
  pass_rates.csv      per cohort: n, pass rate at k-1 / k / k+1
  distance.csv        per cohort: workbooks by flags to the boundary

Read-only on the database. Run from the repo root with the judge environment:
    uv run --project judge python operation/v2/paper_scripts/pass_boundary.py
"""

import argparse
import csv
import math
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pass_rule import FPR, TIER_TOLERANCE, TIERS, PassVerdict, binom_cdf, load_tiers, tier_counts  # noqa: E402
from plot_leaderboard import MIN_TASKS_DEFAULT, REPO_ROOT, database_url, fetch, ranked  # noqa: E402

OUT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "pass_boundary"
SHIFTS = (-1, 0, 1)  # added to every k_t
TOP_N = 5  # cohorts shown in the distance table besides the pooled row
# distance buckets, in display order
BUCKETS = ["pass +2", "pass +1", "fail 1", "fail 2", "fail 3-5", "fail 6+", "accuracy", "no workbook"]
BUCKET_HEAD = {
    "pass +2": "Pass, 2 to spare",
    "pass +1": "Pass, at a limit",
    "fail 1": "Fail, 1 over",
    "fail 2": "Fail, 2 over",
    "fail 3-5": "Fail, 3-5 over",
    "fail 6+": "Fail, 6+ over",
    "accuracy": "Fail, wrong answer",
    "no workbook": "No workbook",
}


def passes(v: PassVerdict, shift: int) -> bool:
    """The pass rule with every k_t moved by shift."""
    return v.deterministic_ok and all(v.flags[t] <= TIER_TOLERANCE[t] + shift for t in TIERS)


def bucket(v: PassVerdict) -> str:
    if not v.deterministic_ok:
        return "no workbook" if not any(v.n.values()) else "accuracy"
    if v.passed:
        return f"pass +{1 + min(TIER_TOLERANCE[t] - v.flags[t] for t in TIERS)}"
    over = sum(max(0, v.flags[t] - TIER_TOLERANCE[t]) for t in TIERS)
    return "fail 1" if over == 1 else "fail 2" if over == 2 else "fail 3-5" if over <= 5 else "fail 6+"


def perfect_pass(tiers, shift: int) -> float:
    counts = tier_counts(tiers)
    return math.prod(binom_cdf(TIER_TOLERANCE[t] + shift, counts[t], FPR) for t in TIERS)


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def md_table(head: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|---|" + "---:|" * (len(head) - 1)] + [
        "| " + " | ".join(r) + " |" for r in rows
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    ap.add_argument("--min-tasks", type=int, default=MIN_TASKS_DEFAULT,
                    help=f"drop cohorts graded on fewer tasks, as the leaderboard (default {MIN_TASKS_DEFAULT})")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    tiers = load_tiers()
    conn = psycopg2.connect(database_url(args))
    try:
        conn.set_session(readonly=True)
        cohorts, _ = fetch(conn, args.min_tasks, tiers)
    finally:
        conn.close()
    if not cohorts:
        sys.exit("no cohort passes --min-tasks")
    cohorts = ranked(cohorts, with_score=False)
    everything = [v for c in cohorts for v in c.verdicts]

    # ---- 1. pass rate at k-1 / k / k+1
    def rates(vs):
        return [sum(passes(v, s) for v in vs) / len(vs) for s in SHIFTS]

    rate_rows = [["All workbooks pooled", "", str(len(everything)), *map(pct, rates(everything))]]
    rate_rows += [[c.name, c.harness, str(len(c.verdicts)), *map(pct, rates(c.verdicts))] for c in cohorts]
    rate_rows += [["Perfect workbook (false flags only)", "", "", *(pct(perfect_pass(tiers, s)) for s in SHIFTS)]]
    rate_head = ["Cohort", "Harness", "n", "Every limit 1 tighter", "Headline", "Every limit 1 looser"]

    # ---- 2. workbooks by flags to the boundary
    def dist(vs):
        b = [bucket(v) for v in vs]
        return [str(b.count(k)) for k in BUCKETS]

    dist_rows = [["All workbooks pooled", *dist(everything)]]
    dist_rows += [[c.name, *dist(c.verdicts)] for c in cohorts[:TOP_N]]
    dist_head = ["Cohort", *(BUCKET_HEAD[k] for k in BUCKETS)]

    limits = " / ".join(str(TIER_TOLERANCE[t]) for t in TIERS)
    md = [
        "## Pass rate if every tier limit moved by one",
        "",
        *md_table(rate_head, rate_rows),
        "",
        f"Tier limits as used: {limits} flags (tiers 0-3). Tighter = each limit minus 1, looser = plus 1. "
        "A wrong final answer fails at every setting. Perfect workbook: P(pass) for a flawless workbook "
        f"at a {pct(FPR)} false-flag rate per check.",
        "",
        "## Workbooks by flags to the boundary",
        "",
        *md_table(dist_head, dist_rows),
        "",
        "Fail, k over: k flags would have to be false for it to pass (sum over tiers of flags above the limit). "
        "Pass, at a limit: one more flag in its tightest tier fails it. "
        "Wrong answer / no workbook: no flag count changes the verdict.",
    ]
    text = "\n".join(md)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "pass_boundary.md").write_text(text + "\n")
    with (args.out_dir / "pass_rates.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(rate_head)
        w.writerows(rate_rows)
    with (args.out_dir / "distance.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Cohort", *BUCKETS])
        w.writerows([["All workbooks pooled", *dist(everything)]] + [[c.name, *dist(c.verdicts)] for c in cohorts])
    print(text)
    print(f"\nwrote {args.out_dir / 'pass_boundary.md'}, pass_rates.csv, distance.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
