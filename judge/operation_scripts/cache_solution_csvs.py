"""Pre-cache solution CSVs for all valid tasks in the database.

A task is considered "valid" when:
  1. tasks.deprecated IS NOT true  (false or NULL)
  2. tasks.task_solution_files IS NOT NULL and contains at least one
     Excel file (.xlsx / .xls / .xlsm)

For each valid task the script:
  - Downloads the solution Excel file(s)
  - Extracts every sheet to CSV (no sheet filtering)
  - Stores the result under  <scratch>/grade_cache/solution_csv_cache/task_id=<id>/
  - Writes a _summary.txt with per-task total character counts (desc) and
    a list of task IDs whose solution CSVs exceed 2 million characters.

Usage:
    source judge/project_configs.sh
    python judge/operation_scripts/cache_solution_csvs.py
    python judge/operation_scripts/cache_solution_csvs.py --force   # re-extract even if cached
"""

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# Add judge/ to path for local imports
_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

import boto3
import psycopg2
import psycopg2.extras
from utils.excel_utils import process_all_worksheets
from utils.logger import logger
from utils.misc_utils import load_project_configs

_EXCEL_EXTS = frozenset({".xlsx", ".xlsm", ".xls"})
CHAR_LIMIT = 2_000_000


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        raise EnvironmentError(
            "DATABASE_URL not set. Run: source judge/project_configs.sh"
        )
    return psycopg2.connect(db_url)


def fetch_valid_tasks(conn):
    """Fetch all valid (non-deprecated, with solution files) tasks."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_solution_files
            FROM tasks
            WHERE (deprecated IS NOT true)
              AND task_solution_files IS NOT NULL
            ORDER BY id
            """
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# File helpers  (mirrors grade_from_db.py patterns)
# ---------------------------------------------------------------------------


def extract_file_refs(json_field):
    """Extract (name, source) pairs from a DB JSON field."""
    if json_field is None:
        return []

    refs = []
    if isinstance(json_field, str):
        refs.append((Path(json_field).name, json_field))
    elif isinstance(json_field, list):
        for item in json_field:
            if isinstance(item, str):
                refs.append((Path(item).name, item))
            elif isinstance(item, dict):
                name = item.get("name", "")
                source = ""
                for key in ("url", "path", "file_path", "location"):
                    if key in item:
                        source = str(item[key])
                        break
                if not name and source:
                    name = Path(source).name
                if source:
                    refs.append((name, source))
    elif isinstance(json_field, dict):
        for name, source in json_field.items():
            if isinstance(source, str):
                refs.append((name, source))

    return refs


_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def download_file(source, dest_path):
    """Download or copy *source* to *dest_path*."""
    source_str = str(source)

    if source_str.startswith("s3://"):
        path = source_str[5:]
        bucket, _, key = path.partition("/")
        _get_s3_client().download_file(bucket, key, str(dest_path))
        return

    if source_str.startswith(("http://", "https://")):
        urllib.request.urlretrieve(source_str, str(dest_path))
        return

    src = Path(source_str).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")
    shutil.copy(str(src), str(dest_path))


# ---------------------------------------------------------------------------
# CSV character counting
# ---------------------------------------------------------------------------


def count_csv_chars(directory: Path) -> int:
    """Return total character count of all .csv files in a directory (recursive)."""
    total = 0
    for csv_file in directory.rglob("*.csv"):
        total += len(csv_file.read_text(errors="replace"))
    return total


def write_summary(cache_base: Path, char_counts: dict):
    """Write _summary.txt with per-task character counts and over-limit list."""
    # Sort descending by character count
    sorted_tasks = sorted(char_counts.items(), key=lambda x: x[1], reverse=True)

    over_limit = [(tid, count) for tid, count in sorted_tasks if count > CHAR_LIMIT]

    lines = []
    lines.append("Solution CSV Cache Summary")
    lines.append("=" * 60)
    lines.append(f"Total tasks cached: {len(sorted_tasks)}")
    lines.append(f"Character limit threshold: {CHAR_LIMIT:,}")
    lines.append(f"Tasks exceeding limit: {len(over_limit)}")
    lines.append("")
    lines.append("Per-task total CSV characters (descending):")
    lines.append("-" * 60)
    for task_id, count in sorted_tasks:
        marker = "  ** OVER LIMIT **" if count > CHAR_LIMIT else ""
        lines.append(f"  task_id={task_id:>4}  {count:>12,} chars{marker}")
    lines.append("")

    if over_limit:
        lines.append(f"Task IDs with solution CSV characters exceeding {CHAR_LIMIT:,}:")
        lines.append("-" * 60)
        for task_id, count in over_limit:
            lines.append(f"  task_id={task_id:>4}  {count:>12,} chars")
    else:
        lines.append("No tasks exceed the character limit.")

    lines.append("")

    summary_path = cache_base / "_summary.txt"
    summary_path.write_text("\n".join(lines))
    logger.info(f"Summary written to {summary_path}")


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


def parse_summary(cache_base: Path) -> dict:
    """Parse _summary.txt and return {task_id: char_count} dict."""
    summary_path = cache_base / "_summary.txt"
    char_counts = {}
    import re

    for line in summary_path.read_text().splitlines():
        m = re.match(r"\s*task_id=\s*(\d+)\s+([\d,]+)\s+chars", line)
        if m:
            task_id = int(m.group(1))
            count = int(m.group(2).replace(",", ""))
            char_counts[task_id] = count
    return char_counts


def plot_char_distribution(cache_base: Path):
    """Read _summary.txt, plot the distribution of CSV chars per task, and save."""
    char_counts = parse_summary(cache_base)
    if not char_counts:
        logger.warning("No data found in _summary.txt — skipping plot.")
        return
    counts = np.array(sorted(char_counts.values(), reverse=True))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Left: histogram of character counts
    ax = axes[0]
    ax.hist(counts, bins=30, edgecolor="black", alpha=0.7)
    ax.axvline(CHAR_LIMIT, color="red", linestyle="--", label=f"2M char limit")
    ax.set_xlabel("Total CSV characters")
    ax.set_ylabel("Number of tasks")
    ax.set_title("Distribution of solution CSV size per task")
    ax.legend()

    # Right: sorted bar chart (descending)
    ax = axes[1]
    x = np.arange(len(counts))
    ax.bar(x, counts, width=1.0, edgecolor="none", alpha=0.7)
    ax.axhline(CHAR_LIMIT, color="red", linestyle="--", label=f"2M char limit")
    ax.set_xlabel("Task rank (sorted by size)")
    ax.set_ylabel("Total CSV characters")
    ax.set_title("Per-task solution CSV size (descending)")
    ax.legend()

    fig.tight_layout()
    plot_path = cache_base / "_char_distribution.png"
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)
    logger.info(f"Plot saved to {plot_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Pre-cache solution CSVs for all valid tasks in the database."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract CSVs even if a cache already exists for the task.",
    )
    args = parser.parse_args()

    load_project_configs()

    scratch_base = os.environ.get("BIZBENCHJUDGE_PATHS_SCRATCH_PATH")
    if not scratch_base:
        logger.error("BIZBENCHJUDGE_PATHS_SCRATCH_PATH not set.")
        sys.exit(1)

    cache_base = Path(scratch_base) / "grade_cache" / "solution_csv_cache"
    cache_base.mkdir(parents=True, exist_ok=True)

    conn = get_db_connection()
    try:
        tasks = fetch_valid_tasks(conn)
    finally:
        conn.close()

    logger.info(f"Fetched {len(tasks)} non-deprecated tasks with solution files.")

    # Filter to tasks that actually have Excel solution files
    valid_tasks = []
    for task in tasks:
        refs = extract_file_refs(task["task_solution_files"])
        excel_refs = [(n, s) for n, s in refs if Path(n).suffix.lower() in _EXCEL_EXTS]
        if excel_refs:
            valid_tasks.append((task, excel_refs))

    logger.info(f"{len(valid_tasks)} tasks have at least one Excel solution file.")

    char_counts = {}  # task_id -> total chars
    skipped = 0
    errors = []

    for i, (task, excel_refs) in enumerate(valid_tasks):
        task_id = task["id"]
        task_name = task["task_name"] or f"task_{task_id}"
        task_cache_dir = cache_base / f"task_id={task_id}"

        # Check existing cache
        if (
            not args.force
            and task_cache_dir.exists()
            and list(task_cache_dir.glob("*.csv"))
        ):
            logger.info(
                f"[{i + 1}/{len(valid_tasks)}] task_id={task_id} ({task_name}) "
                f"— already cached, skipping"
            )
            char_counts[task_id] = count_csv_chars(task_cache_dir)
            skipped += 1
            continue

        logger.info(
            f"[{i + 1}/{len(valid_tasks)}] task_id={task_id} ({task_name}) "
            f"— extracting {len(excel_refs)} Excel file(s)..."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            try:
                # Download Excel files
                for filename, source in excel_refs:
                    dest = tmpdir_path / filename
                    download_file(source, dest)

                # Clean target directory if forcing re-extraction
                if args.force and task_cache_dir.exists():
                    shutil.rmtree(task_cache_dir)

                task_cache_dir.mkdir(parents=True, exist_ok=True)

                # Extract CSVs from each Excel file
                for filename, _ in excel_refs:
                    excel_path = tmpdir_path / filename
                    # If multiple Excel files, put each in a subdirectory
                    if len(excel_refs) > 1:
                        out_dir = task_cache_dir / excel_path.stem
                        out_dir.mkdir(parents=True, exist_ok=True)
                    else:
                        out_dir = task_cache_dir

                    process_all_worksheets(
                        excel_file_path=str(excel_path),
                        output_dir=out_dir,
                        run_calculation=False,
                        sheet_name_filter=None,  # no filtering
                    )

                char_counts[task_id] = count_csv_chars(task_cache_dir)
                logger.info(
                    f"  Cached {sum(1 for _ in task_cache_dir.rglob('*.csv'))} CSV files "
                    f"({char_counts[task_id]:,} chars)"
                )

            except Exception as e:
                logger.error(f"  ERROR for task_id={task_id}: {e}")
                errors.append((task_id, str(e)))
                # Clean up partial cache
                if task_cache_dir.exists():
                    shutil.rmtree(task_cache_dir)

    # Also count chars for any pre-existing cached tasks not in the DB query
    # (e.g. from prior grading runs)
    for cached_dir in cache_base.iterdir():
        if not cached_dir.is_dir() or cached_dir.name.startswith("_"):
            continue
        if cached_dir.name.startswith("task_id="):
            try:
                tid = int(cached_dir.name.split("=")[1])
            except ValueError:
                continue
            if tid not in char_counts:
                char_counts[tid] = count_csv_chars(cached_dir)

    # Write summary
    write_summary(cache_base, char_counts)

    logger.info(
        f"\nDone. {len(char_counts)} tasks cached, {skipped} skipped, {len(errors)} errors."
    )
    if errors:
        logger.info("Errors:")
        for tid, err in errors:
            logger.info(f"  task_id={tid}: {err}")

    # Plot character distribution
    plot_char_distribution(cache_base)


if __name__ == "__main__":
    main()
