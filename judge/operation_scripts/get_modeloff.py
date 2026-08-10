"""
Get all modeloff tasks and download their files from S3.

Usage:
    source judge/project_configs.sh
    python judge/operation_scripts/get_modeloff.py [--output-dir DIR]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import load_project_configs

# Load configs into env vars
load_project_configs()


def get_db_connection():
    db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        print("Error: BIZBENCHJUDGE_KEYS_DATABASE_URL not set.")
        sys.exit(1)
    return psycopg2.connect(db_url)


def get_default_output_dir():
    scratch = os.environ.get("BIZBENCHJUDGE_PATHS_SCRATCH_PATH")
    if not scratch:
        print("Error: BIZBENCHJUDGE_PATHS_SCRATCH_PATH not set.")
        sys.exit(1)
    return Path(scratch) / "modeloff_tasks"


def get_modeloff_tasks(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_starting_files, task_solution_files,
                   task_source, deprecated, deprecated_reason, created_at, updated_at, old_id
            FROM tasks
            WHERE task_source = 'modeloff' AND (deprecated IS NULL OR deprecated = false)
            ORDER BY id
            """
        )
        return cur.fetchall()


def get_deprecated_modeloff_tasks(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, deprecated_reason
            FROM tasks
            WHERE task_source = 'modeloff' AND deprecated = true
            ORDER BY id
            """
        )
        return cur.fetchall()


def parse_s3_uri(uri):
    """Parse an s3://bucket/key URI into (bucket, key)."""
    path = uri[5:]  # strip "s3://"
    bucket, _, key = path.partition("/")
    return bucket, key


def download_s3_file(s3_client, s3_uri, dest_path):
    """Download a single file from S3."""
    bucket, key = parse_s3_uri(s3_uri)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(bucket, key, str(dest_path))


def download_task_files(s3_client, task, task_dir):
    """Download starting and solution files for a task into task_dir."""
    starting_files = task["task_starting_files"] or []
    solution_files = task["task_solution_files"] or []

    starting_dir = task_dir / "starting_files"
    for s3_uri in starting_files:
        filename = Path(s3_uri).name
        download_s3_file(s3_client, s3_uri, starting_dir / filename)

    if solution_files:
        solution_dir = task_dir / "solution_files"
        for s3_uri in solution_files:
            filename = Path(s3_uri).name
            download_s3_file(s3_client, s3_uri, solution_dir / filename)


def main():
    default_dir = get_default_output_dir()
    parser = argparse.ArgumentParser(description="Download modeloff task files from S3")
    parser.add_argument(
        "--output-dir",
        default=str(default_dir),
        help=f"Root output directory (default: {default_dir})",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    conn = get_db_connection()
    try:
        tasks = get_modeloff_tasks(conn)
        deprecated_tasks = get_deprecated_modeloff_tasks(conn)
    finally:
        conn.close()

    with_solutions = []
    without_solutions = []

    for task in tasks:
        if task["task_solution_files"]:
            with_solutions.append(task)
        else:
            without_solutions.append(task)

    print(f"Total modeloff tasks: {len(tasks)}")
    print(f"  With solutions: {len(with_solutions)}")
    print(f"  Without solutions: {len(without_solutions)}")

    print("\n--- Tasks WITH solutions ---")
    for t in with_solutions:
        print(f"  [{t['id']}] {t['task_name']}")

    print("\n--- Tasks WITHOUT solutions ---")
    for t in without_solutions:
        print(f"  [{t['id']}] {t['task_name']}")

    print(f"\n--- Deprecated tasks ({len(deprecated_tasks)}) ---")
    for t in deprecated_tasks:
        reason = t["deprecated_reason"] or "No reason given"
        print(f"  [{t['id']}] {t['task_name']} — {reason}")

    # Download files
    s3_client = boto3.client("s3")

    for label, task_list in [("with_solutions", with_solutions), ("without_solutions", without_solutions)]:
        for task in task_list:
            task_name = task["task_name"] or f"task_{task['id']}"
            folder_name = f"task_id={task['id']}_{task_name}"
            task_dir = output_dir / label / folder_name
            print(f"\nDownloading [{task['id']}] {task_name} -> {task_dir}")
            download_task_files(s3_client, task, task_dir)

    print(f"\nDone. Files saved to {output_dir}/")


if __name__ == "__main__":
    main()
