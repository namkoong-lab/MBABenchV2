"""
Download task files and associated attempt files from S3 for specific task IDs.
Only downloads attempts from specified models, excluding failed/deprecated attempts.

Usage:
    source judge/project_configs.sh
    python judge/operation_scripts/get_tasks_and_attempts.py TASK_ID [TASK_ID ...] --models MODEL [MODEL ...]
    python judge/operation_scripts/get_tasks_and_attempts.py 1 2 3 --models "gpt-4o" "claude-sonnet-4-20250514"
"""

import argparse
import sys
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os

from utils.misc_utils import load_project_configs

# Load configs into env vars
load_project_configs()

DEFAULT_MODELS = [
    "GPT-5.4 (Extended Pro)",
    "GPT-5.4 (Agent)",
    "chatgpt_excel_agent",
    "claude_excel_agent",
    "claude_web",
    "openpyxl_anthropic/claude-opus-4-6",
    "openpyxl_openai/gpt-5.4",
]

# Per-model prompt_version filter. When a model appears here, only attempts
# whose prompt_version matches the specified value are considered valid.
# Models not listed here are not filtered by prompt_version.
DEFAULT_MODELS_PROMPT_VERSION: dict[str, int] = {
    "openpyxl_openai/gpt-5.4": 1105,
    "openpyxl_anthropic/claude-opus-4-6": 1105,
    "claude_web": 8,
    "claude_excel_agent": 8,
    "chatgpt_excel_agent": 8,
    "GPT-5.4 (Extended Pro)": 9,
    "GPT-5.4 (Agent)": 9,
}


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


def get_all_active_tasks(conn):
    """Fetch all tasks that are not deprecated."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_starting_files, task_solution_files,
                   task_source, deprecated, deprecated_reason, created_at, updated_at, old_id
            FROM tasks
            WHERE deprecated = false
            ORDER BY id
            """
        )
        return cur.fetchall()


def get_attempts_for_tasks(conn, task_ids, model_names, prompt_versions=None):
    """Fetch non-failed, non-deprecated attempts for given tasks and models.

    Filters out attempts with empty attempt_files, applies the optional
    per-model prompt_version filter, then keeps only the latest attempt per
    (task_id, agent_model_name).

    Args:
        prompt_versions: dict mapping model name -> required prompt_version.
            Attempts for listed models are kept only if their prompt_version matches.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_id, agent_model_name, agent_model_type,
                   attempt_files, prompt_files, start_time, end_time,
                   time_taken_min, cost, prompt_version
            FROM task_attempts
            WHERE task_id = ANY(%s)
              AND agent_model_name = ANY(%s)
              AND agent_failed = false
              AND deprecated = false
              AND attempt_files IS NOT NULL
              AND attempt_files::text NOT IN ('null', '[]', '{}')
            ORDER BY task_id, agent_model_name, id DESC
            """,
            (task_ids, model_names),
        )
        rows = cur.fetchall()

    if prompt_versions:
        rows = [
            r
            for r in rows
            if r["agent_model_name"] not in prompt_versions
            or r["prompt_version"] == prompt_versions[r["agent_model_name"]]
        ]

    # Dedup: keep latest id per (task_id, agent_model_name). Rows are already
    # ordered by id DESC, so the first occurrence wins.
    seen: set[tuple[int, str]] = set()
    deduped = []
    for r in rows:
        key = (r["task_id"], r["agent_model_name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def get_all_attempts_for_tasks(conn, task_ids, model_names):
    """Fetch all attempts (including failed/deprecated) for given tasks and models."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM task_attempts
            WHERE task_id = ANY(%s)
              AND agent_model_name = ANY(%s)
            ORDER BY task_id, agent_model_name, id DESC
            """,
            (task_ids, model_names),
        )
        return cur.fetchall()


def write_all_attempts_file(all_attempts, task_id, model_name, model_dir):
    """Write a txt file listing all available attempts for a task/model pair."""
    relevant = [
        a
        for a in all_attempts
        if a["task_id"] == task_id and a["agent_model_name"] == model_name
    ]
    if not relevant:
        return

    model_dir.mkdir(parents=True, exist_ok=True)
    out_path = model_dir / "all_attempts.txt"
    with open(out_path, "w") as f:
        f.write(f"All attempts for task_id={task_id}, model={model_name}\n")
        f.write(f"Total: {len(relevant)}\n")
        f.write("=" * 80 + "\n\n")
        for a in relevant:
            for key, value in a.items():
                f.write(f"{key}: {value}\n")
            f.write("-" * 80 + "\n\n")


def parse_s3_uri(uri):
    """Parse an s3://bucket/key URI into (bucket, key)."""
    path = uri[5:]  # strip "s3://"
    bucket, _, key = path.partition("/")
    return bucket, key


def download_s3_file(s3_client, s3_uri, dest_path):
    """Download a single file from S3."""
    if not s3_uri or not s3_uri.startswith("s3://"):
        print(f"  Skipping invalid S3 URI: {s3_uri!r}")
        return
    bucket, key = parse_s3_uri(s3_uri)
    if not bucket or not key:
        print(f"  Skipping malformed S3 URI (empty bucket or key): {s3_uri!r}")
        return
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


def download_attempt_files(s3_client, attempt, attempt_dir):
    """Download attempt files for a single attempt."""
    attempt_files = attempt["attempt_files"] or []
    for s3_uri in attempt_files:
        filename = Path(s3_uri).name
        download_s3_file(s3_client, s3_uri, attempt_dir / filename)


def main():
    parser = argparse.ArgumentParser(
        description="Download task files and associated attempts from S3"
    )
    parser.add_argument("task_ids", nargs="*", type=int, help="Task IDs to download")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download all non-deprecated tasks (ignores task_ids)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help=f"Model names to filter attempts by (default: {DEFAULT_MODELS})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for downloaded files (default: $SCRATCH/tasks).",
    )
    args = parser.parse_args()

    if not args.all and not args.task_ids:
        parser.error("either provide task_ids or use --all")

    conn = get_db_connection()
    try:
        if args.all:
            tasks = get_all_active_tasks(conn)
            task_ids = [t["id"] for t in tasks]
        else:
            task_ids = args.task_ids
            tasks = get_tasks_by_ids(conn, task_ids)
        prompt_versions = (
            DEFAULT_MODELS_PROMPT_VERSION if args.models == DEFAULT_MODELS else None
        )
        attempts = get_attempts_for_tasks(conn, task_ids, args.models, prompt_versions)
        all_attempts = get_all_attempts_for_tasks(conn, task_ids, args.models)
    finally:
        conn.close()

    if not args.all:
        # Report missing task IDs
        found_ids = {t["id"] for t in tasks}
        missing_ids = set(args.task_ids) - found_ids
        if missing_ids:
            print(f"Warning: task IDs not found: {sorted(missing_ids)}")

    if not tasks:
        print("No tasks found.")
        return

    # Group attempts by task_id
    attempts_by_task = {}
    for attempt in attempts:
        attempts_by_task.setdefault(attempt["task_id"], []).append(attempt)

    if args.output_dir is not None:
        output_dir = args.output_dir
    else:
        output_dir = get_scratch_path() / "tasks"
    s3_client = boto3.client("s3")

    skipped_tasks = 0
    skipped_attempts = 0

    for task in tasks:
        task_id = task["id"]
        task_dir = output_dir / f"task_id={task_id}_name={task['task_name']}"

        # Skip task files if starting_files dir already has content
        starting_dir = task_dir / "starting_files"
        if starting_dir.exists() and any(starting_dir.iterdir()):
            print(f"Skipping task [{task_id}] {task['task_name']} (already downloaded)")
            skipped_tasks += 1
        else:
            print(f"Downloading task [{task_id}] {task['task_name']} -> {task_dir}")
            download_task_files(s3_client, task, task_dir)

        task_attempts = attempts_by_task.get(task_id, [])
        if not task_attempts:
            print(f"  No valid attempts found for task {task_id}")
            continue

        for attempt in task_attempts:
            model_dir = task_dir / "attempts" / f"model_{attempt['agent_model_name']}"
            attempt_dir = model_dir / f"attempt_id={attempt['id']}"

            if attempt_dir.exists() and any(attempt_dir.iterdir()):
                print(
                    f"  Skipping attempt [{attempt['id']}] "
                    f"model={attempt['agent_model_name']} (already downloaded)"
                )
                skipped_attempts += 1
            else:
                print(
                    f"  Downloading attempt [{attempt['id']}] "
                    f"model={attempt['agent_model_name']} -> {attempt_dir}"
                )
                download_attempt_files(s3_client, attempt, attempt_dir)

            write_all_attempts_file(
                all_attempts, task_id, attempt["agent_model_name"], model_dir
            )

    print(f"\nDone. Files saved to {output_dir}/")
    print(
        f"Tasks: {len(tasks)} ({skipped_tasks} skipped), Attempts: {len(attempts)} ({skipped_attempts} skipped)"
    )


if __name__ == "__main__":
    main()
