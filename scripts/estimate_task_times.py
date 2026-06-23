"""Estimate expert-human completion time for each task using Gemini.

This script is READ-ONLY with respect to the database: it never updates or
otherwise alters the `tasks` table. It works on the local task folders produced
by ingest_tasks.py:

    <tasks-dir>/
        <task_name>/
            starting_files/<file>.xlsx
            solution_files/<file>.xlsx

For every task folder this script:
  1. reads the task's starting file(s) from starting_files/,
  2. converts each worksheet to CSV text (Gemini does not accept .xlsx),
  3. asks Gemini for an estimate, in minutes, of how long an expert modeler
     would take to complete the task from that starting file (the solution file
     is never shown), and
  4. writes the estimate and the model's reasoning to ai_judgement.json inside
     the task folder.

Tasks that already have an ai_judgement.json are skipped unless --force is
given.

Configuration (model, persona, rate-limit settings) is read from the two-tiered
config system under config/ — see config/config_default.yaml. The Gemini API
key comes from the environment (GEMINI_API_KEY).

Run with --dry-run first to convert without calling Gemini or writing anything.
Reads from <repo root>/scratch/tasks by default; override with --tasks-dir.

Usage:
    python estimate_task_times.py --dry-run --limit 1
    python estimate_task_times.py --limit 1
    python estimate_task_times.py
    python estimate_task_times.py --force
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
from google import genai
from google.genai import types

from config import Config

# Repo root is one level up from this scripts/ directory.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS_DIR = ROOT / "scratch" / "tasks"

JUDGEMENT_FILE = "ai_judgement.json"


def collect_files(subdir: Path) -> list[Path]:
    """Return the regular files inside a subfolder, ignoring hidden and temp files.

    Skips:
      - files starting with '.'  (hidden files, e.g. .DS_Store)
      - files starting with '~$' (Excel temp files created when a file is open)
    """
    if not subdir.is_dir():
        return []
    return sorted(
        p
        for p in subdir.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.startswith("~$")
    )


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


def starting_file_text(starting_files: list[Path]) -> str:
    """Convert each local starting file to CSV text and combine the blocks."""
    return "\n\n".join(xlsx_to_csv_text(p) for p in starting_files)


def estimate_one(client, cfg, task_name: str, csv_text: str) -> tuple[float, str]:
    model = cfg.require("gemini.model")
    persona = cfg.require("gemini.persona")
    user_text = (
        "You are given the starting file for a financial modeling task, converted "
        "to CSV (one block per worksheet). It contains the instructions, the given "
        "inputs, and the blank structure the modeler must complete. Estimate how "
        "many minutes an expert modeler would take to complete the task.\n\n"
        f"Task name: {task_name}\n\n"
        f"Starting file contents:\n{csv_text}\n\n"
        "Respond with ONLY a JSON object of the form: "
        '{"estimate_minutes": <number>, "reasoning": "<one short paragraph>"}'
    )
    resp = client.models.generate_content(
        model=model,
        contents=user_text,
        config=types.GenerateContentConfig(
            system_instruction=persona,
            response_mime_type="application/json",
        ),
    )
    data = json.loads(resp.text)
    return float(data["estimate_minutes"]), str(data.get("reasoning", ""))


def estimate_with_retry(
    client, cfg, task_name: str, csv_text: str
) -> tuple[float, str]:
    """Call estimate_one, retrying on 429 rate-limit errors.

    When Gemini returns a 429 it includes a suggested retry delay in the error
    message. This function reads that delay and waits before trying again,
    up to gemini.max_retries attempts total.
    """
    max_retries = cfg.get("gemini.max_retries", 3)
    for attempt in range(max_retries):
        try:
            return estimate_one(client, cfg, task_name, csv_text)
        except Exception as e:  # noqa: BLE001
            error_str = str(e)
            is_rate_limit = "429" in error_str
            is_last_attempt = attempt == max_retries - 1
            if is_rate_limit and not is_last_attempt:
                # Extract the suggested wait time from the error message.
                # Gemini returns something like 'retryDelay': '32s'.
                match = re.search(r"retryDelay.*?(\d+)s", error_str)
                wait = int(match.group(1)) + 5 if match else 60
                print(
                    f"  Rate limited. Waiting {wait}s then retrying "
                    f"(attempt {attempt + 2}/{max_retries})..."
                )
                time.sleep(wait)
            else:
                raise


def write_judgement(task_dir: Path, task_name: str, estimate_min: float, reasoning: str):
    """Write the AI judgement to ai_judgement.json inside the task folder."""
    judgement = {
        "task_name": task_name,
        "ai_time_estimate_min": estimate_min,
        "ai_time_estimate_reasoning": reasoning,
    }
    dest = task_dir / JUDGEMENT_FILE
    dest.write_text(json.dumps(judgement, indent=2, ensure_ascii=False))
    return dest


def process_task(task_dir: Path, client, cfg, dry_run, force, skip_names, counts):
    task_name = task_dir.name
    model = cfg.require("gemini.model")
    print(f"[{task_name}]")

    if task_name in skip_names:
        print("  SKIP - in --skip list.")
        counts["skipped"] += 1
        return

    if (task_dir / JUDGEMENT_FILE).exists() and not force:
        print(f"  SKIP - already has {JUDGEMENT_FILE} (use --force to redo).")
        counts["skipped"] += 1
        return

    starting_files = collect_files(task_dir / "starting_files")
    if not starting_files:
        print("  SKIP - no starting files.")
        counts["skipped"] += 1
        return

    try:
        csv_text = starting_file_text(starting_files)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED (convert): {type(e).__name__}: {e}")
        counts["failed"] += 1
        return

    if dry_run:
        print(
            f"  [dry-run] would send ~{len(csv_text)} chars to {model}; "
            f"would write {JUDGEMENT_FILE}"
        )
        counts["would_process"] += 1
        return

    # Sleep before the API call to stay within the per-minute token quota.
    time.sleep(cfg.get("gemini.sleep_between_calls", 7))

    try:
        estimate_min, reasoning = estimate_with_retry(client, cfg, task_name, csv_text)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED (gemini): {type(e).__name__}: {e}")
        counts["failed"] += 1
        return

    dest = write_judgement(task_dir, task_name, estimate_min, reasoning)
    print(f"  estimate={estimate_min} min -> {dest}")
    counts["processed"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tasks-dir",
        type=Path,
        default=DEFAULT_TASKS_DIR,
        help="Local directory of task folders (as produced by ingest_tasks.py; "
        "default: <repo root>/scratch/tasks).",
    )
    ap.add_argument("--model", help="Gemini model to use (default from config).")
    ap.add_argument("--limit", type=int)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-estimate tasks that already have an ai_judgement.json.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Convert, but don't call Gemini or write anything.",
    )
    ap.add_argument(
        "--skip",
        nargs="+",
        default=[],
        metavar="TASK_NAME",
        help="Task name(s) to skip entirely (e.g. --skip FundFun).",
    )
    args = ap.parse_args()

    if not args.tasks_dir.is_dir():
        sys.exit(f"Tasks directory not found: {args.tasks_dir}")

    cfg = Config.load()
    # CLI flags override the configured defaults (in-memory only).
    if args.model is not None:
        cfg.set("gemini.model", args.model)

    model = cfg.require("gemini.model")

    api_key = cfg.get("gemini.api_key")
    if not api_key and not args.dry_run:
        sys.exit("Error: gemini.api_key not set (set GEMINI_API_KEY).")

    task_dirs = sorted(
        p
        for p in args.tasks_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    if not task_dirs:
        sys.exit(f"No task folders found in {args.tasks_dir}")
    if args.limit:
        task_dirs = task_dirs[: args.limit]

    client = None if args.dry_run else genai.Client(api_key=api_key)
    skip_names = set(args.skip)

    counts = {"processed": 0, "skipped": 0, "failed": 0, "would_process": 0}
    print(
        f"{len(task_dirs)} task folder(s) in {args.tasks_dir}. "
        f"model={model} dry_run={args.dry_run}\n"
    )
    for d in task_dirs:
        process_task(d, client, cfg, args.dry_run, args.force, skip_names, counts)

    summary = (
        f"processed={counts['processed']} skipped={counts['skipped']} "
        f"failed={counts['failed']}"
    )
    if args.dry_run:
        summary += f" would_process={counts['would_process']}"
    print(f"\nDone. {summary}")


if __name__ == "__main__":
    main()
