#!/usr/bin/env python
"""Stage 2: is the Sol judge consistent with itself?

90 attempts (30 tasks x 3 agents), each graded three times by the same Sol judge (production + two repeats).
Prints to stdout:

  noise        per-attempt SD and max-min range across the 3 runs, and how often the range passes 5 / 10 points
  signal       judge noise next to the spread of scores across attempts (the part the benchmark measures)
  agreement    Pearson correlation between each pair of runs
  leaderboard  each agent's mean score in each run, and whether the agent order changes between runs
  pass rate    the leaderboard's pass verdict (pass_rule.task_verdict): each agent's pass rate in each run,
               how many attempts keep the same verdict in all 3 runs, and the same for each part of the rule
               (deterministic answer check, tier 0-3 allowance) to show which part the judge noise moves;
               per agent, attempts passing in 0-3 of the runs; and each attempt that ever passes, run by run,
               with the part of the rule that failed it in the other runs
  per check    for each rubric check, how often the 3 runs split on flagging it: by tier, by category, and the
               least stable checks
  main metrics the numbers the paper cites: pooled score SD, 3/3 agreement per (attempt, criterion) next to
               a^3 + (1-a)^3 from the judge's annotated accuracy a, and pass-verdict disagreement between runs

Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/additional_experiments/stage2.py
"""

from itertools import combinations

from _common import (
    OK_COLS,
    TIER_TOLERANCE,
    TIERS,
    check_flags,
    header,
    judge_noise,
    load,
    pass_pct,
    repeat_runs,
    with_pass_verdicts,
    with_role,
)

RUNS = [0, 1, 2]
TOP_CHECKS = 15  # least stable checks listed
MIN_CELLS = 20  # attempts a check must be scored on to be listed
# Judge vs human labels, every annotated check (pass_rate_thoughts.md sec 8, 2026-09-23 calc_judge_stat.py tally).
JUDGE_ANNOTATION = {"TP": 203, "FP": 11, "TN": 803, "FN": 22}


def main() -> None:
    df = with_pass_verdicts(load())
    runs = repeat_runs(df)
    scores = runs[RUNS]
    sd = scores.std(axis=1, ddof=1)
    rng = scores.max(axis=1) - scores.min(axis=1)
    noise = judge_noise(df)
    print(f"Stage 2: {len(runs)} attempts, {runs['task_id'].nunique()} tasks, {runs['agent'].nunique()} agents, 3 Sol runs each")

    header("Noise: spread of the 3 runs on one attempt")
    print(f"mean SD              {sd.mean():6.2f} pts   (median {sd.median():.2f})")
    print(f"mean |run_i - run_j| {noise['abs_diff']:6.2f} pts")
    print(f"mean range (max-min) {rng.mean():6.2f} pts   (median {rng.median():.2f}, max {rng.max():.2f})")
    print(f"range > 5 pts        {(rng > 5).mean():6.1%} of attempts")
    print(f"range > 10 pts       {(rng > 10).mean():6.1%} of attempts")
    print(f"\n{'group':<34}{'n':>4}{'mean SD':>9}{'mean range':>12}")
    for col in ("agent", "task_difficulty"):
        for key, idx in runs.groupby(col).groups.items():
            print(f"{key:<34}{len(idx):>4}{sd[idx].mean():>9.2f}{rng[idx].mean():>12.2f}")

    header("Signal vs noise")
    between = scores.mean(axis=1).std(ddof=1)
    print(f"SD of scores across attempts (3-run mean)  {between:6.2f} pts")
    print(f"SD of the judge within an attempt          {sd.mean():6.2f} pts")
    print(f"share of score variance from judge noise   {sd.pow(2).mean() / scores.stack().var(ddof=1):6.1%}")

    header("Agreement between runs (Pearson r over attempts)")
    for i, j in combinations(RUNS, 2):
        print(f"run {i} vs run {j}   r = {scores[i].corr(scores[j]):.3f}")

    header("Leaderboard per run (mean score over the 30 tasks)")
    by_agent = runs.groupby("agent")[RUNS].mean()
    print(f"{'agent':<34}" + "".join(f"{f'run {r}':>9}" for r in RUNS) + f"{'max-min':>9}")
    for agent, row in by_agent.sort_values(0, ascending=False).iterrows():
        print(f"{agent:<34}" + "".join(f"{row[r]:>9.2f}" for r in RUNS) + f"{row.max() - row.min():>9.2f}")
    orders = {r: tuple(by_agent[r].sort_values(ascending=False).index) for r in RUNS}
    print("\nagent order " + ("identical in all 3 runs" if len(set(orders.values())) == 1 else "CHANGES between runs:"))
    if len(set(orders.values())) > 1:
        for r, order in orders.items():
            print(f"  run {r}: {' > '.join(order)}")
    for a, b in combinations(by_agent.index, 2):
        gaps = by_agent.loc[a] - by_agent.loc[b]
        print(f"gap {a} - {b}: " + ", ".join(f"{g:+.2f}" for g in gaps))

    header("Pass rate per run (% +- SE over the 30 tasks)")
    print(f"{'agent':<34}" + "".join(f"{f'run {r}':>14}" for r in RUNS))
    for agent, g in runs.groupby("agent"):
        print(f"{agent:<34}" + "".join(f"{pass_pct(g[f'verdict_{r}']):>14}" for r in RUNS))

    header("Verdict stability: attempts with the same verdict in all 3 runs")
    print(f"{'part of the rule':<34}{'same in 3/3':>12}{'ever true':>11}{'always true':>13}")
    for col in ["passed"] + OK_COLS:
        v = runs[[f"{col}_{r}" for r in RUNS]].astype(int)
        k = v.sum(axis=1)  # runs in which this part holds
        same = ((k == 0) | (k == 3)).mean()
        print(f"{col:<34}{same:>12.1%}{(k > 0).sum():>11}{(k == 3).sum():>13}")
    k = runs[[f"passed_{r}" for r in RUNS]].astype(int).sum(axis=1)
    print(f"\n{'agent':<34}{'n':>4}" + "".join(f"{f'pass {i}/3':>10}" for i in range(4)))
    for agent, idx in runs.groupby("agent").groups.items():
        print(f"{agent:<34}{len(idx):>4}" + "".join(f"{(k[idx] == i).sum():>10}" for i in range(4)))
    print(f"{'all':<34}{len(k):>4}" + "".join(f"{(k == i).sum():>10}" for i in range(4)))

    header("Attempts that pass in at least one run (fail: what broke; tN a/k = a flags in tier N, k allowed)")
    print(f"{'agent':<24}{'task':<30}" + "".join(f"{f'run {r}':<30}" for r in RUNS))
    for attempt_id, row in runs[k > 0].sort_values(["agent", "task_id"]).iterrows():
        cells = [verdict_cell(row[f"verdict_{r}"]) for r in RUNS]
        print(f"{row['agent']:<24}{row['task_name'][:28]:<30}" + "".join(f"{c:<30}" for c in cells))

    cells = per_check_stability(df)
    main_metrics(runs, cells)


def main_metrics(runs, cells) -> None:
    """The numbers the paper's judge-consistency paragraph cites, restated at the end of the output."""
    scores = runs[RUNS]
    v = scores.var(axis=1, ddof=1)  # v_a: variance of attempt a's score over the 3 gradings
    agree = 1 - cells["mixed"].mean()
    tp, fp, tn, fn = (JUDGE_ANNOTATION[k] for k in ("TP", "FP", "TN", "FN"))
    acc = (tp + tn) / (tp + fp + tn + fn)
    passed = runs[[f"passed_{r}" for r in RUNS]].astype(bool)
    pair_dis = lambda p: sum((p[i] != p[j]).mean() for i, j in combinations(p.columns, 2)) / 3  # noqa: E731
    ever = passed.any(axis=1)

    header("Main metrics")
    print(f"attempts / tasks / gradings per attempt     {len(runs)} / {runs['task_id'].nunique()} / {len(RUNS)}")
    print(f"pooled score SD, sqrt(mean_a v_a)           {v.mean() ** 0.5:.2f} / 100")
    print(f"(attempt, criterion) pairs agreeing 3/3     {agree:.1%}  of {len(cells)}")
    print(f"judge accuracy a (human annotation)         {acc:.1%}  (TP {tp}, FP {fp}, TN {tn}, FN {fn})")
    print(f"predicted a^3 + (1-a)^3                     {acc ** 3 + (1 - acc) ** 3:.1%}")
    print(f"pass verdict, two runs disagree             {pair_dis(passed):.1%}  over all {len(runs)} attempts")
    print(f"                                            {pair_dis(passed[ever]):.1%}  over the {ever.sum()} attempts passing in >= 1 run")
    print(f"attempts passing in all 3 / ever            {passed.all(axis=1).sum()} / {ever.sum()}")


def per_check_stability(df):
    """Each rubric check on the stage 2 attempts: how often the 3 runs split on flagging it. Returns the
    attempt x check cells scored in all 3 runs (`mixed` = the runs split)."""
    """Each rubric check on the stage 2 attempts: how often the 3 runs split on flagging it."""
    s2 = with_role(df, "stage2:sol_production", "stage2:sol_repeat")
    flags = check_flags(s2).merge(s2[["grading_id", "attempt_id"]], on="grading_id")
    cells = flags.groupby(["attempt_id", "category", "check", "tier"])["flagged"].agg(["sum", "count"]).reset_index()
    cells = cells[cells["count"] == 3]  # scored in all 3 runs
    k = cells["sum"]
    cells["mixed"] = (k > 0) & (k < 3)  # the runs split
    cells["disagree"] = k * (3 - k) / 3  # share of the 3 run pairs that differ
    cells["flag_rate"] = k / 3

    def table(by: list[str], label: str, rows=None) -> None:
        g = cells.groupby(by).agg(n=("mixed", "size"), flag=("flag_rate", "mean"),
                                  mixed=("mixed", "mean"), disagree=("disagree", "mean"))
        if rows is not None:
            g = rows(g)
        print(f"{label:<62}{'n':>5}{'flag rate':>11}{'runs split':>12}{'pair disagree':>15}")
        for key, r in g.iterrows():
            name = " / ".join(map(str, key)) if isinstance(key, tuple) else str(key)
            print(f"{name[:60]:<62}{r['n']:>5.0f}{r['flag']:>11.1%}{r['mixed']:>12.1%}{r['disagree']:>15.1%}")

    header("Per-check stability (attempt x check cells scored in all 3 runs)")
    print("runs split = share of cells not flagged the same in all 3 runs; pair disagree = share of run pairs that differ\n")
    table(["tier"], "tier", rows=lambda g: g.loc[["det"] + list(TIERS)])
    print(f"{'all':<62}{len(cells):>5}{cells['flag_rate'].mean():>11.1%}{cells['mixed'].mean():>12.1%}"
          f"{cells['disagree'].mean():>15.1%}\n")
    table(["category"], "category", rows=lambda g: g.sort_values("disagree", ascending=False))
    print()
    table(["tier", "category", "check"], f"{TOP_CHECKS} least stable checks (n >= {MIN_CELLS}), tier / category / check",
          rows=lambda g: g[g["n"] >= MIN_CELLS].sort_values("disagree", ascending=False).head(TOP_CHECKS))
    per_check = cells.groupby(["category", "check"])["mixed"].mean()
    print(f"\nchecks never split across runs: {(per_check == 0).sum()} of {len(per_check)}")
    return cells


def verdict_cell(v) -> str:
    """'pass', or 'fail' with each part of the rule it broke: 'det' and 'tN a/k' per tier over its allowance."""
    if v.passed:
        return "pass"
    broke = (["det"] if not v.deterministic_ok else []) + [
        f"t{t} {v.flags[t]}/{TIER_TOLERANCE[t]}" for t in TIERS if v.flags[t] > TIER_TOLERANCE[t]
    ]
    return "fail " + ", ".join(broke)


if __name__ == "__main__":
    main()
