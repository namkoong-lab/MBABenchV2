#!/usr/bin/env python
"""Judge error rates against human annotation, for a fixed set of gradings.

Reads public.judge_annotations (human TP/FP/TN/FN label per "Category::Check") for
the gradings in GRADING_IDS, keeps the latest annotation of each (highest revision,
then newest), and prints to stdout TP/FP/TN/FN with FPR, FNR, accuracy, precision,
recall, specificity and F1 for:

  per grading  each grading's own labels, with agent, task, judge version, annotator
  overall      every label of every listed grading, and the LLM-decided subset
               (DETERMINISTIC_CHECKS left out; the FPR plot_leaderboard.py uses)
  tier         each rubrics.csv Importance tier
  category     each rubric category, in rubrics.csv order

Positive class is "check failed": TP both say fail, FP the judge failed a passing
check, TN both say pass, FN the judge passed a failing check. FPR and FNR of the
pooled groups also carry a 95% Wilson interval, since they rest on few errors.
Last, P(pass | perfect) and P(pass | imperfect) under pass_rule.py at the
LLM-decided FPR and FNR, both in closed form (pass_rule.confidence and
pass_rule.pass_given_imperfect). Imperfect is any criterion actually wrong, each
wrong at the per-tier rate the human labels give (TP + FN).

Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/calc_judge_stat.py
"""

import csv
import importlib.util
import math
from collections import Counter
from pathlib import Path

import psycopg2
import psycopg2.extras

import pass_rule

# This file lives at <repo>/operation/v2/paper_scripts/, so the repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
RUBRIC_CSV = REPO_ROOT / "operation" / "v2" / "rubrics.csv"

GRADING_IDS = (
    2337,
    1244,
    1985,
    2117,
    2045,
    1257,
    1666,
    1356,
    1104,
    1121,
    1969,
    2109,
)  # gradings whose annotations enter the tally
# Decided by the deterministic answer checker, not the LLM (as in plot_leaderboard.py).
DETERMINISTIC_CHECKS = {("Accuracy", "Final calculation accuracy")}
LABELS = ("TP", "FP", "TN", "FN")
Z95 = 1.959964  # two-sided 95% normal quantile for the Wilson interval


def db_url() -> str:
    spec = importlib.util.spec_from_file_location("_cfg", REPO_ROOT / "config" / "python" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Config.load(REPO_ROOT / "config").as_dict()["database"]["v2_url"]


def load_tiers(path: Path = RUBRIC_CSV) -> dict[tuple[str, str], int]:
    """(Category, Name) -> Importance tier from rubrics.csv ("0 = ultra important" -> 0), in file order."""
    with path.open(newline="") as f:
        return {
            (row["Category"], row["Name"]): int(row["Importance"].split("=")[0])
            for row in csv.DictReader(f)
            if row["No."].strip()
        }


def fetch(grading_ids: tuple[int, ...]) -> list[dict]:
    """The latest annotation of each grading in grading_ids, with its agent and task."""
    conn = psycopg2.connect(db_url())
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        select distinct on (a.grading_id)
               a.grading_id, a.annotator_id, a.revision, a.created_at, a.labels,
               g.judge_version, ta.agent_model_name, ta.task_id
        from judge_annotations a
        join gradings g on g.id = a.grading_id
        join task_attempts ta on ta.id = a.attempt_id
        where a.grading_id = any(%s)
        order by a.grading_id, a.revision desc, a.created_at desc
        """,
        (list(grading_ids),),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def ratio(num: int, den: int) -> float:
    return num / den if den else math.nan


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if not n:
        return math.nan, math.nan
    p = k / n
    centre = (p + Z95**2 / (2 * n)) / (1 + Z95**2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n**2)) / (1 + Z95**2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x:.2%}"


def metrics(c: Counter) -> dict[str, float]:
    tp, fp, tn, fn = (c[k] for k in LABELS)
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    return {
        "FPR": ratio(fp, fp + tn),
        "FNR": ratio(fn, fn + tp),
        "Accuracy": ratio(tp + tn, tp + fp + tn + fn),
        "Precision": precision,
        "Recall": recall,
        "Specificity": ratio(tn, tn + fp),
        "F1": ratio(2 * precision * recall, precision + recall) if tp else math.nan,
    }


METRIC_NAMES = tuple(metrics(Counter()))


def print_table(title: str, groups: dict[str, Counter], key_header: str = "group") -> None:
    """One row per group: label counts, then every metric."""
    width = max(len(key_header), *(len(k) for k in groups))
    print(f"\n{title}")
    print(f"{key_header:<{width}}" + "".join(f"{k:>6}" for k in LABELS)
          + "".join(f"{k:>12}" for k in METRIC_NAMES))
    for name, c in groups.items():
        m = metrics(c)
        print(f"{name:<{width}}" + "".join(f"{c[k]:>6}" for k in LABELS)
              + "".join(f"{pct(m[k]):>12}" for k in METRIC_NAMES))


def print_intervals(groups: dict[str, Counter]) -> None:
    width = max(len(k) for k in groups)
    print("\n95% Wilson intervals")
    for name, c in groups.items():
        fp_lo, fp_hi = wilson(c["FP"], c["FP"] + c["TN"])
        fn_lo, fn_hi = wilson(c["FN"], c["FN"] + c["TP"])
        print(f"{name:<{width}}  FPR {c['FP']:>3}/{c['FP'] + c['TN']:<5} [{pct(fp_lo):>6}, {pct(fp_hi):>6}]"
              f"   FNR {c['FN']:>3}/{c['FN'] + c['TP']:<5} [{pct(fn_lo):>6}, {pct(fn_hi):>6}]")


def report(rows: list[dict], tiers: dict[tuple[str, str], int]) -> None:
    missing = sorted(set(GRADING_IDS) - {r["grading_id"] for r in rows})
    print(f"{len(rows)}/{len(GRADING_IDS)} gradings in GRADING_IDS annotated (positive = check failed)")
    if missing:
        print(f"no annotation for grading(s): {', '.join(map(str, missing))}")

    rows = sorted(rows, key=lambda r: (r["agent_model_name"], r["task_id"]))
    print("\nper grading (latest annotation)")
    print(f"{'grading':>7}  {'agent':<42} {'task':>4}  {'judge':>5}  {'annot':>5}  {'rev':>3}")
    for r in rows:
        print(f"{r['grading_id']:>7}  {r['agent_model_name']:<42} {r['task_id']:>4}  "
              f"{'v' + str(r['judge_version']):>5}  {r['annotator_id']:>5}  {r['revision']:>3}")
    print_table("per grading", {
        str(r["grading_id"]): Counter(v for v in r["labels"].values() if v in LABELS) for r in rows
    }, key_header="grading")

    overall = {"all": Counter(), "LLM-decided": Counter()}
    by_tier = {f"tier {t}": Counter() for t in sorted(set(tiers.values()))}
    by_category = {cat: Counter() for cat, _ in tiers}  # dict keeps rubrics.csv order
    for r in rows:
        for key, label in r["labels"].items():
            if label not in LABELS:
                continue
            check = tuple(key.split("::", 1))
            overall["all"][label] += 1
            if check not in DETERMINISTIC_CHECKS:
                overall["LLM-decided"][label] += 1
            by_tier.setdefault(f"tier {tiers[check]}" if check in tiers else "untiered", Counter())[label] += 1
            by_category.setdefault(check[0], Counter())[label] += 1
    by_category = {k: c for k, c in by_category.items() if c}

    print_table("overall, every annotated check", overall)
    print_table("by importance tier", by_tier, key_header="tier")
    print_table("by rubric category", by_category, key_header="category")
    print_intervals(overall | by_tier | by_category)
    print_pass_probability(overall["LLM-decided"], rows)


# ==============================================================================
# ######## P(pass | perfect) and P(pass | imperfect)
# ==============================================================================
# Both are closed forms in pass_rule.py, at the LLM-decided FPR and FNR above.
#
#   perfect    every criterion right, so every flag is a false positive:
#                  P(pass | perfect) = prod_t P(Binomial(N_t, FPR) <= k_t)
#              (pass_rule.confidence).
#
#   imperfect  the answer passes the deterministic accuracy check, but at least one
#              LLM-decided (judge) check is actually wrong. Each tier-t judge check is
#              wrong independently with probability q_t, so it is flagged with
#                  p_t = q_t (1 - FNR) + (1 - q_t) FPR,
#              and with the perfect workbooks (prob P0) taken out
#                  P(pass | imperfect) = (prod_t P(Binomial(N_t, p_t) <= k_t) - P0 P(pass | perfect))
#                                        / (1 - P0),       P0 = prod_t (1 - q_t)^N_t
#              (pass_rule.pass_given_imperfect, derivation there).
#
# q_t comes from the human labels on the annotated workbooks whose answer is right
# (mistake_rates): a check is actually wrong when its label is TP (judge flagged it) or FN
# (judge missed it), so q_t = (TP + FN) / labelled tier-t checks. Since the answer depends
# on q, it is also printed over a range of q (the same in every tier). The judge's actual
# verdict on those workbooks (TP + FP flags through the pass rule) is printed as a check.


def workbook_truth(row: dict) -> dict:
    """One annotated workbook, from its labels: real mistakes (TP/FN: the check really failed) and the judge's
    flags (TP/FP), each as the deterministic check plus a count per pass_rule tier."""
    tiers = pass_rule.load_tiers()
    out = {"det_wrong": False, "det_flag": False, "mistakes": Counter(), "flags": Counter(), "checks": Counter()}
    for key, label in row["labels"].items():
        check = tuple(key.split("::", 1))
        if label not in LABELS or (check not in DETERMINISTIC_CHECKS and check not in tiers):
            continue
        wrong, flag = label in ("TP", "FN"), label in ("TP", "FP")
        if check in DETERMINISTIC_CHECKS:
            out["det_wrong"] |= wrong
            out["det_flag"] |= flag
        else:
            out["mistakes"][tiers[check]] += wrong
            out["flags"][tiers[check]] += flag
            out["checks"][tiers[check]] += 1
    out["imperfect"] = not out["det_wrong"] and sum(out["mistakes"].values()) > 0
    out["judge_passed"] = not out["det_flag"] and all(
        out["flags"][t] <= pass_rule.TIER_TOLERANCE[t] for t in pass_rule.TIERS)
    return out


def mistake_rates(truths: list[dict]) -> dict[int, float]:
    """q_t: the share of labelled tier-t judge checks actually wrong, over the workbooks whose answer is right."""
    right = [w for w in truths if not w["det_wrong"]]
    return {t: ratio(sum(w["mistakes"][t] for w in right), sum(w["checks"][t] for w in right))
            for t in pass_rule.TIERS}


def pass_given_imperfect(truths: list[dict], fpr: float, fnr: float) -> float:
    """P(pass | answer right, some judge check wrong) at this FPR and FNR, at the annotated mistake rates."""
    return pass_rule.pass_given_imperfect(pass_rule.load_tiers(), fpr, fnr, mistake_rates(truths))


def print_pass_probability(c: Counter, rows: list[dict]) -> None:
    """P(pass | perfect) and P(pass | imperfect) under pass_rule at the LLM-decided FPR and FNR."""
    m = metrics(c)
    fpr, fnr = m["FPR"], m["FNR"]
    tiers = pass_rule.load_tiers()
    truths = [workbook_truth(r) for r in rows]
    imperfect = [w for w in truths if w["imperfect"]]
    q = mistake_rates(truths)
    counts = pass_rule.tier_counts(tiers)

    print(f"\nP(pass) under pass_rule at the LLM-decided FPR {pct(fpr)} and FNR {pct(fnr)}"
          f" (pass: flags_t <= k_t in every tier)")
    print(f"imperfect = answer passes the accuracy check, some judge check actually wrong; q_t from the "
          f"{len(imperfect)} annotated workbooks with the answer right")
    print(f"  {'tier':<6}{'N_t':>5}{'k_t':>5}{'q_t (wrong)':>13}{'p_t (flagged)':>15}")
    for t in pass_rule.TIERS:
        p_t = q[t] * (1 - fnr) + (1 - q[t]) * fpr
        print(f"  {t:<6}{counts[t]:>5}{pass_rule.TIER_TOLERANCE[t]:>5}{pct(q[t]):>13}{pct(p_t):>15}")

    print(f"\nP(pass | perfect)    {pass_rule.confidence(tiers, fpr)[0]:.3g}   every criterion right")
    print(f"P(pass | imperfect)  {pass_given_imperfect(truths, fpr, fnr):.3g}   "
          f"at the q_t above ({sum(q[t] * counts[t] for t in pass_rule.TIERS):.1f} real mistakes on average)")
    print(f"(check: the judge actually passed {sum(w['judge_passed'] for w in imperfect)} of those "
          f"{len(imperfect)} annotated imperfect workbooks)")

    print("\nP(pass | imperfect) by mistake rate q (the same in every tier)")
    print(f"  {'q':>6}{'mean real mistakes':>20}{'P(pass | imperfect)':>21}")
    for qq in (0.005, 0.01, 0.02, 0.03, 0.05, 0.10, 0.15):
        p = pass_rule.pass_given_imperfect(tiers, fpr, fnr, {t: qq for t in pass_rule.TIERS})
        print(f"  {qq:>6.1%}{qq * sum(counts.values()):>20.1f}{p:>21.3f}")


def main():
    report(fetch(GRADING_IDS), load_tiers())


if __name__ == "__main__":
    main()
