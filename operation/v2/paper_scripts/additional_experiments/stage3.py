#!/usr/bin/env python
"""Stage 3: does the agent ranking persist under a more expensive judge?

The same attempts graded by Sol (production) and by Fable 5.1, restricted to the tasks all three agents cover.
Prints to stdout:

  leaderboard     each agent's mean score (+- SE) and rank under each judge
  gaps            every agent pair's mean gap under each judge, and its per-task win/tie/loss count
  judge vs judge  per-attempt Pearson r, mean Fable - Sol offset per agent (a larger offset on the Fable agent
                  would be self-preference), and mean |Fable - Sol| next to Sol's own repeat noise from stage 2
  pass rate       the leaderboard's pass verdict (pass_rule.task_verdict) per agent under each judge, each
                  part of the rule under each judge, and how often the two judges give an attempt the same
                  verdict, next to how often two Sol runs do (stage 2)
  per check       how often the two judges flag a rubric check the same way on the same attempt, by tier, next
                  to two Sol runs on the 90 stage 2 attempts, and each judge's flag rate
  main metrics    the numbers the paper cites: ranking under each judge, score correlation and per-check
                  agreement (all attempts, and on the stage 2 attempts next to Sol vs Sol), how much stricter
                  Fable is, and Fable's offset on its own model next to the other agents

Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/additional_experiments/stage3.py
"""

from itertools import combinations

import pandas as pd

from _common import (
    OK_COLS,
    TIERS,
    VERDICT_COLS,
    check_flags,
    header,
    judge_noise,
    load,
    pass_pct,
    repeat_runs,
    se,
    with_pass_verdicts,
    with_role,
)

SOL, FABLE = "gpt-5.6-sol", "claude-fable-5-1"
SOL_ROLE, FABLE_ROLE = "stage3:sol_production", "stage3:fable_judge"
REPEAT_ROLES = ("stage2:sol_production", "stage2:sol_repeat")
RUNS = [0, 1, 2]
SHORT = {"gpt-6-astra-xhigh": "Astra", "claude-fable-5-1-max": "Fable (In-house)", "gemini-3.8-flash-high": "Gemini"}
SELF_AGENT = "claude-fable-5-1-max"  # the agent sharing the Fable judge's model


def main() -> None:
    df = with_pass_verdicts(load())
    s3 = with_role(df, SOL_ROLE, FABLE_ROLE)
    wide = s3.pivot_table(index=["task_id", "agent"], columns="judge", values="total_score").reset_index()
    common = set.intersection(*(set(g["task_id"]) for _, g in wide.groupby("agent")))
    wide = wide[wide["task_id"].isin(common)]
    agents = sorted(wide["agent"].unique())
    print(f"Stage 3: {len(common)} tasks common to all {len(agents)} agents, each attempt graded by Sol and Fable")

    header("Leaderboard under each judge (mean +- SE over tasks)")
    means = wide.groupby("agent")[[SOL, FABLE]].mean()
    ranks = means.rank(ascending=False).astype(int)
    print(f"{'agent':<34}{'Sol':>16}{'rank':>6}{'Fable':>16}{'rank':>6}")
    for agent in means.sort_values(SOL, ascending=False).index:
        g = wide[wide["agent"] == agent]
        print(f"{agent:<34}{means.at[agent, SOL]:>9.2f} +-{se(g[SOL]):>4.2f}{ranks.at[agent, SOL]:>6}"
              f"{means.at[agent, FABLE]:>9.2f} +-{se(g[FABLE]):>4.2f}{ranks.at[agent, FABLE]:>6}")
    same = (ranks[SOL] == ranks[FABLE]).all()
    print(f"\nagent ranking {'IDENTICAL' if same else 'DIFFERS'} under the two judges")

    header("Pairwise gaps (A - B; per-task wins/ties/losses for A)")
    by_task = {j: wide.pivot(index="task_id", columns="agent", values=j) for j in (SOL, FABLE)}
    print(f"{'A - B':<60}{'Sol gap':>9}{'W/T/L':>12}{'Fable gap':>11}{'W/T/L':>12}{'same sign':>11}")
    for a, b in combinations(agents, 2):
        cells = []
        for j in (SOL, FABLE):
            d = by_task[j][a] - by_task[j][b]
            cells.append((d.mean(), f"{(d > 0).sum()}/{(d == 0).sum()}/{(d < 0).sum()}"))
        agree = "yes" if (cells[0][0] > 0) == (cells[1][0] > 0) else "NO"
        print(f"{a + ' - ' + b:<60}{cells[0][0]:>+9.2f}{cells[0][1]:>12}{cells[1][0]:>+11.2f}{cells[1][1]:>12}{agree:>11}")

    header("Judge vs judge on the same attempt")
    diff = wide[FABLE] - wide[SOL]
    print(f"Pearson r (Sol, Fable) over {len(wide)} attempts   {wide[SOL].corr(wide[FABLE]):.3f}")
    print(f"mean Fable - Sol offset                       {diff.mean():+.2f} +- {se(diff):.2f} pts")
    print(f"mean |Fable - Sol|                            {diff.abs().mean():.2f} pts")
    print(f"mean |Sol run_i - Sol run_j| (stage 2 noise)  {judge_noise(df)['abs_diff']:.2f} pts")
    print(f"\n{'agent':<34}{'r':>7}{'Fable - Sol':>18}")
    for agent, g in wide.groupby("agent"):
        d = g[FABLE] - g[SOL]
        print(f"{agent:<34}{g[SOL].corr(g[FABLE]):>7.3f}{d.mean():>+11.2f} +-{se(d):>4.2f}")

    # verdicts on the same common tasks, one row per (task, agent), `<col>_<judge>` columns
    verdicts = s3[s3["task_id"].isin(common)].pivot_table(
        index=["task_id", "agent"], columns="judge", values=VERDICT_COLS, aggfunc="first"
    )
    verdicts.columns = [f"{col}_{j}" for col, j in verdicts.columns]
    verdicts = verdicts.reset_index()

    header("Pass rate under each judge (% +- SE over tasks)")
    print(f"{'agent':<34}{'Sol':>14}{'rank':>6}{'Fable':>14}{'rank':>6}")
    rates = verdicts.groupby("agent")[[f"passed_{SOL}", f"passed_{FABLE}"]].mean()
    ranks = rates.rank(ascending=False, method="min").astype(int)
    for agent, g in verdicts.groupby("agent"):
        print(f"{agent:<34}{pass_pct(g[f'verdict_{SOL}']):>14}{ranks.at[agent, f'passed_{SOL}']:>6}"
              f"{pass_pct(g[f'verdict_{FABLE}']):>14}{ranks.at[agent, f'passed_{FABLE}']:>6}")

    header("Each part of the rule under each judge (% of tasks within it, Sol / Fable)")
    print(f"{'agent':<34}" + "".join(f"{col:>16}" for col in OK_COLS))
    for agent, g in verdicts.groupby("agent"):
        print(f"{agent:<34}" + "".join(f"{100 * g[f'{c}_{SOL}'].mean():>8.1f} /{100 * g[f'{c}_{FABLE}'].mean():>5.1f}"
                                       for c in OK_COLS))

    header("Verdict agreement on the same attempt")
    runs = repeat_runs(df)
    print(f"{'part of the rule':<18}{'Sol vs Fable':>14}{'Sol vs Sol':>12}{'both':>7}{'Sol only':>10}{'Fable only':>12}{'neither':>9}")
    for col in ["passed"] + OK_COLS:
        s, f = verdicts[f"{col}_{SOL}"].astype(bool), verdicts[f"{col}_{FABLE}"].astype(bool)
        sol_sol = sum((runs[f"{col}_{i}"] == runs[f"{col}_{j}"]).mean() for i, j in combinations(range(3), 2)) / 3
        print(f"{col:<18}{(s == f).mean():>14.1%}{sol_sol:>12.1%}{(s & f).sum():>7}{(s & ~f).sum():>10}"
              f"{(~s & f).sum():>12}{(~s & ~f).sum():>9}")
    print("(Sol vs Sol: the stage 2 attempts, mean over the 3 run pairs)")

    checks, repeats = per_check_agreement(df)
    main_metrics(df, wide, verdicts, checks, repeats)


def per_check_agreement(df):
    """Each (attempt, check) scored by both judges, as `sol`/`fable` flagged columns, and each (attempt, check)
    on the stage 2 attempts scored in all 3 Sol runs, as columns 0-2. Prints agreement by tier."""
    s3 = with_role(df, SOL_ROLE, FABLE_ROLE)
    key = ["attempt_id", "category", "check", "tier"]
    f3 = check_flags(s3).merge(s3[["grading_id", "attempt_id", "judge"]], on="grading_id")
    checks = f3.pivot_table(index=key, columns="judge", values="flagged", aggfunc="first").dropna()
    checks = checks.rename(columns={SOL: "sol", FABLE: "fable"}).astype(bool).reset_index()

    s2 = with_role(df, *REPEAT_ROLES).sort_values(["attempt_id", "grading_id"])
    s2 = s2.assign(run=s2.groupby("attempt_id").cumcount())  # same run numbering as repeat_runs
    f2 = check_flags(s2).merge(s2[["grading_id", "attempt_id", "run"]], on="grading_id")
    repeats = f2.pivot_table(index=key, columns="run", values="flagged", aggfunc="first").dropna()
    repeats = repeats.astype(bool).reset_index()

    header("Per-check agreement (attempt x check scored by both judges)")
    print("agree = same flag / no-flag; Sol vs Sol = stage 2 attempts, mean over the 3 run pairs\n")
    print(f"{'tier':<8}{'n':>7}{'Sol flags':>11}{'Fable flags':>13}{'Sol vs Fable':>14}{'Sol only':>10}"
          f"{'Fable only':>12}{'Sol vs Sol':>12}")
    for tier in ["det"] + list(TIERS) + ["all"]:
        c = checks if tier == "all" else checks[checks["tier"] == tier]
        r = repeats if tier == "all" else repeats[repeats["tier"] == tier]
        s, f = c["sol"], c["fable"]
        print(f"{tier!s:<8}{len(c):>7}{s.mean():>11.1%}{f.mean():>13.1%}{(s == f).mean():>14.1%}"
              f"{(s & ~f).sum():>10}{(~s & f).sum():>12}{pair_agree(r):>12.1%}")
    return checks, repeats


def pair_agree(repeats) -> float:
    """Share of (attempt, check) cells two Sol runs flag the same way, mean over the 3 run pairs."""
    return sum((repeats[i] == repeats[j]).mean() for i, j in combinations(RUNS, 2)) / 3


def order(values: pd.Series) -> str:
    """Agents from best to worst, '=' between ties, e.g. 'Astra > Fable (In-house) = Gemini'."""
    out = ""
    for i, (agent, v) in enumerate(values.sort_values(ascending=False).items()):
        out += SHORT.get(agent, agent) if i == 0 else (" = " if v == prev else " > ") + SHORT.get(agent, agent)
        prev = v
    return out


def main_metrics(df, wide, verdicts, checks, repeats) -> None:
    """The numbers the paper's judge-choice paragraph cites, restated at the end of the output."""
    s3 = with_role(df, SOL_ROLE, FABLE_ROLE)
    scores = s3.pivot(index="attempt_id", columns="judge", values="total_score")
    per_agent = s3.drop_duplicates("attempt_id")["agent"].value_counts()
    means = wide.groupby("agent")[[SOL, FABLE]].mean()
    rates = verdicts.groupby("agent")[[f"passed_{SOL}", f"passed_{FABLE}"]].mean()

    # the 90 stage 2 attempts: 3 Sol runs and the Fable grading of the same attempt
    runs = repeat_runs(df)
    fable = with_role(df, FABLE_ROLE).set_index("attempt_id")
    runs["F"] = fable["total_score"].reindex(runs.index)
    r_ss = sum(runs[i].corr(runs[j]) for i, j in combinations(RUNS, 2)) / 3
    r_sf = sum(runs[i].corr(runs["F"]) for i in RUNS) / 3
    same = checks[checks["attempt_id"].isin(runs.index)]

    s, f = checks["sol"], checks["fable"]
    diff = wide[FABLE] - wide[SOL]
    offset = {a: (g[FABLE] - g[SOL]) for a, g in wide.groupby("agent")}
    ps, pf = verdicts[f"passed_{SOL}"].astype(bool), verdicts[f"passed_{FABLE}"].astype(bool)

    header("Main metrics")
    print(f"Fable-judge gradings (one per attempt)          {len(scores)}  ("
          + ", ".join(f"{SHORT.get(a, a)} {n}" for a, n in per_agent.items()) + ")")
    print(f"tasks common to all 3 agents (ranking, offset)  {wide['task_id'].nunique()}  ({len(wide)} attempts)")
    print("\nranking")
    for label, col, table in [("mean score", "", means), ("pass rate", "passed_", rates)]:
        o_s, o_f = order(table[f"{col}{SOL}"]), order(table[f"{col}{FABLE}"])
        print(f"  {label:<12} Sol    {o_s}")
        print(f"  {'':<12} Fable  {o_f}   {'(same)' if o_s == o_f else '(DIFFERS)'}")
    print("\nagreement                                       Sol vs Fable   Sol vs Sol")
    print(f"  score Pearson r, all {len(scores)} attempts            {scores[SOL].corr(scores[FABLE]):>10.3f}")
    print(f"  score Pearson r, the {len(runs)} stage 2 attempts      {r_sf:>10.3f}   {r_ss:>10.3f}")
    print(f"  per-check agreement, all {len(checks)} checks       {(s == f).mean():>9.1%}")
    print(f"  per-check agreement, stage 2 attempts ({len(same)})  "
          f"{(same['sol'] == same['fable']).mean():>9.1%}   {pair_agree(repeats):>9.1%}")
    print("\nFable is stricter than Sol")
    print(f"  check flag rate, Sol / Fable                  {s.mean():.1%} / {f.mean():.1%}")
    print(f"  checks where the judges split, Fable flags    {(~s & f).sum()} of {(s != f).sum()} "
          f"({(~s & f).sum() / (s != f).sum():.1%})")
    print(f"  mean score Fable - Sol                        {diff.mean():+.2f} +- {se(diff):.2f} pts")
    print(f"  attempts passing Sol only / Fable only        {(ps & ~pf).sum()} / {(~ps & pf).sum()}")
    for agent in rates.sort_values(f"passed_{SOL}", ascending=False).index:
        g = verdicts[verdicts["agent"] == agent]
        print(f"  pass rate {SHORT.get(agent, agent):<18} Sol / Fable  "
              f"{pass_pct(g[f'verdict_{SOL}']).strip()} / {pass_pct(g[f'verdict_{FABLE}']).strip()} %")
    print("\nFable does not favor Fable (mean Fable - Sol per agent)")
    for agent, d in sorted(offset.items(), key=lambda kv: -kv[1].mean()):
        print(f"  {SHORT.get(agent, agent):<18}{d.mean():>+8.2f} +- {se(d):.2f} pts")
    others = [a for a in offset if a != SELF_AGENT]
    for a in others:
        gap = offset[SELF_AGENT].mean() - offset[a].mean()
        print(f"  {SHORT[SELF_AGENT]} offset - {SHORT.get(a, a)} offset".ljust(48) + f"{gap:+.2f} pts")


if __name__ == "__main__":
    main()
