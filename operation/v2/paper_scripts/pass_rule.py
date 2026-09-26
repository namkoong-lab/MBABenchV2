"""The v2 pass rule: one graded task's per-check verdicts -> pass / fail, and a cohort's pass rate.

Shared by plot_leaderboard.py (the paper's leaderboard) and additional_experiments/stage*.py, so every
pass rate in the paper comes from the same rule. Everything the rule depends on is in the box below.
"""

import csv
import math
from dataclasses import dataclass
from pathlib import Path

# This file lives at <repo>/operation/v2/paper_scripts/, so the repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]


# ==============================================================================
# ######## Pass Rate Derivation
# ==============================================================================
# A workbook passes when, in every Importance tier t, the judge flags at most k_t
# of that tier's checks. The judge is an LLM that flags a correct check with
# probability FPR, so a flawless workbook still collects about K*FPR flags over
# its K checks; the k_t are the per-tier allowance for that noise, strict where a
# real mistake matters most. The Confidence section below turns (FPR, k_t) into
# P(pass | perfect workbook).
#
# Where the calculation lives, in order:
#   parameters (just below)  every number the rule depends on
#   load_tiers               rubrics.csv -> Importance tier per check
#   task_verdict             THE MAIN PASS CALCULATION: one task's flags -> pass / fail
#   pass_rate                a cohort's verdicts -> pass rate, standard error
#
# Parameters, every number the rule depends on:
RUBRIC_CSV = (
    REPO_ROOT / "operation" / "v2" / "rubrics.csv"
)  # Category, Name, Importance per check
TIERS = (
    0,
    1,
    2,
    3,
)  # rubrics.csv Importance: 0 = ultra important ... 3 = not absolutely crucial
TIER_TOLERANCE = {
    0: 1,
    1: 2,
    2: 3,
    3: 2,
}  # k_t: flags allowed in tier t for the workbook to pass
# Decided by the deterministic answer checker (judge/utils/answer_check.py), not the LLM: no
# false positives, so a flag is a real mistake and fails the workbook. Kept out of the tiers.
DETERMINISTIC_CHECKS = {("Accuracy", "Final calculation accuracy")}
FLAG_MISTAKES = 1  # a check is flagged when its mistake count (0..5) reaches this
# P(judge flags a correct check), one number for every LLM-decided check. PLACEHOLDER: the
# 2026-09-23 judge_annotations tally, 11 FP / 814 correct checks.
FPR = 0.0161


@dataclass(frozen=True)
class PassVerdict:
    """One task's pass decision and what it rested on."""

    passed: bool
    deterministic_ok: bool  # no DETERMINISTIC_CHECKS flagged
    flags: dict[int, int]  # tier -> flagged LLM-decided checks
    n: dict[int, int]  # tier -> applicable, scored LLM-decided checks


def load_tiers(path: Path = RUBRIC_CSV) -> dict[tuple[str, str], int]:
    """(Category, Name) -> Importance tier from rubrics.csv ("0 = ultra important" -> 0), DETERMINISTIC_CHECKS left out."""
    tiers: dict[tuple[str, str], int] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if (
                not row["No."].strip()
                or (row["Category"], row["Name"]) in DETERMINISTIC_CHECKS
            ):
                continue
            tiers[(row["Category"], row["Name"])] = int(row["Importance"].split("=")[0])
    return tiers


def task_verdict(
    check_scores: dict | None, tiers: dict[tuple[str, str], int]
) -> PassVerdict | None:
    """MAIN PASS CALCULATION: the pass decision for one graded task, or None when it cannot be decided.

    check_scores is scored_results.check_scores: {category: {check: {mistakes, unscored, ...}}},
    holding only the checks applicable to the task. An unscored check (the judge skipped
    it) is unknown: dropped from the count, and fatal to the verdict when it is tier 0 or
    deterministic. Retired checks (in the judge's rubric but not in rubrics.csv) have no
    tier and are ignored.

        passed  <=>  no DETERMINISTIC_CHECKS flagged  and  flags_t <= k_t  for every tier t in TIERS
    """
    if not check_scores:
        return None
    deterministic_ok = True
    n = {t: 0 for t in TIERS}
    flags = {t: 0 for t in TIERS}
    for cat, checks in check_scores.items():
        for name, res in checks.items():
            if (cat, name) in DETERMINISTIC_CHECKS:
                if res.get("unscored"):
                    return None
                deterministic_ok &= res["mistakes"] < FLAG_MISTAKES
                continue
            tier = tiers.get((cat, name))
            if tier is None:
                continue
            if res.get("unscored"):
                if tier == 0:
                    return None
                continue
            n[tier] += 1
            if res["mistakes"] >= FLAG_MISTAKES:
                flags[tier] += 1
    passed = deterministic_ok and all(flags[t] <= TIER_TOLERANCE[t] for t in TIERS)
    return PassVerdict(passed, deterministic_ok, flags, n)


def pass_rate(verdicts: list[PassVerdict]) -> tuple[float, float]:
    """(pass rate, its standard error) over the tasks with a verdict, in [0, 1]."""
    if not verdicts:
        return 0.0, 0.0
    n = len(verdicts)
    p = sum(v.passed for v in verdicts) / n
    se = math.sqrt(p * (1 - p) / n) if n > 1 else 0.0
    return p, se


# ==============================================================================
# ######## Confidence
# ==============================================================================
# P(workbook deemed pass | workbook is perfect). A perfect workbook has no real
# mistake, so every flag is a false positive. DETERMINISTIC_CHECKS never raise
# one (factor 1). In tier t the LLM-decided flags are Binomial(N_t, FPR), N_t the
# tier's check count in rubrics.csv without DETERMINISTIC_CHECKS (tier 0: 11, not
# 12), and the tiers are independent, so
#
#     P(pass | perfect) = prod_t  P(Binomial(N_t, FPR) <= k_t)
#
# N_t counts every check in the rubric; a task where fewer apply is exposed to
# less noise, so this is the lower end for a perfect workbook.


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(Binomial(n, p) <= k)."""
    return sum(
        math.comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(0, min(k, n) + 1)
    )


def tier_counts(tiers: dict[tuple[str, str], int]) -> dict[int, int]:
    """N_t: rubrics.csv LLM-decided checks per Importance tier."""
    return {t: sum(1 for v in tiers.values() if v == t) for t in TIERS}


def confidence(
    tiers: dict[tuple[str, str], int], fpr: float = FPR
) -> tuple[float, dict[int, float]]:
    """P(pass | perfect workbook), and the per-tier factor P(Binomial(N_t, FPR) <= k_t)."""
    counts = tier_counts(tiers)
    per_tier = {t: binom_cdf(TIER_TOLERANCE[t], counts[t], fpr) for t in TIERS}
    return math.prod(per_tier.values()), per_tier


# P(pass) for a workbook with m_t real mistakes among tier t's N_t checks.
#
# 1. Deterministic check. A wrong answer is always flagged and always fails, so
#    P(pass) = 0; a right answer is never flagged (factor 1). Below, the answer is right.
# 2. Flags in tier t have two independent sources: the judge catches each real mistake
#    with probability 1 - FNR, and flags each correct check with probability FPR:
#
#        flags_t = hits_t + false_t,  hits_t ~ Binomial(m_t, 1 - FNR),
#                                     false_t ~ Binomial(N_t - m_t, FPR)
#
# 3. The tier is within its allowance when flags_t <= k_t. Split on the hits i (at most
#    k_t, since more hits alone exceed it), leaving k_t - i room for false flags:
#
#        P(tier t ok) = sum_{i=0..min(m_t, k_t)}  C(m_t, i) (1-FNR)^i FNR^(m_t-i)
#                                                 * P(Binomial(N_t - m_t, FPR) <= k_t - i)
#
# 4. Tiers are independent:  P(pass | m) = prod_t P(tier t ok).
#
# With m_t = 0 in every tier the sum is its i = 0 term, P(Binomial(N_t, FPR) <= k_t),
# and this is confidence(). Worked example at FPR 1.61%, FNR 12.70%, N = {11, 41, 52,
# 24}, m = {0: 2}: tier 0 is 0.0161 * 0.9913 (both missed) + 0.2217 * 0.8639 (one
# caught) = 0.2075; tiers 1-3 as for a perfect workbook, 0.9716 * 0.9998 * 1.0000;
# P(pass) = 0.202.


def pass_probability(
    tiers: dict[tuple[str, str], int], fpr: float, fnr: float, mistakes: dict[int, int]
) -> float:
    """P(pass) for a workbook with mistakes[t] real mistakes in tier t, at the given FPR and FNR."""
    out = 1.0
    for t, n in tier_counts(tiers).items():
        m, k = mistakes.get(t, 0), TIER_TOLERANCE[t]
        out *= sum(
            math.comb(m, i)
            * (1 - fnr) ** i
            * fnr ** (m - i)
            * binom_cdf(k - i, n - m, fpr)
            for i in range(min(m, k) + 1)
        )
    return out


# P(workbook deemed pass | workbook is imperfect). Imperfect = the answer passes the
# deterministic accuracy check, but at least one LLM-decided (judge) check is actually
# wrong. The answer being right, DETERMINISTIC_CHECKS are factor 1. Which judge checks are
# wrong is random: each tier-t check independently with probability q_t.
#
# 1. Each tier-t check is flagged when it is wrong and caught, or right and falsely flagged:
#
#        p_t = q_t (1 - FNR) + (1 - q_t) FPR,   flags_t ~ Binomial(N_t, p_t)
#
#    so, over every answer-right workbook (perfect or not),
#
#        P(pass | answer right) = prod_t P(Binomial(N_t, p_t) <= k_t)
#
# 2. Take the perfect workbooks out. An answer-right workbook has no wrong judge check with
#    probability P0 = prod_t (1 - q_t)^N_t, and then passes with P(pass | perfect)
#    (confidence()), so
#
#        P(pass | imperfect) = (P(pass | answer right) - P0 P(pass | perfect)) / (1 - P0)
#
# With q -> 0 this tends to P(pass) with one mistake; as q_t grows each p_t moves from FPR
# toward 1 - FNR and P(pass | imperfect) falls. Over N = sum N_t checks the mean number of
# real mistakes is sum q_t N_t, and past sum k_t (12) the workbook rarely passes.


def pass_given_imperfect(
    tiers: dict[tuple[str, str], int], fpr: float, fnr: float, q: dict[int, float]
) -> float:
    """P(pass | answer right, some judge check wrong), each tier-t judge check wrong with prob q[t]."""
    counts = tier_counts(tiers)
    p_pass = math.prod(
        binom_cdf(TIER_TOLERANCE[t], counts[t], q[t] * (1 - fnr) + (1 - q[t]) * fpr)
        for t in TIERS
    )
    p0 = math.prod((1 - q[t]) ** counts[t] for t in TIERS)
    return (p_pass - p0 * confidence(tiers, fpr)[0]) / (1 - p0)


# ==============================================================================
# ######## end of Pass Rate Derivation
# ==============================================================================
