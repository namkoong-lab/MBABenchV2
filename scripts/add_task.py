"""Upload a new task's files to S3 and register it in the tasks table.

This script is the WRITE counterpart to ingest_tasks.py. For a given task name
it:
  1. Validates all local files exist before touching anything remote.
  2. Uploads each starting file to:
       s3://<bucket>/<prefix>/<task_name>/starting_files/<filename>
  3. Uploads each solution file to:
       s3://<bucket>/<prefix>/<task_name>/solution_files/<filename>
  4. Inserts a row into the tasks table with the resulting S3 URIs.

If the task already exists in the tasks table the script exits with an error
unless --force is given, which re-uploads the files and UPDATEs the existing
row's file lists.

Configuration (bucket, prefix, DB URL) is read from the two-tiered config
system under config/ — see config/config_default.yaml. DATABASE_URL must be
set in the environment. AWS credentials are read from ~/.aws/credentials or
AWS_* env vars.

Always dry-run first to preview every upload and the DB write without executing:

Usage:
    python scripts/add_task.py --dry-run \\
        --task-name MyTask \\
        --starting-files /path/to/MyTask.xlsx \\
        --solution-files "/path/to/MyTask - Solution.xlsx"

    python scripts/add_task.py \\
        --task-name MyTask \\
        --task-source jp \\
        --starting-files /path/to/MyTask.xlsx \\
        --solution-files "/path/to/MyTask - Solution.xlsx"
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import psycopg2

from config import Config

ROOT = Path(__file__).resolve().parent.parent


def get_db_connection(cfg):
    url = cfg.get("database.url")
    if not url:
        sys.exit(
            "Error: database.url is missing or empty. "
            "Set DATABASE_URL in your shell or config/config.yaml."
        )
    return psycopg2.connect(url)


def preflight(s3, conn, bucket):
    ident = boto3.client("sts").get_caller_identity()
    print(f"AWS account={ident.get('Account')} arn={ident.get('Arn')}")
    s3.head_bucket(Bucket=bucket)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    print(f"DB + S3 bucket {bucket!r} reachable.\n")


def task_exists(conn, task_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tasks WHERE task_name = %s", (task_name,))
        return cur.fetchone() is not None


def upload_file(s3, bucket: str, key: str, local_path: Path, dry_run: bool) -> str:
    uri = f"s3://{bucket}/{key}"
    if dry_run:
        print(f"  [dry-run] would upload {local_path} -> {uri}")
    else:
        print(f"  upload {local_path} -> {uri}")
        s3.upload_file(str(local_path), bucket, key)
    return uri


def register_task(
    conn,
    task_name: str,
    task_source: str,
    starting_uris: list[str],
    solution_uris: list[str],
    force: bool,
    dry_run: bool,
):
    exists = task_exists(conn, task_name)

    if dry_run:
        if exists:
            action = "UPDATE (--force)" if force else "INSERT — but task already exists; re-run with --force to overwrite"
        else:
            action = "INSERT"
        print(f"\n  [dry-run] DB {action}:")
        print(f"    task_name:           {task_name}")
        print(f"    task_source:         {task_source}")
        print(f"    task_starting_files: {starting_uris}")
        print(f"    task_solution_files: {solution_uris}")
        return

    if exists and not force:
        sys.exit(
            f"Error: task {task_name!r} already exists in the tasks table. "
            "Use --force to overwrite its file lists."
        )

    with conn.cursor() as cur:
        if exists:
            cur.execute(
                """
                UPDATE tasks
                   SET task_starting_files = %s,
                       task_solution_files = %s,
                       task_source         = %s,
                       updated_at          = CURRENT_TIMESTAMP
                 WHERE task_name = %s
                """,
                (json.dumps(starting_uris), json.dumps(solution_uris), task_source, task_name),
            )
            print(f"\n  DB UPDATE: task {task_name!r} file lists replaced.")
        else:
            cur.execute(
                """
                INSERT INTO tasks (task_name, task_source, task_starting_files, task_solution_files)
                VALUES (%s, %s, %s, %s)
                """,
                (task_name, task_source, json.dumps(starting_uris), json.dumps(solution_uris)),
            )
            print(f"\n  DB INSERT: task {task_name!r} registered.")
    conn.commit()


def main():
    ap = argparse.ArgumentParser(
        description="Upload a new task to S3 and register it in the tasks table."
    )
    ap.add_argument(
        "--task-name",
        required=True,
        help="Unique task name — becomes the S3 folder name and the tasks.task_name value.",
    )
    ap.add_argument(
        "--starting-files",
        nargs="+",
        required=True,
        metavar="FILE",
        help="Local path(s) to the starting file(s) to upload.",
    )
    ap.add_argument(
        "--solution-files",
        nargs="+",
        required=True,
        metavar="FILE",
        help="Local path(s) to the solution file(s) to upload.",
    )
    ap.add_argument(
        "--task-source",
        help="task_source value written to the DB (default: tasks.default_source from config).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="If the task already exists, re-upload files and UPDATE the DB row.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview all uploads and the DB write without executing any of them.",
    )
    args = ap.parse_args()

    # Resolve and validate all local files before touching anything remote.
    starting_paths = [Path(f).expanduser().resolve() for f in args.starting_files]
    solution_paths = [Path(f).expanduser().resolve() for f in args.solution_files]
    missing = [p for p in starting_paths + solution_paths if not p.is_file()]
    if missing:
        sys.exit(
            "Error: local file(s) not found:\n" + "\n".join(f"  {p}" for p in missing)
        )

    cfg = Config.load()
    task_source = args.task_source or cfg.require("tasks.default_source")
    bucket = cfg.require("aws.s3_bucket")
    prefix = cfg.require("aws.s3_prefix")  # e.g. "MBABenchV2/tasks"

    s3 = boto3.client("s3")
    conn = get_db_connection(cfg)

    try:
        preflight(s3, conn, bucket)

        task_name = args.task_name
        print(f"Task:     {task_name}")
        print(f"Source:   {task_source}")
        print(f"Dry run:  {args.dry_run}\n")

        print("Starting files:")
        starting_uris = []
        for p in starting_paths:
            key = f"{prefix}/{task_name}/starting_files/{p.name}"
            starting_uris.append(upload_file(s3, bucket, key, p, args.dry_run))

        print("Solution files:")
        solution_uris = []
        for p in solution_paths:
            key = f"{prefix}/{task_name}/solution_files/{p.name}"
            solution_uris.append(upload_file(s3, bucket, key, p, args.dry_run))

        register_task(
            conn, task_name, task_source,
            starting_uris, solution_uris,
            args.force, args.dry_run,
        )

    finally:
        conn.close()

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to execute.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
