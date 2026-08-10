"""
Download task files from S3 for specific task IDs.

Usage:
    source judge/project_configs.sh
    python judge/operation_scripts/get_tasks.py TASK_ID [TASK_ID ...]
    python judge/operation_scripts/get_tasks.py 1 2 3
"""

import argparse
import sys
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import load_project_configs

import os

# Load configs into env vars
load_project_configs()


def get_db_connection():
    db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        print("Error: BIZBENCHJUDGE_KEYS_DATABASE_URL not set.")
        sys.exit(1)
    return psycopg2.connect(db_url)


def get_scratch_path():
    scratch = os.environ.get("BIZBENCHJUDGE_PATHS_SCRATCH_PATH")
    if not scratch:
        print("Error: BIZBENCHJUDGE_PATHS_SCRATCH_PATH not set.")
        sys.exit(1)
    return Path(scratch)


def get_tasks_by_ids(conn, task_ids):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_starting_files, task_solution_files,
                   task_source, deprecated, deprecated_reason, created_at, updated_at, old_id
            FROM tasks
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            (task_ids,),
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
    parser = argparse.ArgumentParser(description="Download task files from S3 by task ID")
    parser.add_argument("task_ids", nargs="+", type=int, help="Task IDs to download")
    args = parser.parse_args()

    conn = get_db_connection()
    try:
        tasks = get_tasks_by_ids(conn, args.task_ids)
    finally:
        conn.close()

    found_ids = {t["id"] for t in tasks}
    missing_ids = set(args.task_ids) - found_ids
    if missing_ids:
        print(f"Warning: task IDs not found: {sorted(missing_ids)}")

    if not tasks:
        print("No tasks found.")
        return

    scratch = get_scratch_path()
    output_dir = scratch / "tasks"
    s3_client = boto3.client("s3")

    for task in tasks:
        task_dir = output_dir / f"task_id={task['id']}"
        print(f"Downloading [{task['id']}] {task['task_name']} -> {task_dir}")
        download_task_files(s3_client, task, task_dir)

    print(f"\nDone. Files saved to {output_dir}/")


if __name__ == "__main__":
    main()
