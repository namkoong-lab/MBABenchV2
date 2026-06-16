"""Ingest local task folders into the `tasks` table and S3.

Each task lives in its own folder under the tasks directory, with a
`starting_files/` subfolder and a `solution_files/` subfolder:

    Tasks/
        <task_name>/
            starting_files/<file>.xlsx
            solution_files/<file>.xlsx

For each task, this script uploads the starting and solution files to S3 under

    s3://<bucket>/BizbenchV2/tasks/<task_name>/starting_files/<file>
    s3://<bucket>/BizbenchV2/tasks/<task_name>/solution_files/<file>

and inserts one row into the `tasks` table with the resulting S3 URIs stored in
`task_starting_files` and `task_solution_files`.

The database connection string is read from the DATABASE_URL environment
variable. AWS credentials are read from the standard locations
(environment variables or ~/.aws/credentials).

Run with --dry-run first to preview every upload and insert without making any
changes.

Usage:
    python ingest_tasks.py --tasks-dir /path/to/Tasks --dry-run
    python ingest_tasks.py --tasks-dir /path/to/Tasks --limit 1   # first real run
    python ingest_tasks.py --tasks-dir /path/to/Tasks
"""

import argparse
import os
import sys
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras

# --- configuration -----------------------------------------------------------
S3_BUCKET = "biz-bench"
# Tasks are stored under this prefix in the bucket. Change it here in one place.
S3_PREFIX = "BizbenchV2/tasks"
DB_URL_ENV = "DATABASE_URL"


def get_db_connection():
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        sys.exit(f"Error: {DB_URL_ENV} not set.")
    return psycopg2.connect(db_url)


def preflight(s3, conn):
    """Fail loudly BEFORE any writes: confirm AWS, the bucket, and the DB."""
    ident = boto3.client("sts").get_caller_identity()
    print(f"AWS account={ident.get('Account')} arn={ident.get('Arn')}")
    s3.head_bucket(Bucket=S3_BUCKET)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    print(f"DB ({DB_URL_ENV}) + S3 bucket {S3_BUCKET!r} reachable.\n")


def collect_files(subdir: Path) -> list[Path]:
    """Return the regular files inside a subfolder, ignoring hidden and temp files.

    Skips:
      - files starting with '.'  (hidden files, e.g. .DS_Store)
      - files starting with '~$' (Excel temp files created when a file is open)
    """
    if not subdir.is_dir():
        return []
    return sorted(
        p for p in subdir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.startswith("~$")
    )


def upload(s3, local: Path, task_name: str, subfolder: str, dry_run: bool) -> str:
    key = f"{S3_PREFIX}/{task_name}/{subfolder}/{local.name}"
    uri = f"s3://{S3_BUCKET}/{key}"
    if dry_run:
        print(f"  [dry-run] would upload {local.name} -> {uri}")
    else:
        print(f"  upload {local.name} -> {uri}")
        s3.upload_file(str(local), S3_BUCKET, key)
    return uri


def task_exists(conn, task_name: str, task_source: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tasks WHERE task_name = %s AND task_source = %s LIMIT 1",
            (task_name, task_source),
        )
        return cur.fetchone() is not None


def insert_task(conn, task_name, task_source, starting_uris, solution_uris, dry_run):
    if dry_run:
        print(f"  [dry-run] would INSERT task_name={task_name!r} "
              f"source={task_source} "
              f"({len(starting_uris)} starting, {len(solution_uris)} solution)")
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (task_name, task_source, task_starting_files,
                               task_solution_files, deprecated)
            VALUES (%(name)s, %(source)s, %(starting)s, %(solution)s, false)
            RETURNING id
            """,
            {
                "name": task_name,
                "source": task_source,
                "starting": psycopg2.extras.Json(starting_uris),
                "solution": psycopg2.extras.Json(solution_uris),
            },
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    print(f"  inserted task id={new_id}")
    return new_id


def process_task(task_dir: Path, task_source: str, s3, conn, dry_run, counts):
    task_name = task_dir.name
    print(f"[{task_name}]")

    if task_exists(conn, task_name, task_source):
        print("  SKIP - a task with this (name, source) already exists.")
        counts["skipped"] += 1
        return

    starting = collect_files(task_dir / "starting_files")
    solution = collect_files(task_dir / "solution_files")

    if not starting and not solution:
        print("  SKIP - no files found in starting_files/ or solution_files/.")
        counts["skipped"] += 1
        return

    if not starting:
        print("  WARNING: no files found in starting_files/")
    if not solution:
        print("  WARNING: no files found in solution_files/")

    starting_uris = [upload(s3, p, task_name, "starting_files", dry_run) for p in starting]
    solution_uris = [upload(s3, p, task_name, "solution_files", dry_run) for p in solution]
    insert_task(conn, task_name, task_source, starting_uris, solution_uris, dry_run)
    counts["processed"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", type=Path, required=True)
    ap.add_argument("--task-source", default="jp")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.tasks_dir.is_dir():
        sys.exit(f"Tasks directory not found: {args.tasks_dir}")

    task_dirs = sorted(
        p for p in args.tasks_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    if not task_dirs:
        sys.exit(f"No task folders found in {args.tasks_dir}")
    if args.limit:
        task_dirs = task_dirs[:args.limit]

    print(f"Found {len(task_dirs)} task folder(s). "
          f"source={args.task_source!r} dry_run={args.dry_run}\n")

    s3 = boto3.client("s3")
    conn = get_db_connection()
    counts = {"processed": 0, "skipped": 0}
    try:
        preflight(s3, conn)
        for d in task_dirs:
            process_task(d, args.task_source, s3, conn, args.dry_run, counts)
    finally:
        conn.close()
    print(f"\nDone. processed={counts['processed']} skipped={counts['skipped']}")


if __name__ == "__main__":
    main()
