"""Estimate expert-human completion time for each task using Gemini.

For every task of a given source, this script:
  1. reads the task row from the database (the `tasks` table),
  2. downloads its starting file(s) from S3,
  3. converts each worksheet to CSV text (Gemini does not accept .xlsx),
  4. asks Gemini for an estimate, in minutes, of how long an expert modeler
     would take to complete the task from that starting file (the solution file
     is never shown), and
  5. writes the estimate and the model's reasoning back to the row
     (`ai_time_estimate_min` and `ai_time_estimate_reasoning`).

Tasks that already have an estimate are skipped unless --force is given.

Environment:
    DATABASE_URL    Postgres connection string (the database holding `tasks`).
    GEMINI_API_KEY  Gemini API key.
    AWS credentials are read from the standard locations (env vars or
    ~/.aws/credentials).

Run with --dry-run first to download and convert without calling Gemini or
writing to the database.

Usage:
    python estimate_task_times.py --dry-run --limit 1   # verify download + conversion
    python estimate_task_times.py --limit 1             # first real estimate
    python estimate_task_times.py                        # all tasks for the source
    python estimate_task_times.py --force                # re-estimate everything
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import boto3
import pandas as pd
import psycopg2
import psycopg2.extras
from google import genai
from google.genai import types

# --- configuration -----------------------------------------------------------
# The Gemini model to use. Change it here in one place.
GEMINI_MODEL = "gemini-3.5-flash"
DB_URL_ENV = "DATABASE_URL"

# Seconds to sleep between API calls to avoid exhausting the per-minute
# token quota on the free tier. Increase if rate limit errors persist.
SLEEP_BETWEEN_CALLS = 7

# How many times to retry a single task after a 429 rate-limit response.
MAX_RETRIES = 3

# The expert baseline the estimate is measured against. Stating the reference
# (an expert, not a novice) keeps estimates comparable across tasks.
PERSONA = (
    "You are an expert financial modeler with over a decade of experience "
    "building valuation, three-statement, FP&A, and transaction models in Excel. "
    "You estimate how long a competent expert modeler -- not a novice -- would "
    "take to complete a modeling task from its starting file, accounting for "
    "reading and understanding the brief, building the structure and formulas "
    "(using proper formulas rather than hardcoded values), and checking the work. "
    "Your estimates are realistic and well-calibrated, given in minutes."
)


def get_db_connection():
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        sys.exit(f"Error: {DB_URL_ENV} not set.")
    return psycopg2.connect(db_url)


def preflight(s3, conn, model):
    """Fail loudly BEFORE doing work: confirm AWS, the bucket, and the DB."""
    ident = boto3.client("sts").get_caller_identity()
    print(f"AWS account={ident.get('Account')} arn={ident.get('Arn')}")
    s3.head_bucket(Bucket="biz-bench")
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    print(f"DB ({DB_URL_ENV}) + S3 reachable. model={model}\n")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    path = uri[5:]  # strip "s3://"
    bucket, _, key = path.partition("/")
    return bucket, key


def fetch_tasks(conn, task_source: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_starting_files, ai_time_estimate_min
            FROM tasks
            WHERE task_source = %s
              AND (deprecated IS NULL OR deprecated = false)
            ORDER BY id
            """,
            (task_source,),
        )
        return cur.fetchall()


def xlsx_to_csv_text(path: Path) -> str:
    """Convert every worksheet in an xlsx to CSV text, labeled by sheet name.

    Reads cell values (header=None keeps the raw grid; nothing is mistaken for a
    header row). All-empty rows and columns are dropped: a spreadsheet's "used
    range" is often far larger than its actual data (a stray cell or leftover
    formatting can push it out to thousands of empty rows), which would otherwise
    become enormous blocks of empty cells. Formulas are not needed here -- a
    starting file is mostly instructions, given inputs, and blank structure.
    """
    sheets = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
    parts = [f"# File: {path.name}"]
    for sheet_name, df in sheets.items():
        # Drop rows and columns that are entirely empty.
        df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
        parts.append(f"=== Sheet: {sheet_name} ===")
        parts.append(df.to_csv(index=False, header=False))
    return "\n".join(parts)


def starting_file_text(s3, starting_uris: list[str]) -> str:
    """Download each starting file from S3 and return the combined CSV text."""
    blocks = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for uri in starting_uris:
            bucket, key = parse_s3_uri(uri)
            dest = tmp / Path(key).name
            s3.download_file(bucket, key, str(dest))
            blocks.append(xlsx_to_csv_text(dest))
    return "\n\n".join(blocks)


def estimate_one(client, model: str, task_name: str, csv_text: str) -> tuple[float, str]:
    user_text = (
        "You are given the starting file for a financial modeling task, converted "
        "to CSV (one block per worksheet). It contains the instructions, the given "
        "inputs, and the blank structure the modeler must complete. Estimate how "
        "many minutes an expert modeler would take to complete the task.\n\n"
        f"Task name: {task_name}\n\n"
        f"Starting file contents:\n{csv_text}\n\n"
        'Respond with ONLY a JSON object of the form: '
        '{"estimate_minutes": <number>, "reasoning": "<one short paragraph>"}'
    )
    resp = client.models.generate_content(
        model=model,
        contents=user_text,
        config=types.GenerateContentConfig(
            system_instruction=PERSONA,
            response_mime_type="application/json",
        ),
    )
    data = json.loads(resp.text)
    return float(data["estimate_minutes"]), str(data.get("reasoning", ""))


def estimate_with_retry(client, model: str, task_name: str, csv_text: str) -> tuple[float, str]:
    """Call estimate_one, retrying on 429 rate-limit errors.

    When Gemini returns a 429 it includes a suggested retry delay in the error
    message. This function reads that delay and waits before trying again,
    up to MAX_RETRIES attempts total.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return estimate_one(client, model, task_name, csv_text)
        except Exception as e:  # noqa: BLE001
            error_str = str(e)
            is_rate_limit = "429" in error_str
            is_last_attempt = attempt == MAX_RETRIES - 1
            if is_rate_limit and not is_last_attempt:
                # Extract the suggested wait time from the error message.
                # Gemini returns something like 'retryDelay': '32s'.
                match = re.search(r"retryDelay.*?(\d+)s", error_str)
                wait = int(match.group(1)) + 5 if match else 60
                print(f"  Rate limited. Waiting {wait}s then retrying "
                      f"(attempt {attempt + 2}/{MAX_RETRIES})...")
                time.sleep(wait)
            else:
                raise


def update_task(conn, task_id: int, estimate_min: float, reasoning: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
               SET ai_time_estimate_min = %(est)s,
                   ai_time_estimate_reasoning = %(reason)s,
                   updated_at = now()
             WHERE id = %(id)s
            """,
            {"est": estimate_min, "reason": reasoning, "id": task_id},
        )
    conn.commit()


def process_task(task, s3, client, model, conn, dry_run, force, skip_names, counts):
    task_id = task["id"]
    task_name = task["task_name"]
    print(f"[{task_id}] {task_name}")

    if task_name in skip_names:
        print("  SKIP - in --skip list.")
        counts["skipped"] += 1
        return

    if task["ai_time_estimate_min"] is not None and not force:
        print("  SKIP - already has an estimate (use --force to redo).")
        counts["skipped"] += 1
        return

    starting_uris = task["task_starting_files"] or []
    if not starting_uris:
        print("  SKIP - no starting files.")
        counts["skipped"] += 1
        return

    try:
        csv_text = starting_file_text(s3, starting_uris)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED (download/convert): {type(e).__name__}: {e}")
        counts["failed"] += 1
        return

    if dry_run:
        print(f"  [dry-run] would send ~{len(csv_text)} chars to {model}; "
              f"would set ai_time_estimate_min")
        counts["would_process"] += 1
        return

    # Sleep before the API call to stay within the per-minute token quota.
    time.sleep(SLEEP_BETWEEN_CALLS)

    try:
        estimate_min, reasoning = estimate_with_retry(client, model, task_name, csv_text)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED (gemini): {type(e).__name__}: {e}")
        counts["failed"] += 1
        return

    update_task(conn, task_id, estimate_min, reasoning)
    print(f"  estimate={estimate_min} min")
    counts["processed"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-source", default="jp")
    ap.add_argument("--model", default=GEMINI_MODEL)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true",
                    help="Re-estimate tasks that already have an estimate.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Download and convert, but don't call Gemini or write.")
    ap.add_argument("--skip", nargs="+", default=[], metavar="TASK_NAME",
                    help="Task name(s) to skip entirely (e.g. --skip FundFun).")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("Error: GEMINI_API_KEY not set.")

    conn = get_db_connection()
    s3 = boto3.client("s3")
    client = None if args.dry_run else genai.Client(api_key=api_key)
    skip_names = set(args.skip)

    counts = {"processed": 0, "skipped": 0, "failed": 0, "would_process": 0}
    try:
        preflight(s3, conn, args.model)
        tasks = fetch_tasks(conn, args.task_source)
        if args.limit:
            tasks = tasks[:args.limit]
        print(f"{len(tasks)} task(s) for source={args.task_source!r}. "
              f"model={args.model} dry_run={args.dry_run}\n")
        for t in tasks:
            process_task(t, s3, client, args.model, conn,
                         args.dry_run, args.force, skip_names, counts)
    finally:
        conn.close()

    summary = (f"processed={counts['processed']} skipped={counts['skipped']} "
               f"failed={counts['failed']}")
    if args.dry_run:
        summary += f" would_process={counts['would_process']}"
    print(f"\nDone. {summary}")


if __name__ == "__main__":
    main()
