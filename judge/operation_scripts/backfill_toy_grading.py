"""Backfill toy gradings that grade_toy.py parked as pending_toy_grading.json
(the row is written locally when the Neon insert fails twice — a dropped SSL
session during a 40-minute grading, or a network wobble at write time).

Usage (repo root):
    python judge/operation_scripts/backfill_toy_grading.py [--dry-run] [PATH ...]

With no PATH it walks judge/scratch/toy_runs/ for every pending file. A file
whose (run_id, check_no, variant, repeat_no) row already exists is skipped;
a backfilled file is renamed *.backfilled.json so it is never inserted twice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "main_scripts"))

import psycopg2  # noqa: E402

import grade_from_db as gfd  # noqa: E402
import grade_toy as gt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    paths = [Path(p) for p in args.paths] or sorted((HERE.parent / "scratch" / "toy_runs").rglob("pending_toy_grading.json"))
    if not paths:
        print("nothing pending"); return 0
    gt.load_project_configs(benchmark="v2")  # toys are v2-only; picks the DB URL like grade_toy does
    conn = None if args.dry_run else gfd.get_db_connection()
    n = 0
    for p in paths:
        d = json.loads(p.read_text())
        key = (d["run_id"], d["toy"]["check_no"], d["variant"], int(d["repeat_no"]))
        print(f"{p}: run {key[0]} check {key[1]} {key[2]} repeat {key[3]} verdict={d['verdict'].get('verdict')}")
        if args.dry_run:
            continue
        with conn.cursor() as cur:
            cur.execute(f"select id from {gt.SCHEMA}.toy_gradings where run_id=%s and check_no=%s and variant=%s and repeat_no=%s", key)
            if cur.fetchone():
                print("  already in DB; skipping"); continue
        try:
            gid = gt.insert_grading(conn, d["run_id"], d["toy"], d["variant"], int(d["repeat_no"]), d["target"],
                                    d["result"], d["verdict"], d["versions"], d["model"])
        except psycopg2.Error as e:
            conn.rollback(); print(f"  INSERT FAILED: {e}"); continue
        p.rename(p.with_suffix(".backfilled.json"))
        print(f"  -> toy_gradings id {gid}"); n += 1
    print(f"backfilled {n} of {len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
