#!/usr/bin/env python
"""Every number in the paper that depends on the pass rule, recomputed under the current pass_rule.py.

Change TIER_TOLERANCE / FPR in pass_rule.py, rerun this, and read off what moves in the draft. Each line
names where the number sits in the paper, the value the draft currently prints (PAPER_* below, the
2026-09-25 draft) and the value now; lines that differ are marked "<<<<<<".

  rule        Sec 3 "Metrics": k_t per tier, N_t, and P(pass | perfect) at FPR
  leaderboard Fig 3 bars and 0% list, Abstract / Intro top pass rate and the 0% cohorts, Fig 3 caption
              and Conclusion ("only a few ... non-zero"), Sec 6.1 same-model harness gap, App H in-house
  judge       Sec 5.2 "Judge Backbone Model": pass-rate ranking under Sol and Fable (stage 3)
  leniency    Sec 3 "upper bounds" / Fig 8 caption: P(pass | imperfect) at the annotated FPR, FNR

Not pass-rule dependent, so not here: scores (Sec 5.2 self-consistency, Sec 6.1 effort ablation),
accuracy (Fig 7), judge FPR/FNR themselves (calc_judge_stat.py).

Reads the v2 database (select only). Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/pass_rule_all.py
"""

import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "additional_experiments")]

import pass_rule  # noqa: E402
from calc_judge_stat import GRADING_IDS, fetch as fetch_annotations, metrics, workbook_truth, pass_given_imperfect  # noqa: E402
from plot_leaderboard import MIN_TASKS_DEFAULT, database_url, fetch as fetch_leaderboard, ranked  # noqa: E402

# What the 2026-09-25 draft prints; paper tier k1..k4 = pass_rule tier 0..3.
PAPER_K = {0: 1, 1: 3, 2: 4, 3: 3}
PAPER_N = {0: 11, 1: 41, 2: 52, 3: 24}
PAPER_CONFIDENCE = 0.981
PAPER_TOP = 23  # Abstract and Intro; Fig 3 itself shows 22
PAPER_FIG3 = {  # pass rate % per cohort, 0 for the "0% pass rate" list
    "Fable 5.1-Code": 22, "Fable 5.1-Cowork": 21, "Astra-Work": 15, "Opus 5-Cowork": 15, "Opus 5-Code": 15,
    "Qwen 3.8-Code": 11, "Astra-Code": 8, "GPT-6-Pro": 6, "Grok 4.6-Code": 5, "Kimi K3-Code": 4,
    "Fable 5.1-Excel": 2, "Opus 5-Excel": 2,
    "Fable 5.1": 0, "Sol-Excel": 0, "Astra": 0, "Qwen 3.8": 0, "Grok 4.6": 0, "Kimi K3": 0,
    "GLM 5.3-Code": 0, "Gemini 3.8-Code": 0, "Gemini 3.8": 0,
}
PAPER_JUDGE_RANKING_SAME = True  # Sec 5.2: "On both score and pass rate, all 3 agents maintain their relative rankings"
SAME_MODEL = ("Fable 5.1", "Opus 5")  # Sec 6.1 names Fable 5.1 across Cowork / Code / Excel


TODO: list[tuple[str, str, str, str]] = []  # (where, quantity, paper, now) for every spot to update


def flag(where: str, what: str, paper, now) -> str:
    """'  <<<<<<' and a TODO entry when the draft's value differs from now's, else ''."""
    if str(paper) == str(now):
        return ""
    TODO.append((where, what, str(paper), str(now)))
    return "  <<<<<<"


def line(where: str, what: str, paper, now) -> None:
    flag_ = flag(where, what, paper, now)
    print(f"{where:<26}{what:<40}{str(paper):>14}{str(now):>14}{flag_}")


def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 90 - len(title)))
    print(f"{'where':<26}{'quantity':<40}{'paper':>14}{'now':>14}")


def rule(tiers) -> None:
    section(f"Pass rule (FPR {pass_rule.FPR:.2%})")
    counts = pass_rule.tier_counts(tiers)
    conf, per_tier = pass_rule.confidence(tiers)
    fmt = lambda d: "/".join(str(d[t]) for t in pass_rule.TIERS)  # noqa: E731
    line("Sec 3 Metrics", "k_t, tiers 0..3", fmt(PAPER_K), fmt(pass_rule.TIER_TOLERANCE))
    line("Sec 3 Metrics", "N_t, tiers 0..3", fmt(PAPER_N), fmt(counts))
    line("Sec 3 Metrics", "P(pass | perfect)", f"{PAPER_CONFIDENCE:.3f}", f"{conf:.3f}")
    print("  per tier P(Bin(N_t, FPR) <= k_t): " + ", ".join(f"t{t} {per_tier[t]:.4f}" for t in pass_rule.TIERS))


def leaderboard(tiers) -> None:
    conn = psycopg2.connect(database_url(SimpleNamespace(database_url=None)))
    try:
        cohorts, n_tasks = fetch_leaderboard(conn, MIN_TASKS_DEFAULT, tiers)
    finally:
        conn.close()
    cohorts = ranked(cohorts, with_score=False)
    pct = {c.name: round(100 * c.pass_rate) for c in cohorts}

    section(f"Leaderboard ({len(cohorts)} cohorts, {n_tasks} live tasks)")
    top = cohorts[0]
    line("Abstract L27, Intro L108", f"top pass rate ({top.name})", f"{PAPER_TOP}%", f"{pct[top.name]}%")
    zero = sorted(c.name for c in cohorts if c.pass_rate == 0)
    paper_zero = sorted(n for n, p in PAPER_FIG3.items() if p == 0)
    line("Intro L109, Fig 3, Concl.", "cohorts at exactly 0%", len(paper_zero), len(zero))
    line("Fig 3 caption", "cohorts with non-zero pass rate", len(PAPER_FIG3) - len(paper_zero), len(cohorts) - len(zero))
    in_house = [c for c in cohorts if c.harness == "In-house"]
    line("App H L2534", "in-house cohorts that ever pass", 0, sum(c.pass_rate > 0 for c in in_house))

    print(f"\n  Fig 3, best first (pass % +- SE, n tasks with a verdict)")
    for i, c in enumerate(cohorts, 1):
        paper = PAPER_FIG3.get(c.name, "-")
        now = pct[c.name]
        mark = flag("Fig 3", f"{c.name} pass rate", f"{paper}%", f"{now}%")
        print(f"  {i:>2} {c.name:<20}{c.harness:<10}{100 * c.pass_rate:>6.1f} +-{100 * c.pass_se:>4.1f}"
              f"  n={c.pass_n:<4} paper {paper!s:>3}%  now {now:>3}%{mark}")
    for name in sorted(set(PAPER_FIG3) - set(pct)):
        mark = flag("Fig 3", f"{name} pass rate", f"{PAPER_FIG3[name]}%", "not on leaderboard")
        print(f"     {name:<20}in the paper but not on the leaderboard now{mark}")

    print("\n  Sec 6.1 L412: same model, different harness")
    for model in SAME_MODEL:
        row = [f"{c.name} {pct[c.name]}%" for c in cohorts if c.name == model or c.name.startswith(model + "-")]
        print(f"    {model:<10} " + ", ".join(row))


def judge_backbone() -> None:
    from _common import load, with_pass_verdicts, with_role
    from stage3 import FABLE, FABLE_ROLE, SOL, SOL_ROLE, order

    s3 = with_role(with_pass_verdicts(load()), SOL_ROLE, FABLE_ROLE)
    common = set.intersection(*(set(g["task_id"]) for _, g in s3.groupby("agent")))
    s3 = s3[s3["task_id"].isin(common)]
    rates = s3.pivot_table(index=["task_id", "agent"], columns="judge", values="passed", aggfunc="first")
    rates = rates.astype(float).groupby("agent").mean()
    sol, fable = order(rates[SOL]), order(rates[FABLE])

    section(f"Judge backbone, stage 3 ({len(common)} common tasks)")
    line("Sec 5.2 L393", "pass-rate ranking same, Sol vs Fable", PAPER_JUDGE_RANKING_SAME, sol == fable)
    print(f"  Sol    {sol}   " + ", ".join(f"{a.split('/')[-1]} {100 * v:.1f}%" for a, v in rates[SOL].items()))
    print(f"  Fable  {fable}   " + ", ".join(f"{a.split('/')[-1]} {100 * v:.1f}%" for a, v in rates[FABLE].items()))


def leniency() -> None:
    rows = fetch_annotations(GRADING_IDS)
    llm = Counter(
        v for r in rows for k, v in r["labels"].items()
        if v in ("TP", "FP", "TN", "FN") and tuple(k.split("::", 1)) not in pass_rule.DETERMINISTIC_CHECKS
    )
    m = metrics(llm)
    tiers = pass_rule.load_tiers()
    section(f"Leniency at the annotated LLM-decided FPR {m['FPR']:.2%}, FNR {m['FNR']:.2%}")
    line("Sec 3 L236 (FPR used)", "pass_rule.FPR vs annotated FPR", f"{pass_rule.FPR:.2%}", f"{m['FPR']:.2%}")
    print(f"  P(pass | perfect)    {pass_rule.confidence(tiers, m['FPR'])[0]:.3f}")
    print(f"  P(pass | imperfect)  {pass_given_imperfect([workbook_truth(r) for r in rows], m['FPR'], m['FNR']):.3g}"
          "   (Sec 3 'upper bounds', Fig 8 caption; the draft states no number)")


def main() -> None:
    tiers = pass_rule.load_tiers()
    rule(tiers)
    leaderboard(tiers)
    judge_backbone()
    leniency()
    todo()


def todo() -> None:
    """Every spot in the draft that differs from now, in one place."""
    print(f"\n== Update in the paper: {len(TODO)} spot(s) " + "=" * 60)
    if not TODO:
        print("nothing, the draft matches the current rule")
    for where, what, paper, now in TODO:
        print(f"<<<<<< {where:<26}{what:<40}{paper:>14} -> {now}")


if __name__ == "__main__":
    main()
