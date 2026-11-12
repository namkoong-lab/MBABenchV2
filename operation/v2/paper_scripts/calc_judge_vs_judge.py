#!/usr/bin/env python
"""Fable judge vs Sol judge on the same attempts: score rank correlation and pass rate (READ-ONLY).

Input is stage3_fable_vs_sol_grading_ids.json: one Fable grading and one Sol production grading
per attempt (Sol repeat rows are not used). Two numbers per cohort and over all attempts:

  Spearman   rank correlation of the two judges' total scores, from the scores in the JSON.
  pass rate  pass_rule.task_verdict on each grading's scored_results.check_scores, read (select
             only) from the v2 database by grading id; and how often the two verdicts match.

    python operation/v2/paper_scripts/calc_judge_vs_judge.py ~/Downloads/stage3_fable_vs_sol_grading_ids.json
"""

import argparse
import json
from pathlib import Path

import psycopg2
from scipy.stats import spearmanr

from pass_rule import load_tiers, task_verdict
from plot_difficulty_and_types import load_config


def fetch_check_scores(grading_ids: list[int]) -> dict[int, dict]:
    """grading_id -> scored_results.check_scores."""
    conn = psycopg2.connect(load_config()["database"]["v2_url"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("select id, scored_results->'check_scores' from gradings where id = any(%s)", (grading_ids,))
            return dict(cur.fetchall())
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("json_path", type=Path, help="stage3_fable_vs_sol_grading_ids.json")
    args = ap.parse_args()

    attempts = json.loads(args.json_path.read_text())["attempts"]
    ids = [a[k] for a in attempts for k in ("fable_grading_id", "sol_production_grading_id")]
    check_scores, tiers = fetch_check_scores(ids), load_tiers()
    verdict = {gid: task_verdict(check_scores.get(gid), tiers) for gid in ids}

    cohorts = list(dict.fromkeys(a["cohort"] for a in attempts))
    print(f"{'cohort':<16}{'n':>5}{'Spearman':>10}{'Fable pass':>16}{'Sol pass':>16}{'same verdict':>14}")
    for cohort in cohorts + ["all"]:
        rows = [a for a in attempts if cohort in ("all", a["cohort"])]
        rho = spearmanr([a["fable_score"] for a in rows], [a["sol_production_score"] for a in rows]).statistic
        # Pass rate over the attempts both judges gave a verdict (None = an unscored critical check).
        pairs = [(verdict[a["fable_grading_id"]], verdict[a["sol_production_grading_id"]]) for a in rows]
        pairs = [(f.passed, s.passed) for f, s in pairs if f is not None and s is not None]
        n = len(pairs)
        fable, sol, same = sum(f for f, _ in pairs), sum(s for _, s in pairs), sum(f == s for f, s in pairs)
        print(f"{cohort:<16}{len(rows):>5}{rho:>10.3f}{f'{fable}/{n} {fable / n:6.1%}':>16}"
              f"{f'{sol}/{n} {sol / n:6.1%}':>16}{same / n:>14.1%}")


if __name__ == "__main__":
    main()
