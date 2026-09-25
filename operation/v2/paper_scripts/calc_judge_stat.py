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


def main():
    report(fetch(GRADING_IDS), load_tiers())


if __name__ == "__main__":
    main()
