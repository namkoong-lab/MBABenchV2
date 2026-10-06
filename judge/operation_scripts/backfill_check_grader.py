"""Backfill `grader` ("deterministic" | "llm") on every rubric item of gradings.scored_results.check_scores
(maintainer 2026-10-06: which grader decided each item must be readable from Neon alone).

New rows get the key from judge._finalize_case (judge.tag_check_graders). This adds it to rows graded
before that landed: every live row with scored_results.accuracy_engine (judge v13 and v14; older rows
have no Python verdicts and are left alone). The value is derived exactly as the judge derives it:
"deterministic" where accuracy_engine.checks["<Category>/<name>"] has engine "harness" and counted
true, else "llm". Nothing else in scored_results changes; rows that already carry the key on every
item are skipped. Re-runnable.

    cd judge && uv run python operation_scripts/backfill_check_grader.py --benchmark v2            # dry run
    cd judge && uv run python operation_scripts/backfill_check_grader.py --benchmark v2 --write
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

_judge_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_judge_root))
sys.path.insert(0, str(_judge_root / "main_scripts"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from utils.misc_utils import get_db_url, load_project_configs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--benchmark", default="v2", choices=("v1", "v2"))
    ap.add_argument("--write", action="store_true", help="update the rows (default: report only)")
    ap.add_argument("--min-judge-version", type=int, default=13)
    ap.add_argument("--grading-ids", type=int, nargs="*", help="only these rows")
    args = ap.parse_args()
    load_project_configs(benchmark=args.benchmark)
    from main_scripts.judge import tag_check_graders  # after the config, as every judge driver does

    conn = psycopg2.connect(get_db_url())
    if not args.write:
        conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sql = """SELECT id, judge_version, scored_results FROM gradings
             WHERE judge_version >= %s AND scored_results->'accuracy_engine' IS NOT NULL
               AND scored_results->'check_scores' IS NOT NULL"""
    params: list = [args.min_judge_version]
    if args.grading_ids:
        sql += " AND id = ANY(%s)"
        params.append(args.grading_ids)
    cur.execute(sql + " ORDER BY id", params)
    rows = cur.fetchall()
    stats = {"rows": len(rows), "already_tagged": 0, "updated": 0, "deterministic": 0, "llm": 0}
    by_version: dict = {}
    for r in rows:
        sr = r["scored_results"] if isinstance(r["scored_results"], dict) else json.loads(r["scored_results"])
        items = [it for cat in (sr.get("check_scores") or {}).values() for it in (cat or {}).values() if isinstance(it, dict)]
        if items and all("grader" in it for it in items):
            stats["already_tagged"] += 1
            continue
        new = copy.deepcopy(sr)
        counts = tag_check_graders(new)
        stats["deterministic"] += counts["deterministic"]
        stats["llm"] += counts["llm"]
        by_version[r["judge_version"]] = by_version.get(r["judge_version"], 0) + 1
        if args.write:
            cur.execute("UPDATE gradings SET scored_results = %s WHERE id = %s", (json.dumps(new), r["id"]))
        stats["updated"] += 1
    if args.write:
        conn.commit()
    print(f"{'WROTE' if args.write else 'DRY RUN'}: {stats}; rows needing the key by judge_version: {by_version}")


if __name__ == "__main__":
    main()
