"""
Check which agent models have completed valid attempts for tasks.

Valid attempts must satisfy:
  - agent_failed = false
  - deprecated = false
  - attempt_files is a non-empty JSON array containing exactly one *.xlsx
    entry (additional non-xlsx files in the array are allowed)
  - prompt_version matches DEFAULT_MODELS_PROMPT_VERSION (if the model is listed)

Attempts that fail any of the above are reported as "invalid" with a reason
(e.g. agent_failed, no attempt_files, no xlsx file, expected 1 xlsx file got
N, prompt_v mismatch) so missing tasks can be distinguished from broken ones.

Usage:
    source judge/project_configs.sh

    # No task flag - use DEFAULT_TASK_IDS, or all non-deprecated tasks if empty
    python judge/operation_scripts/check_attempt_completion.py

    # Single task - list unique agents with valid attempts
    python judge/operation_scripts/check_attempt_completion.py --task-id 42

    # Multiple tasks - completion matrix across all agents
    python judge/operation_scripts/check_attempt_completion.py --task-ids 42 43 44

    # All non-deprecated tasks - completion matrix across all agents
    python judge/operation_scripts/check_attempt_completion.py --all-tasks

    # Restrict to specific agent models
    python judge/operation_scripts/check_attempt_completion.py --task-ids 42 43 --models "gpt-4o" "claude-sonnet-4-20250514"

TODO: Add default filtering for specific models' prompt version. For example, only consider GPT-4 attempts with prompt_version = "v2".
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import load_project_configs

# Load configs into env vars
load_project_configs()

# ---------------------------------------------------------------------------
# Optional: hard-code a default model list here. When non-empty, only these
# models are considered unless overridden by --models / --all-models.
# ---------------------------------------------------------------------------
DEFAULT_MODELS: list[str] = [
    # "GPT-5.4 (Extended Pro)",
    # "GPT-5.4 (Agent)",
    # "chatgpt_pro",
    # "chatgpt_agent",
    # "chatgpt_web_pro",
    # "chatgpt_excel_agent",
    # "claude_excel_agent",
    # "claude_web",
    "openpyxl_allenai/olmo-3.1-32b-instruct",
    # "openpyxl_anthropic/claude-opus-4-6",
    # "openpyxl_openai/gpt-5.4",
]

# Optional: per-model prompt_version filter. When a model appears here, only
# attempts whose prompt_version matches the specified value are considered valid.
# Models not listed here are not filtered by prompt_version.
# Example: {"GPT-4": 2} means only GPT-4 attempts with prompt_version=2 are kept.
DEFAULT_MODELS_PROMPT_VERSION: dict[str, int] = {
    "openpyxl_openai/gpt-5.4": 1105,
    "openpyxl_anthropic/claude-opus-4-6": 1105,
    "claude_web": 9,
    "claude_excel_agent": 8,
    "chatgpt_excel_agent": 8,
    "chatgpt_agent": 9,
    # "openpyxl_allenai/olmo-3.1-32b-instruct": 1105,
}

# ---------------------------------------------------------------------------
# Optional: hard-code a default task list here. When non-empty, these task IDs
# are used unless overridden by --task-id / --task-ids / --all-tasks.
# ---------------------------------------------------------------------------
DEFAULT_TASK_IDS: list[int] = [
    # 61,
    # 108,
    # 166,
    # 168,
    # 169,
    # 187,
    # 222,
    # 321,
    # 327,
    # 355,
    # 389,
]


def get_db_connection():
    db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        print("Error: BIZBENCHJUDGE_KEYS_DATABASE_URL not set.")
        sys.exit(1)
    return psycopg2.connect(db_url)


def classify_attempt(row, prompt_versions=None):
    """Return (is_valid, reason). reason is None when the attempt is valid."""
    if row.get("agent_failed"):
        return False, "agent_failed=true"
    if row.get("deprecated"):
        return False, "deprecated=true"

    files = row.get("attempt_files")
    if not isinstance(files, list) or len(files) == 0:
        return False, "no attempt_files"

    xlsx_count = sum(
        1 for f in files if isinstance(f, str) and f.lower().endswith(".xlsx")
    )
    if xlsx_count == 0:
        return False, f"no xlsx file (got {len(files)} non-xlsx)"
    if xlsx_count > 1:
        return False, f"expected 1 xlsx file, got {xlsx_count}"

    if prompt_versions:
        model = row["agent_model_name"]
        if model in prompt_versions:
            want = prompt_versions[model]
            got = row.get("prompt_version")
            if got != want:
                return False, f"prompt_v={got}, expected {want}"

    return True, None


def fetch_attempts(conn, task_ids, models=None, prompt_versions=None):
    """Return (valid_rows, invalid_rows) for the given task ids.

    Invalid rows include a 'reason' field describing why the attempt is invalid
    (agent_failed, deprecated, wrong file count, non-xlsx, prompt_v mismatch).

    Args:
        prompt_versions: dict mapping model name -> required prompt_version.
            Mismatches surface as invalid-with-reason rather than being dropped.
    """
    query = """
        SELECT ta.id AS attempt_id,
               ta.task_id,
               ta.agent_model_name,
               ta.prompt_version,
               ta.agent_model_type,
               ta.time_taken_min,
               ta.cost,
               ta.start_time,
               ta.end_time,
               ta.agent_failed,
               ta.deprecated,
               ta.attempt_files
        FROM task_attempts ta
        WHERE ta.task_id = ANY(%s)
    """
    params: list = [task_ids]

    if models:
        query += " AND ta.agent_model_name = ANY(%s)"
        params.append(models)

    query += " ORDER BY ta.task_id, ta.agent_model_name, ta.id"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    valid: list[dict] = []
    invalid: list[dict] = []
    for row in rows:
        is_valid, reason = classify_attempt(row, prompt_versions)
        if is_valid:
            valid.append(row)
        else:
            row["reason"] = reason
            invalid.append(row)

    return valid, invalid


def fetch_all_agent_models(conn, models=None):
    """Return all unique agent model names (from valid attempts)."""
    query = """
        SELECT DISTINCT agent_model_name
        FROM task_attempts
        WHERE agent_failed = false AND deprecated = false
    """
    params: list = []
    if models:
        query += " AND agent_model_name = ANY(%s)"
        params.append(models)
    query += " ORDER BY agent_model_name"

    with conn.cursor() as cur:
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall()]


def fetch_all_task_ids(conn):
    """Return all non-deprecated task IDs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM tasks WHERE deprecated = false OR deprecated IS NULL ORDER BY id"
        )
        return [row[0] for row in cur.fetchall()]


def fetch_task_names(conn, task_ids):
    """Return a mapping of task_id -> task_name."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, task_name FROM tasks WHERE id = ANY(%s) ORDER BY id",
            [task_ids],
        )
        return {row["id"]: row["task_name"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def format_attempt_detail(row):
    """Format versioning and metadata for a single attempt row."""
    parts = [f"id={row['attempt_id']}"]
    if row.get("prompt_version") is not None:
        parts.append(f"prompt_v={row['prompt_version']}")
    if row.get("agent_model_type"):
        parts.append(f"type={row['agent_model_type']}")
    if row.get("time_taken_min") is not None:
        parts.append(f"time={row['time_taken_min']:.1f}min")
    if row.get("cost") is not None:
        parts.append(f"cost=${row['cost']:.2f}")
    if row.get("start_time"):
        parts.append(f"date={row['start_time'].strftime('%Y-%m-%d')}")
    return ", ".join(parts)


def print_single_task(task_id, task_names, valid_attempts, invalid_attempts):
    """Print details for a single task."""
    name = task_names.get(task_id, "Unknown")
    print(f"\nTask {task_id}: {name}")
    print("-" * 60)

    # Group valid by model
    by_model: dict[str, list[dict]] = {}
    for row in valid_attempts:
        by_model.setdefault(row["agent_model_name"], []).append(row)

    # Group invalid by model
    invalid_by_model: dict[str, list[dict]] = {}
    for row in invalid_attempts:
        invalid_by_model.setdefault(row["agent_model_name"], []).append(row)

    if not by_model and not invalid_by_model:
        print("  No attempts found.")
        return

    if by_model:
        print("Valid attempts:")
        for model, rows in sorted(by_model.items()):
            print(f"  {model}  ({len(rows)} attempt{'s' if len(rows) != 1 else ''})")
            for row in rows:
                print(f"    - {format_attempt_detail(row)}")

    if invalid_by_model:
        print("\nInvalid attempts:")
        for model, rows in sorted(invalid_by_model.items()):
            print(f"  {model}  ({len(rows)} attempt{'s' if len(rows) != 1 else ''})")
            for row in rows:
                print(f"    - {format_attempt_detail(row)}  [{row['reason']}]")

    print(f"\nTotal unique models with valid attempts: {len(by_model)}")


def print_multi_task(task_ids, task_names, valid_attempts, invalid_attempts, all_models):
    """Print a completion matrix for multiple tasks."""
    # Build lookup: (task_id, model) -> [attempt rows]
    lookup: dict[tuple[int, str], list[dict]] = {}
    for row in valid_attempts:
        key = (row["task_id"], row["agent_model_name"])
        lookup.setdefault(key, []).append(row)

    # Build invalid lookup: (task_id, model) -> [reason, ...]
    invalid_lookup: dict[tuple[int, str], list[str]] = {}
    for row in invalid_attempts:
        key = (row["task_id"], row["agent_model_name"])
        invalid_lookup.setdefault(key, []).append(row["reason"])

    # Header
    print(f"\nCompletion matrix ({len(task_ids)} tasks x {len(all_models)} models)")
    print("=" * 80)

    for model in all_models:
        completed = []
        missing = []
        for tid in task_ids:
            if (tid, model) in lookup:
                completed.append(tid)
            else:
                missing.append(tid)

        rate = len(completed) / len(task_ids) * 100
        print(f"\n  {model}  —  {len(completed)}/{len(task_ids)} ({rate:.0f}%)")

        if completed:
            print(f"    Completed tasks:")
            for tid in completed:
                name = task_names.get(tid, "")
                rows = lookup[(tid, model)]
                print(f"      Task {tid} ({name}):")
                for row in rows:
                    print(f"        - {format_attempt_detail(row)}")

        if missing:
            parts = []
            for tid in missing:
                reasons = invalid_lookup.get((tid, model), [])
                if reasons:
                    unique = list(dict.fromkeys(reasons))
                    parts.append(f"{tid} ({'; '.join(unique)})")
                else:
                    parts.append(str(tid))
            print(f"    Missing tasks: [{', '.join(parts)}]")

    # Invalid attempts grouped by reason, per model
    print("\n" + "=" * 80)
    print("Invalid attempts by reason")
    print("=" * 80)

    by_model_reasons: dict[str, dict[str, int]] = {}
    for (_tid, model), reasons in invalid_lookup.items():
        bucket = by_model_reasons.setdefault(model, {})
        for r in reasons:
            bucket[r] = bucket.get(r, 0) + 1

    if not by_model_reasons:
        print("\n  (none)")
    else:
        for model in sorted(by_model_reasons):
            counts = by_model_reasons[model]
            total = sum(counts.values())
            print(f"\n  {model}  ({total} invalid)")
            for reason, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
                print(f"    {n:>4}  {reason}")

    # Per-(model, prompt_version) missing tasks
    print("\n" + "=" * 80)
    print("Missing tasks per model (by prompt_version)")
    print("=" * 80)

    for model in all_models:
        pv_tasks: dict[str, set[int]] = {}
        for tid in task_ids:
            for row in lookup.get((tid, model), []):
                pv = row.get("prompt_version")
                pv_key = str(pv) if pv is not None else "none"
                pv_tasks.setdefault(pv_key, set()).add(tid)

        if not pv_tasks:
            print(f"\n  {model}: no valid attempts (missing all {len(task_ids)} tasks)")
            missing_str = ", ".join(str(t) for t in task_ids)
            print(f"    Missing tasks: [{missing_str}]")
            continue

        print(f"\n  {model}:")
        for pv_key in sorted(pv_tasks):
            completed = pv_tasks[pv_key]
            missing = [t for t in task_ids if t not in completed]
            print(
                f"    prompt_v={pv_key}: missing {len(missing)}/{len(task_ids)} tasks"
            )
            if missing:
                missing_str = ", ".join(str(t) for t in missing)
                print(f"      Missing tasks: [{missing_str}]")

    # Summary: completion rate per model, broken down by prompt_version
    print("\n" + "=" * 80)
    print("Summary: completion rate per model (by prompt_version)")
    print("=" * 80)

    for model in all_models:
        # Group attempts by (task_id, prompt_version)
        pv_tasks: dict[str, set[int]] = {}
        for tid in task_ids:
            for row in lookup.get((tid, model), []):
                pv = row.get("prompt_version")
                pv_key = str(pv) if pv is not None else "none"
                pv_tasks.setdefault(pv_key, set()).add(tid)

        if not pv_tasks:
            print(f"\n  {model}: no valid attempts")
            continue

        print(f"\n  {model}:")
        for pv_key in sorted(pv_tasks):
            count = len(pv_tasks[pv_key])
            rate = count / len(task_ids) * 100
            print(f"    prompt_v={pv_key}: {count}/{len(task_ids)} ({rate:.0f}%)")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def resolve_models(args_models, args_all_models):
    """Determine the model filter list and per-model prompt_version filter."""
    if args_all_models:
        return None, None
    if args_models:
        # Explicit --models: no default prompt_version filtering
        return args_models, None
    if DEFAULT_MODELS:
        pv = DEFAULT_MODELS_PROMPT_VERSION if DEFAULT_MODELS_PROMPT_VERSION else None
        return DEFAULT_MODELS, pv
    return None, None


def main():
    parser = argparse.ArgumentParser(
        description="Check valid attempt completion per agent model."
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--task-id",
        type=int,
        help="Single task ID to inspect.",
    )
    group.add_argument(
        "--task-ids",
        type=int,
        nargs="+",
        help="List of task IDs to check completion across.",
    )
    group.add_argument(
        "--all-tasks",
        action="store_true",
        default=False,
        help="Check completion against all non-deprecated tasks in the database.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Only consider these agent model names.",
    )
    parser.add_argument(
        "--all-models",
        action="store_true",
        default=False,
        help="Ignore DEFAULT_MODELS and consider every model in the database.",
    )
    args = parser.parse_args()

    models, prompt_versions = resolve_models(args.models, args.all_models)

    conn = get_db_connection()

    if args.all_tasks:
        task_ids = fetch_all_task_ids(conn)
        if not task_ids:
            print("No non-deprecated tasks found in the database.")
            conn.close()
            return
        print(f"Found {len(task_ids)} non-deprecated tasks.")
    elif args.task_id is not None:
        task_ids = [args.task_id]
    elif args.task_ids:
        task_ids = args.task_ids
    elif DEFAULT_TASK_IDS:
        task_ids = DEFAULT_TASK_IDS
        print(f"Using DEFAULT_TASK_IDS ({len(task_ids)} tasks).")
    else:
        task_ids = fetch_all_task_ids(conn)
        if not task_ids:
            print("No non-deprecated tasks found in the database.")
            conn.close()
            return
        print(
            f"No tasks specified; defaulting to all {len(task_ids)} "
            "non-deprecated tasks."
        )
    try:
        task_names = fetch_task_names(conn, task_ids)

        # Verify all requested task ids exist
        missing = [t for t in task_ids if t not in task_names]
        if missing:
            print(f"Warning: task IDs not found in database: {missing}")

        valid_attempts, invalid_attempts = fetch_attempts(
            conn, task_ids, models, prompt_versions
        )

        if args.task_id and not args.all_tasks:
            print_single_task(
                args.task_id, task_names, valid_attempts, invalid_attempts
            )
        else:
            all_models = fetch_all_agent_models(conn, models)
            print_multi_task(
                task_ids, task_names, valid_attempts, invalid_attempts, all_models
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
