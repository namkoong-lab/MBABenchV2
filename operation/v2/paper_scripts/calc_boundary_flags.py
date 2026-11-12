#!/usr/bin/env python
"""Judge flags around the pass boundary, by task difficulty (ICLR rebuttal, reviewer ndEy).

Workbooks: the leaderboard's own selection (plot_leaderboard.fetch and fold_cohorts): the paper
cohorts, newest clean grading per (cohort, task) with judge >= MIN_JUDGE_VERSION. Each verdict is
pass_rule.task_verdict. Backfilled zero rows (no workbook) were never graded and are not counted.

Two flag counts per workbook, over the tiered rubric checks (Final calculation accuracy is not tiered):
  rule flags  every flagged tiered check, LLM- or Python-decided: what the pass rule counts.
              They decide pass/fail, slack and overage.
  LLM flags   the flagged tiered checks whose check_scores item has grader "llm" (written from
              judge v13; an item without a grader counts as LLM). The table entries.

  slack    (passes) allowance left, summed over tiers: sum_t (k_t - flags_t) = 8 - rule flags.
  overage  (fails)  flags over the allowance, summed over tiers: sum_t max(0, flags_t - k_t).
           Room left in one tier does not offset an overage in another, so 2 over is either
           1 over in two tiers or 2 over in one.

Prints, summary first:
  passes by flag count 0..8 (rule flags, with slack, and LLM flags)
  fails with a right final answer by overage 1..--max-over, the rest in one bucket, each with the
    chance that a workbook carries at least that many false LLM flags, P(Binomial(N, FPR) >= n),
    N = mean applicable LLM-decided checks per workbook. A fail n over would pass only if at
    least n of its flags in the over tiers were false, so this chance is an upper bound for that.
    Python-decided checks are taken as error-free, and missed mistakes (FNR) only add flags to a
    fail, so they cannot turn it into a pass.
then six tables of LLM flags per workbook (rows: tasks.human_difficulty_measure; columns:
workbooks, quartiles by linear interpolation, mean, min, max):
  1 passes                                 4 passes with 1-2 slack
  2 fails with a right final answer        5 fails 1 over the limit
  3 passes with 1 slack                    6 fails 2 over the limit
Fails with a wrong final answer (the deterministic Final calculation accuracy check) are left out
of tables 2, 5 and 6; they fail whatever their flags.

    python operation/v2/paper_scripts/calc_boundary_flags.py [--max-over 5] [--fpr 0.0161]
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass

import numpy as np
import psycopg2
import psycopg2.extras

import plot_leaderboard as pl
from pass_rule import FLAG_MISTAKES, FPR, TIER_TOLERANCE, TIERS, binom_cdf, load_tiers, task_verdict

DIFFICULTIES = ("Easy", "Medium", "Medium-Hard", "Hard")  # tasks.human_difficulty_measure, easiest first
ALLOWANCE = sum(TIER_TOLERANCE.values())  # 8: the most rule flags a passing workbook can carry
MAX_OVER_DEFAULT = 5  # at FPR 1.61% over ~96 LLM checks, 6+ false flags has a chance near 0.5%


@dataclass(frozen=True)
class Workbook:
    cohort: str
    task_id: int
    difficulty: str
    passed: bool
    answer_ok: bool  # Final calculation accuracy not flagged
    rule_flags: dict[int, int]  # tier -> flagged tiered checks, LLM- and Python-decided
    llm_flags: int  # flagged tiered checks decided by the LLM
    llm_checks: int  # applicable, scored tiered checks decided by the LLM

    @property
    def total_rule_flags(self) -> int:
        return sum(self.rule_flags.values())

    @property
    def slack(self) -> int:
        return sum(TIER_TOLERANCE[t] - self.rule_flags[t] for t in TIERS)

    @property
    def overage(self) -> int:
        return sum(max(0, self.rule_flags[t] - TIER_TOLERANCE[t]) for t in TIERS)


def fetch(conn, min_tasks: int, tiers: dict[tuple[str, str], int]) -> list[Workbook]:
    """Every graded workbook of the plotted cohorts, with its verdict and flag counts."""
    cohorts, _ = pl.fetch(conn, min_tasks, tiers)  # the plotted cohorts; notes go to stderr
    names = {c.name for c in cohorts}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(pl.SQL, {"judge": pl.MIN_JUDGE_VERSION})
    rows = [r for r in pl.fold_cohorts(cur.fetchall()) if r["cohort"] in names]
    cur.execute("select id, human_difficulty_measure from tasks")
    difficulty = {r["id"]: r["human_difficulty_measure"] for r in cur.fetchall()}

    books = []
    for r in rows:
        verdict = task_verdict(r["check_scores"], tiers)
        if verdict is None:  # an unscored tier-0 or deterministic check: no verdict, as on the leaderboard
            continue
        llm_flags = llm_checks = 0
        for category, checks in (r["check_scores"] or {}).items():
            for name, item in (checks or {}).items():
                if (category, name) not in tiers or not isinstance(item, dict) or item.get("unscored"):
                    continue
                if item.get("grader", "llm") != "llm":
                    continue
                llm_checks += 1
                llm_flags += item["mistakes"] >= FLAG_MISTAKES
        books.append(Workbook(r["cohort"], r["task_id"], difficulty.get(r["task_id"]), verdict.passed,
                              verdict.deterministic_ok, dict(verdict.flags), llm_flags, llm_checks))
    unrated = {b.task_id for b in books if b.difficulty not in DIFFICULTIES}
    if unrated:
        print(f"tasks without a difficulty in {DIFFICULTIES} (counted in All only): {sorted(unrated)}",
              file=sys.stderr)
    return books


def num(x: float) -> str:
    """A quartile: up to two decimals, trailing zeros dropped."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def print_summary(books: list[Workbook], max_over: int, fpr: float) -> None:
    passes = [b for b in books if b.passed]
    fails = [b for b in books if not b.passed and b.answer_ok]
    wrong = sum(not b.answer_ok for b in books)
    assert all(b.overage >= 1 for b in fails)  # a right-answer fail is over in some tier
    print(f"{len(books)} graded workbooks: {len(passes)} pass, {len(fails)} fail with a right final answer, "
          f"{wrong} fail with a wrong final answer. Allowance k_t = {TIER_TOLERANCE}, {ALLOWANCE} in all.")

    print("\nPasses by number of flags")
    print("| flags | passes, rule flags | slack | passes, LLM flags |")
    print("|---|---|---|---|")
    by_rule = Counter(b.total_rule_flags for b in passes)
    by_llm = Counter(b.llm_flags for b in passes)
    for x in range(ALLOWANCE + 1):
        print(f"| {x} | {by_rule[x]} | {ALLOWANCE - x} | {by_llm[x]} |")
    print(f"| total | {len(passes)} | | {len(passes)} |")

    n_llm = round(float(np.mean([b.llm_checks for b in books])))
    by_over = Counter(min(b.overage, max_over + 1) for b in fails)
    print(f"\nFails with a right final answer, by flags over the limit "
          f"(false-flag chance at FPR {fpr:.2%} over {n_llm} LLM-decided checks)")
    print("| over the limit | fails | share of these fails | chance of at least that many false LLM flags |")
    print("|---|---|---|---|")
    for n in range(1, max_over + 2):
        label = str(n) if n <= max_over else f"{n} or more"
        chance = 1 - binom_cdf(n - 1, n_llm, fpr)
        print(f"| {label} | {by_over[n]} | {by_over[n] / len(fails):.1%} | {chance:.1%} |")
    print(f"| total | {len(fails)} | 100.0% | |")


def print_table(title: str, books: list[Workbook]) -> None:
    print(f"\n{title}")
    print("| difficulty | workbooks | Q1 | median | Q3 | mean | min | max |")
    print("|---|---|---|---|---|---|---|---|")
    for d in DIFFICULTIES + ("All",):
        xs = [b.llm_flags for b in books if d == "All" or b.difficulty == d]
        if not xs:
            print(f"| {d} | 0 | – | – | – | – | – | – |")
            continue
        q1, med, q3 = np.percentile(xs, [25, 50, 75])
        print(f"| {d} | {len(xs)} | {num(q1)} | {num(med)} | {num(q3)} | {np.mean(xs):.1f} | {min(xs)} | {max(xs)} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    parser.add_argument("--min-tasks", type=int, default=pl.MIN_TASKS_DEFAULT,
                        help=f"drop cohorts graded on fewer tasks (default {pl.MIN_TASKS_DEFAULT}, as the leaderboard)")
    parser.add_argument("--max-over", type=int, default=MAX_OVER_DEFAULT,
                        help=f"list fails 1..N over the limit one by one, the rest in one bucket (default {MAX_OVER_DEFAULT})")
    parser.add_argument("--fpr", type=float, default=FPR,
                        help=f"LLM false-flag rate for the chance column (default pass_rule.FPR, {FPR})")
    args = parser.parse_args()

    tiers = load_tiers()
    conn = psycopg2.connect(pl.database_url(args))
    try:
        books = fetch(conn, args.min_tasks, tiers)
    finally:
        conn.close()

    print_summary(books, args.max_over, args.fpr)
    passes = [b for b in books if b.passed]
    fails = [b for b in books if not b.passed and b.answer_ok]
    print("\nEntries below: LLM flags per workbook. Slack and overage use the rule flags.")
    print_table("1. Passes", passes)
    print_table("2. Fails with a right final answer", fails)
    print_table("3. Passes with 1 slack", [b for b in passes if b.slack == 1])
    print_table("4. Passes with 1-2 slack", [b for b in passes if b.slack in (1, 2)])
    print_table("5. Fails 1 over the limit", [b for b in fails if b.overage == 1])
    print_table("6. Fails 2 over the limit", [b for b in fails if b.overage == 2])


if __name__ == "__main__":
    main()
