"""Shared loading for the stage 2/3/4 scripts: consolidated_gradings_stages_2_3_4.json as a DataFrame,
and the leaderboard's pass verdict per grading (pass_rule.task_verdict, the rule plot_leaderboard.py uses).
The JSON has no per-check verdicts, so check_scores is read (select only) from the v2 database by grading_id."""

import json
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

# This file lives at <repo>/operation/v2/paper_scripts/additional_experiments/, so the repo root is four levels up.
REPO_ROOT = Path(__file__).resolve().parents[4]
GRADINGS_JSON = REPO_ROOT / "operation" / "results" / "v2" / "consolidated_gradings_stages_2_3_4.json"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plot_difficulty_and_types import load_config  # noqa: E402
from pass_rule import (  # noqa: E402
    DETERMINISTIC_CHECKS,
    FLAG_MISTAKES,
    TIER_TOLERANCE,
    TIERS,
    load_tiers,
    pass_rate,
    task_verdict,
)

# Boolean parts of the pass rule added by with_pass_verdicts: the deterministic answer check and each tier's allowance.
OK_COLS = ["det_ok"] + [f"ok{t}" for t in TIERS]
VERDICT_COLS = ["verdict", "passed"] + OK_COLS


def load() -> pd.DataFrame:
    """One row per grading. `judge` drops the provider prefix (tensorblock/ and openai/ serve the same Sol);
    `agent` drops the harness prefix; `roles` is stage_roles as a set."""
    df = pd.DataFrame(json.loads(GRADINGS_JSON.read_text())["gradings"])
    df["judge"] = df["judge_model"].str.split("/").str[-1]
    df["agent"] = df["agent_model_name"].str.split("/").str[-1]
    df["roles"] = df["stage_roles"].apply(set)
    return df


def fetch_check_scores(grading_ids: list[int]) -> dict[int, dict]:
    """grading_id -> scored_results.check_scores, read from the v2 database (select only)."""
    with psycopg2.connect(load_config()["database"]["v2_url"]) as conn, conn.cursor() as cur:
        cur.execute("select id, scored_results->'check_scores' from gradings where id = any(%s)", (grading_ids,))
        return dict(cur.fetchall())


def check_flags(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (grading, scored check): grading_id, category, check, tier ("det" for DETERMINISTIC_CHECKS)
    and flagged (mistakes >= FLAG_MISTAKES), counted as pass_rule.task_verdict counts them."""
    tiers = load_tiers()
    rows = []
    for gid, cs in fetch_check_scores(df["grading_id"].tolist()).items():
        for cat, checks in (cs or {}).items():
            for name, res in checks.items():
                tier = "det" if (cat, name) in DETERMINISTIC_CHECKS else tiers.get((cat, name))
                if tier is None or res.get("unscored"):
                    continue
                rows.append((gid, cat, name, tier, res["mistakes"] >= FLAG_MISTAKES))
    return pd.DataFrame(rows, columns=["grading_id", "category", "check", "tier", "flagged"])


def with_pass_verdicts(df: pd.DataFrame) -> pd.DataFrame:
    """df plus the leaderboard's verdict per grading (pass_rule.task_verdict): `verdict` (the PassVerdict),
    `passed`, `det_ok` and `ok0`..`ok3` (tier t's flags within k_t). Gradings without a verdict (no
    check_scores, or an unscored critical check) are dropped with a note, as the leaderboard drops them."""
    tiers = load_tiers()
    check_scores = fetch_check_scores(df["grading_id"].tolist())
    v = df["grading_id"].map(lambda gid: task_verdict(check_scores.get(gid), tiers))
    if v.isna().any():
        print(f"{v.isna().sum()} gradings without a pass verdict, left out", file=sys.stderr)
    df, v = df[v.notna()].copy(), v[v.notna()]
    df["verdict"] = v
    df["passed"] = v.map(lambda x: x.passed)
    df["det_ok"] = v.map(lambda x: x.deterministic_ok)
    for t in TIERS:
        df[f"ok{t}"] = v.map(lambda x, t=t: x.flags[t] <= TIER_TOLERANCE[t])
    return df


def pass_pct(verdicts) -> str:
    """Pass rate of a set of PassVerdicts with its SE (pass_rule.pass_rate), in percent, e.g. ' 18.8 +- 3.9'."""
    p, err = pass_rate(list(verdicts))
    return f"{100 * p:5.1f} +-{100 * err:4.1f}"


def with_role(df: pd.DataFrame, *roles: str) -> pd.DataFrame:
    """Rows carrying any of the given stage roles."""
    return df[df["roles"].apply(lambda r: bool(r & set(roles)))]


def repeat_runs(df: pd.DataFrame) -> pd.DataFrame:
    """Stage 2 as attempt x run (run 0 = production, 1-2 = repeats in grading_id order), plus agent/task columns,
    and `verdict_<run>`, `passed_<run>`, `ok0_<run>`, ... when df went through with_pass_verdicts."""
    s2 = with_role(df, "stage2:sol_production", "stage2:sol_repeat").sort_values(["attempt_id", "grading_id"])
    s2 = s2.assign(run=s2.groupby("attempt_id").cumcount())
    wide = s2.pivot(index="attempt_id", columns="run", values="total_score")
    for col in VERDICT_COLS if "verdict" in s2 else []:  # verdict columns as `<col>_<run>`
        wide = wide.join(s2.pivot(index="attempt_id", columns="run", values=col).add_prefix(f"{col}_"))
    meta = s2.drop_duplicates("attempt_id").set_index("attempt_id")[["agent", "task_id", "task_name", "task_difficulty"]]
    return wide.join(meta)


def judge_noise(df: pd.DataFrame) -> dict[str, float]:
    """Sol's repeat-to-repeat noise on the stage 2 attempts: mean within-attempt SD, and mean |run_i - run_j|."""
    runs = repeat_runs(df)[[0, 1, 2]]
    pair_abs = np.concatenate([(runs[i] - runs[j]).abs().to_numpy() for i, j in combinations(runs.columns, 2)])
    return {"sd": runs.std(axis=1, ddof=1).mean(), "abs_diff": pair_abs.mean()}


def se(x) -> float:
    """Standard error of the mean."""
    x = np.asarray(x, dtype=float)
    return x.std(ddof=1) / np.sqrt(len(x))


def header(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 76 - len(title)))
