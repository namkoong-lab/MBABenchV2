"""Download tasks from the `tasks` table and S3 into a local folder.

This script is READ-ONLY with respect to the database: it never inserts,
updates, or otherwise alters the `tasks` table. For each task of a given source
it recreates the task folder locally:

    <out-dir>/
        <task_name>/
            starting_files/<file>.xlsx
            solution_files/<file>.xlsx

The xlsx files are pulled from the S3 URIs stored in `task_starting_files` and
`task_solution_files`. The AI judgement is added separately by
estimate_task_times.py, which writes ai_judgement.json into each task folder.

The S3 bucket and the default task source are read from the two-tiered config
system under config/ (see config/config_default.yaml). The database URL is read
from the environment (DATABASE_URL); AWS credentials are read from the standard
locations (environment variables or ~/.aws/credentials).

Run with --dry-run first to preview every download without writing any files.
Files download to <repo root>/scratch/tasks by default; override with --out-dir.

Usage:
    python ingest_tasks.py --dry-run
    python ingest_tasks.py --limit 1                  # first real run
    python ingest_tasks.py --out-dir /path/to/Tasks   # custom destination
"""

import argparse
import sys
from pathlib import Path

import boto3
import psycopg2.extras

from config import Config

# Repo root is one level up from this scripts/ directory.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "scratch" / "tasks"


def get_db_connection(cfg):
    """Connect to Postgres using the configured connection string."""
    url = cfg.get("database.url")
    if not url:
        sys.exit(
            "Error: database.url is missing or empty. "
            "Set DATABASE_URL in your shell or config/config.yaml."
        )
    return psycopg2.connect(url)


def preflight(s3, conn, bucket):
    """Fail loudly BEFORE any work: confirm AWS, the bucket, and the DB."""
    ident = boto3.client("sts").get_caller_identity()
    print(f"AWS account={ident.get('Account')} arn={ident.get('Arn')}")
    s3.head_bucket(Bucket=bucket)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    print(f"DB + S3 bucket {bucket!r} reachable.\n")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    path = uri[5:]  # strip "s3://"
    bucket, _, key = path.partition("/")
    return bucket, key


def fetch_tasks(conn, task_source: str):
    """Read every non-deprecated task for the source (read-only)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT task_name, task_starting_files, task_solution_files
            FROM tasks
            WHERE task_source = %s
              AND (deprecated IS NULL OR deprecated = false)
            ORDER BY task_name
            """,
            (task_source,),
        )
        return cur.fetchall()


def download(s3, uri: str, dest_dir: Path, dry_run: bool) -> Path:
    """Download one S3 object into dest_dir, keeping its filename."""
    bucket, key = parse_s3_uri(uri)
    dest = dest_dir / Path(key).name
    if dry_run:
        print(f"  [dry-run] would download {uri} -> {dest}")
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"  download {uri} -> {dest}")
        s3.download_file(bucket, key, str(dest))
    return dest


def process_task(task, s3, out_dir: Path, dry_run: bool, counts):
    task_name = task["task_name"]
    print(f"[{task_name}]")

    task_dir = out_dir / task_name
    starting_uris = task["task_starting_files"] or []
    solution_uris = task["task_solution_files"] or []

    if not starting_uris:
        print("  WARNING: no starting files recorded.")
    if not solution_uris:
        print("  WARNING: no solution files recorded.")

    for uri in starting_uris:
        download(s3, uri, task_dir / "starting_files", dry_run)
    for uri in solution_uris:
        download(s3, uri, task_dir / "solution_files", dry_run)

    counts["processed"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Local directory to download task folders into "
        "(default: <repo root>/scratch/tasks).",
    )
    ap.add_argument(
        "--task-source", help="Task source to download (default from config)."
    )
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    if args.task_source is not None:
        cfg.set("tasks.default_source", args.task_source)

    task_source = cfg.require("tasks.default_source")
    bucket = cfg.require("aws.s3_bucket")

    s3 = boto3.client("s3")
    conn = get_db_connection(cfg)
    counts = {"processed": 0}
    try:
        preflight(s3, conn, bucket)
        tasks = fetch_tasks(conn, task_source)
        if args.limit:
            tasks = tasks[: args.limit]
        if not tasks:
            sys.exit(f"No tasks found for source={task_source!r}.")
        print(
            f"Found {len(tasks)} task(s). "
            f"source={task_source!r} out_dir={args.out_dir} dry_run={args.dry_run}\n"
        )
        for t in tasks:
            process_task(t, s3, args.out_dir, args.dry_run, counts)
    finally:
        conn.close()
    print(f"\nDone. processed={counts['processed']}")


if __name__ == "__main__":
    main()
