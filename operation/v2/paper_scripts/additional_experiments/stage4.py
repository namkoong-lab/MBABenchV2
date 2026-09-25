#!/usr/bin/env python
"""Stage 4: does Fable 5.1 (Claude Code) score higher with more thinking effort?

101 tasks, each run at low, high and max effort and graded once by Sol. Prints to stdout:

  by effort       mean (+- SE) and median score per effort level, overall and per difficulty tier
  paired steps    high - low, max - high, max - low: mean paired gap (+- SE) and per-task wins/ties/losses,
                  with the gap in units of Sol's repeat noise from stage 2 (one grading per attempt here)
  monotone        tasks where low < high < max
  pass rate       the leaderboard's pass verdict (pass_rule.task_verdict) per effort level, overall and per
                  difficulty tier, each part of the rule per effort level, and per step the tasks that start or
                  stop passing

Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/additional_experiments/stage4.py
"""

from _common import OK_COLS, with_pass_verdicts, header, judge_noise, load, pass_pct, se, with_role

EFFORTS = ["low", "high", "max"]
STEPS = [("high", "low"), ("max", "high"), ("max", "low")]
DIFFICULTIES = ["Easy", "Medium", "Medium-Hard", "Hard"]


def main() -> None:
    df = with_pass_verdicts(load())
    s4 = with_role(df, *(f"stage4:effort_ablation_{e}" for e in EFFORTS))
    wide = s4.pivot(index="task_id", columns="agent_effort", values="total_score")[EFFORTS]
    ok = {col: s4.pivot(index="task_id", columns="agent_effort", values=col)[EFFORTS].astype(bool)
          for col in ["passed"] + OK_COLS}  # task x effort, per part of the pass rule
    verdicts = s4.pivot(index="task_id", columns="agent_effort", values="verdict")[EFFORTS]
    difficulty = s4.drop_duplicates("task_id").set_index("task_id")["task_difficulty"]
    noise = judge_noise(df)["abs_diff"]
    print(f"Stage 4: {len(wide)} tasks x {len(EFFORTS)} effort levels, one Sol grading each")

    header("Score by effort (mean +- SE over tasks)")
    print(f"{'':<14}{'n':>4}" + "".join(f"{e:>16}" for e in EFFORTS))
    groups = [("all", wide)] + [(d, wide[difficulty[wide.index] == d]) for d in DIFFICULTIES]
    for name, g in groups:
        print(f"{name:<14}{len(g):>4}" + "".join(f"{g[e].mean():>9.2f} +-{se(g[e]):>4.2f}" for e in EFFORTS))
    print(f"{'median':<18}" + "".join(f"{wide[e].median():>16.2f}" for e in EFFORTS))

    header("Paired steps (per-task gap; W/T/L = tasks where the higher effort scores above/equal/below)")
    print(f"Sol repeat noise, mean |run_i - run_j| on one attempt: {noise:.2f} pts")
    print(f"\n{'step':<14}{'mean gap':>16}{'gap / noise':>13}{'W/T/L':>12}")
    for hi, lo in STEPS:
        d = wide[hi] - wide[lo]
        wtl = f"{(d > 0).sum()}/{(d == 0).sum()}/{(d < 0).sum()}"
        print(f"{hi + ' - ' + lo:<14}{d.mean():>+9.2f} +-{se(d):>4.2f}{d.mean() / noise:>13.2f}{wtl:>12}")
    print(f"\n{'max - low by tier':<18}{'n':>4}{'mean gap':>16}")
    for d in DIFFICULTIES:
        g = wide[difficulty[wide.index] == d]
        gap = g["max"] - g["low"]
        print(f"{d:<18}{len(g):>4}{gap.mean():>+9.2f} +-{se(gap):>4.2f}")

    header("Monotone in effort")
    mono = ((wide["low"] < wide["high"]) & (wide["high"] < wide["max"])).sum()
    print(f"low < high < max on {mono} of {len(wide)} tasks ({mono / len(wide):.1%}; 1/6 = 16.7% if effort did nothing)")

    header("Pass rate by effort (% +- SE over tasks)")
    print(f"{'':<14}{'n':>4}" + "".join(f"{e:>14}" for e in EFFORTS))
    for name, g in [("all", verdicts)] + [(d, verdicts[difficulty[verdicts.index] == d]) for d in DIFFICULTIES]:
        print(f"{name:<14}{len(g):>4}" + "".join(f"{pass_pct(g[e]):>14}" for e in EFFORTS))

    header("Each part of the rule by effort (% of tasks within it)")
    print(f"{'part of the rule':<18}" + "".join(f"{e:>9}" for e in EFFORTS))
    for col in OK_COLS:
        print(f"{col:<18}" + "".join(f"{100 * ok[col][e].mean():>9.1f}" for e in EFFORTS))

    header("Pass transitions (tasks that start / stop passing at the higher effort)")
    passed = ok["passed"]
    print(f"{'step':<14}{'start':>7}{'stop':>6}{'net':>6}")
    for hi, lo in STEPS:
        start, stop = (passed[hi] & ~passed[lo]).sum(), (~passed[hi] & passed[lo]).sum()
        print(f"{hi + ' - ' + lo:<14}{start:>7}{stop:>6}{start - stop:>+6}")


if __name__ == "__main__":
    main()
