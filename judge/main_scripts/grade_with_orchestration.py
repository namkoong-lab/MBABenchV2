"""Grade attempts with orchestration: filter by model + prompt_version + task,
then run the judge in parallel.

Selection logic is replicated inline (not imported from check_attempt_completion.py)
so this script is self-contained for routine grading runs.

Parallelism model:
    - N worker threads each call grade_single_attempt().
    - A single writer thread drains a results queue and writes to the DB
      (avoids per-worker psycopg2 connections and write locks).
    - Solution/attempt CSV caches: races are allowed during extraction. When a
      worker finishes grading, it publishes its CSV directory to the cache by
      copying to a temp dir and os.rename()-ing to the final name. If another
      worker already published, the loser drops its temp copy.
    - Per-attempt file logs are disabled when workers > 1 because the project
      logger is a module-level singleton and concurrent handlers would cross
      contaminate log files.

Usage:
    source judge/project_configs.sh

    # Use in-file defaults (DEFAULT_MODELS, DEFAULT_MODELS_PROMPT_VERSION, DEFAULT_TASK_IDS)
    python judge/main_scripts/grade_with_orchestration.py

    # Custom tasks and workers
    python judge/main_scripts/grade_with_orchestration.py --task-ids 42 43 --workers 4

    # Override model filter
    python judge/main_scripts/grade_with_orchestration.py --models "gpt-4o" --all-tasks

    # Dry run: preview what would be graded
    python judge/main_scripts/grade_with_orchestration.py --dry-run
"""

import argparse
import csv
import importlib.util
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2
import psycopg2.extras

# Ensure judge/ is on the path for local imports
_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

from utils.llm_utils import get_client
from utils.logger import add_log_file, logger, remove_log_file
from utils.misc_utils import (
    load_env_var,
    load_project_configs,
    relative_path_from_project_root,
)

# Load project configs into env BEFORE importing sibling modules whose
# module-level constants read those env vars at import time.
load_project_configs()

# Reuse grading helpers from grade_from_db.py without copying them
_spec = importlib.util.spec_from_file_location(
    "_grade_from_db", str(Path(__file__).parent / "grade_from_db.py")
)
_gfd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gfd)
grade_single_attempt = _gfd.grade_single_attempt
write_grading_to_db = _gfd.write_grading_to_db
fetch_attempts_by_ids = _gfd.fetch_attempts_by_ids
get_db_connection = _gfd.get_db_connection
resolve_agentic_mode = _gfd.resolve_agentic_mode
add_agentic_cli_args = _gfd.add_agentic_cli_args


# ---------------------------------------------------------------------------
# Defaults — replicated from check_attempt_completion.py so this script is
# self-contained. Keep these in sync manually if that file changes.
# ---------------------------------------------------------------------------

DEFAULT_MODELS: list[str] = [
    "perturbations",
    # "GPT-5.4 (Agent)",
    # "GPT-5.4 (Extended Pro)",
    # "chatgpt_excel_agent",
    # "claude_excel_agent",
    # "claude_web",
    # "openpyxl_anthropic/claude-opus-4-6",
    # "openpyxl_openai/gpt-5.4",
]

DEFAULT_MODELS_PROMPT_VERSION: dict[str, int] = {
    "openpyxl_openai/gpt-5.4": 1105,
    "openpyxl_anthropic/claude-opus-4-6": 1105,
    "claude_excel_agent": 8,
    "chatgpt_excel_agent": 8,
    "claude_web": 8,
}

DEFAULT_TASK_IDS: list[int] = [
    61,  # long context
    108,  # long context
    166,
    168,
    169,
    187,
    222,
    355,
    # 321,  # long context
    # 327,  # long context
    # 389,  # long context
]


# ---------------------------------------------------------------------------
# Selection (replicated from check_attempt_completion.py, simplified)
# ---------------------------------------------------------------------------


def fetch_valid_attempts(conn, task_ids, models=None, prompt_versions=None):
    """Return valid attempts for the given task_ids.

    Valid = agent_failed=false, deprecated=false, has a non-empty attempt_files
    JSON blob. Optionally restrict to ``models``; optionally keep attempts for
    a model only when ``prompt_version`` matches ``prompt_versions[model]``.
    """
    query = """
        SELECT ta.id AS attempt_id,
               ta.task_id,
               ta.agent_model_name,
               ta.prompt_version,
               ta.agent_model_type
        FROM task_attempts ta
        WHERE ta.agent_failed = false
          AND ta.deprecated = false
          AND ta.attempt_files IS NOT NULL
          AND ta.attempt_files::text NOT IN ('null', '[]', '{}')
          AND ta.task_id = ANY(%s)
    """
    params: list = [task_ids]

    if models:
        query += " AND ta.agent_model_name = ANY(%s)"
        params.append(models)

    query += " ORDER BY ta.task_id, ta.agent_model_name, ta.id"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    if prompt_versions:
        rows = [
            r
            for r in rows
            if r["agent_model_name"] not in prompt_versions
            or r["prompt_version"] == prompt_versions[r["agent_model_name"]]
        ]

    return rows


def dedup_latest_attempt(attempts):
    """Keep only the latest attempt per (task_id, agent_model_name, prompt_version).

    "Latest" = max attempt_id within the bucket, since task_attempts.id is
    auto-increment. Returns a new list sorted by (task_id, attempt_id).
    """
    by_bucket: dict[tuple, dict] = {}
    for a in attempts:
        key = (a["task_id"], a["agent_model_name"], a.get("prompt_version"))
        current = by_bucket.get(key)
        if current is None or a["attempt_id"] > current["attempt_id"]:
            by_bucket[key] = a
    return sorted(by_bucket.values(), key=lambda r: (r["task_id"], r["attempt_id"]))


def fetch_all_task_ids(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM tasks WHERE deprecated = false OR deprecated IS NULL ORDER BY id"
        )
        return [row[0] for row in cur.fetchall()]


def resolve_selection(args):
    """Apply precedence: CLI > defaults. Returns (task_ids, models, prompt_versions)."""
    # Tasks
    if args.all_tasks:
        task_ids = None  # filled in after we open a conn
    elif args.task_ids:
        task_ids = list(args.task_ids)
    elif DEFAULT_TASK_IDS:
        task_ids = list(DEFAULT_TASK_IDS)
    else:
        raise SystemExit(
            "No tasks specified. Provide --task-ids, --all-tasks, or set DEFAULT_TASK_IDS."
        )

    # Models
    if args.all_models:
        models = None
        prompt_versions = None
    elif args.models:
        models = list(args.models)
        prompt_versions = (
            None
            if args.no_prompt_version_filter
            else dict(DEFAULT_MODELS_PROMPT_VERSION)
        )
    else:
        models = list(DEFAULT_MODELS) if DEFAULT_MODELS else None
        prompt_versions = (
            None
            if args.no_prompt_version_filter
            else (dict(DEFAULT_MODELS_PROMPT_VERSION) or None)
        )

    if args.prompt_versions_json:
        prompt_versions = json.loads(args.prompt_versions_json)

    return task_ids, models, prompt_versions


# ---------------------------------------------------------------------------
# Atomic cache publish
# ---------------------------------------------------------------------------


def publish_cache_dir(src_dir, final_dir):
    """Copy ``src_dir`` to ``final_dir`` atomically.

    Copies to a sibling ``<final_dir>.tmp.<uuid>`` first, then os.rename() onto
    the final path. If the final already exists (another worker won the race),
    discards the temp copy. Returns True iff this caller published.
    """
    final_path = Path(final_dir)
    if final_path.exists():
        return False

    tmp_path = final_path.with_name(f"{final_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    shutil.copytree(str(src_dir), str(tmp_path))
    try:
        os.rename(str(tmp_path), str(final_path))
        return True
    except OSError:
        # Another worker got there first — drop our copy.
        shutil.rmtree(str(tmp_path), ignore_errors=True)
        return False


def _fmt_dur(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Dry-run preview
# ---------------------------------------------------------------------------


def print_dry_run(attempts, force_agentic=False, force_no_agentic=False):
    print(f"\n{'=' * 70}")
    print(f"DRY RUN — would grade {len(attempts)} attempts")
    print("=" * 70)
    # Group by (model, prompt_version)
    by_bucket: dict[tuple[str, object], list[dict]] = defaultdict(list)
    for a in attempts:
        by_bucket[(a["agent_model_name"], a.get("prompt_version"))].append(a)
    for (model, pv), rows in sorted(by_bucket.items(), key=lambda x: str(x[0])):
        print(f"\n  {model}  prompt_v={pv}  ({len(rows)} attempts)")
        agentic_tasks = sorted(
            {
                r["task_id"]
                for r in rows
                if resolve_agentic_mode(r["task_id"], force_agentic, force_no_agentic)
            }
        )
        standard_tasks = sorted(
            {
                r["task_id"]
                for r in rows
                if not resolve_agentic_mode(
                    r["task_id"], force_agentic, force_no_agentic
                )
            }
        )
        if agentic_tasks:
            print(f"    tasks (agentic judge): {agentic_tasks}")
        if standard_tasks:
            print(f"    tasks (standard judge): {standard_tasks}")
    print()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class GradeOrchestrator:
    def __init__(
        self,
        workers,
        model,
        rubric_path,
        template_path,
        agentic_template_path,
        scratch_run_dir,
        scratch_base,
        files_base_dir,
        solution_char_limit,
        attempt_char_limit,
        total_char_limit,
        attempt_sheet_name_filter,
        ignore_sheets,
        run_calculation,
        force_agentic,
        force_no_agentic,
        carry_over_context,
        max_tool_rounds,
        nocall,
        noupload,
        no_db_write,
        no_s3_upload,
        on_overflow,
        reasoning_effort,
    ):
        self.workers = workers
        self.model = model
        self.rubric_path = rubric_path
        self.template_path = template_path
        self.agentic_template_path = agentic_template_path
        self.scratch_run_dir = scratch_run_dir
        self.files_base_dir = files_base_dir
        self.solution_char_limit = solution_char_limit
        self.attempt_char_limit = attempt_char_limit
        self.total_char_limit = total_char_limit
        self.attempt_sheet_name_filter = attempt_sheet_name_filter
        self.ignore_sheets = ignore_sheets
        self.run_calculation = run_calculation
        self.force_agentic = force_agentic
        self.force_no_agentic = force_no_agentic
        self.carry_over_context = carry_over_context
        self.max_tool_rounds = max_tool_rounds
        self.nocall = nocall
        self.noupload = noupload
        self.no_db_write = no_db_write
        self.no_s3_upload = no_s3_upload
        self.on_overflow = on_overflow
        self.reasoning_effort = reasoning_effort

        # Provider-routed, cached client (thread-safe per SDK).
        self.client = get_client(model)

        # Persistent CSV cache roots
        self.solution_cache_base = (
            Path(scratch_base) / "grade_cache" / "solution_csv_cache"
        )
        self.attempt_cache_base = (
            Path(scratch_base) / "grade_cache" / "attempt_csv_cache"
        )
        self.solution_cache_base.mkdir(parents=True, exist_ok=True)
        self.attempt_cache_base.mkdir(parents=True, exist_ok=True)

        self._attempt_cache_suffix = (
            "__sheet_filtered" if attempt_sheet_name_filter else ""
        )

        # In-memory lookup of published cache dirs.
        self._solution_csv_cache: dict[int, str] = {}
        self._attempt_csv_cache: dict[int, str] = {}
        self._cache_lock = threading.Lock()

        # Result queue drained by the writer thread
        self._result_queue: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None

        self.results: list[dict] = []
        self._results_lock = threading.Lock()

        # Live progress counters. Updated under _results_lock from worker
        # threads; read by the per-completion progress log line.
        self._completed = 0
        self._succeeded = 0
        self._failed = 0
        self._total = 0
        self._start_time = 0.0

    # ---- cache helpers ----

    def _lookup_solution_cache(self, task_id):
        with self._cache_lock:
            cached = self._solution_csv_cache.get(task_id)
        if cached:
            return cached
        # Cold path: check the filesystem (another run may have populated it)
        disk_dir = self.solution_cache_base / f"task_id={task_id}"
        if disk_dir.exists() and list(disk_dir.glob("*.csv")):
            with self._cache_lock:
                self._solution_csv_cache.setdefault(task_id, str(disk_dir))
                return self._solution_csv_cache[task_id]
        return None

    def _lookup_attempt_cache(self, attempt_id):
        with self._cache_lock:
            cached = self._attempt_csv_cache.get(attempt_id)
        if cached:
            return cached
        disk_dir = (
            self.attempt_cache_base
            / f"attempt_id={attempt_id}{self._attempt_cache_suffix}"
        )
        if disk_dir.exists() and list(disk_dir.glob("*.csv")):
            with self._cache_lock:
                self._attempt_csv_cache.setdefault(attempt_id, str(disk_dir))
                return self._attempt_csv_cache[attempt_id]
        return None

    def _publish_solution_cache(self, task_id, src_dir):
        final = self.solution_cache_base / f"task_id={task_id}"
        if not final.exists():
            try:
                publish_cache_dir(src_dir, final)
            except Exception as e:
                logger.warning(
                    f"  Failed to publish solution cache for task {task_id}: {e}"
                )
        # Record in-memory regardless of who won the race.
        with self._cache_lock:
            self._solution_csv_cache.setdefault(task_id, str(final))

    def _publish_attempt_cache(self, attempt_id, src_dir):
        final = (
            self.attempt_cache_base
            / f"attempt_id={attempt_id}{self._attempt_cache_suffix}"
        )
        if not final.exists():
            try:
                publish_cache_dir(src_dir, final)
            except Exception as e:
                logger.warning(
                    f"  Failed to publish attempt cache for attempt {attempt_id}: {e}"
                )
        with self._cache_lock:
            self._attempt_csv_cache.setdefault(attempt_id, str(final))

    # ---- writer thread ----

    def _writer_loop(self):
        conn = get_db_connection()
        try:
            while True:
                item = self._result_queue.get()
                if item is None:
                    self._result_queue.task_done()
                    break
                attempt, result, agentic = item
                try:
                    if (
                        not self.no_db_write
                        and result.get("success")
                        and not result.get("skipped")
                    ):
                        try:
                            grading_id = write_grading_to_db(
                                conn, attempt, result, self.model, agentic=agentic
                            )
                        except (
                            psycopg2.OperationalError,
                            psycopg2.InterfaceError,
                        ) as e:
                            logger.warning(
                                f"  DB connection lost ({e.__class__.__name__}: {e}); "
                                f"reconnecting and retrying write..."
                            )
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = get_db_connection()
                            grading_id = write_grading_to_db(
                                conn, attempt, result, self.model, agentic=agentic
                            )
                        result["grading_id"] = grading_id
                        logger.info(
                            f"  [attempt {attempt['attempt_id']}] wrote grading id={grading_id}"
                        )
                    elif self.no_db_write:
                        logger.info(
                            f"  [attempt {attempt['attempt_id']}] --no-db-write: skipping DB"
                        )
                except Exception as e:
                    logger.error(
                        f"  [attempt {attempt['attempt_id']}] DB write failed: {e}"
                    )
                    result["db_write_error"] = str(e)
                finally:
                    self._result_queue.task_done()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- per-attempt work ----

    def _grade_one(self, attempt):
        attempt_id = attempt["attempt_id"]
        task_id = attempt["task_id"]
        agentic = resolve_agentic_mode(
            task_id, self.force_agentic, self.force_no_agentic
        )

        # Rename the worker thread so every record it emits — including from
        # deeper callees that use the shared module logger — carries the
        # attempt id via %(threadName)s.
        threading.current_thread().name = f"grader-{attempt_id}"

        cached_solution = self._lookup_solution_cache(task_id)
        cached_attempt = self._lookup_attempt_cache(attempt_id)

        cache_notes = []
        if cached_solution:
            cache_notes.append(f"cached solution CSVs for task {task_id}")
        if cached_attempt:
            cache_notes.append(f"cached attempt CSVs for attempt {attempt_id}")
        note = f" (using {'; '.join(cache_notes)})" if cache_notes else ""
        mode = "agentic" if agentic else "standard"
        logger.info(f"[start] attempt {attempt_id} task {task_id} mode={mode}{note}")

        try:
            result = grade_single_attempt(
                attempt=attempt,
                client=self.client,
                rubric_path=self.rubric_path,
                template_path=self.template_path,
                agentic_template_path=self.agentic_template_path,
                model=self.model,
                scratch_run_dir=self.scratch_run_dir,
                files_base_dir=self.files_base_dir,
                nocall=self.nocall,
                noupload=self.noupload,
                run_calculation=self.run_calculation,
                solution_char_limit=self.solution_char_limit,
                attempt_char_limit=self.attempt_char_limit,
                total_char_limit=self.total_char_limit,
                cached_solution_csv_dir=cached_solution,
                cached_attempt_csv_dir=cached_attempt,
                attempt_sheet_name_filter=self.attempt_sheet_name_filter,
                ignore_sheets=self.ignore_sheets,
                agentic=agentic,
                carry_over_context=self.carry_over_context,
                max_tool_rounds=self.max_tool_rounds,
                no_s3_upload=self.no_s3_upload,
                on_overflow=self.on_overflow,
                reasoning_effort=self.reasoning_effort,
            )
        except Exception as e:
            logger.error(f"  [attempt {attempt_id}] grade_single_attempt raised: {e}")
            traceback.print_exc()
            result = {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "success": False,
                "error": f"uncaught: {e}",
                "traceback": traceback.format_exc(),
            }

        # Publish caches atomically on success.
        if result.get("success") and not result.get("skipped"):
            sol_src = result.get("solution_csv_dir")
            if sol_src and not cached_solution:
                self._publish_solution_cache(task_id, sol_src)
            att_src = result.get("attempt_csv_dir")
            if att_src and not cached_attempt:
                self._publish_attempt_cache(attempt_id, att_src)

        # If judge_case auto-routed to the agentic judge due to context
        # overflow, the run that actually produced these scores was agentic.
        # Reflect that in the DB row so agentic_mode + version columns match
        # what _metadata.json records.
        if result.get("auto_routed"):
            agentic = True

        # Hand off to writer thread.
        self._result_queue.put((attempt, result, agentic))

        with self._results_lock:
            self.results.append(result)
            self._completed += 1
            if result.get("success"):
                self._succeeded += 1
            else:
                self._failed += 1
            completed = self._completed
            succeeded = self._succeeded
            failed = self._failed
            total = self._total
            elapsed = time.monotonic() - self._start_time

        status = "OK" if result.get("success") else "FAILED"
        # Warm-up gate: skip ETA until we've cleared at least one full pool
        # cycle so the estimate isn't dominated by cold-cache outliers.
        if completed >= max(3, self.workers) and elapsed > 0:
            eta_sec = (total - completed) * (elapsed / completed)
            timing = f"elapsed={_fmt_dur(elapsed)} eta={_fmt_dur(eta_sec)}"
        else:
            timing = f"elapsed={_fmt_dur(elapsed)} eta=…"
        logger.info(
            f"[progress {completed}/{total}] OK={succeeded} FAIL={failed} — "
            f"{timing} — attempt {attempt_id} task {task_id} {status}"
        )

        return result

    # ---- run ----

    def run(self, attempts):
        self._total = len(attempts)
        self._start_time = time.monotonic()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="grading-writer", daemon=False
        )
        self._writer_thread.start()

        try:
            with ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="grader"
            ) as ex:
                futures = {ex.submit(self._grade_one, a): a for a in attempts}
                for fut in as_completed(futures):
                    # _grade_one catches its own exceptions; this is a last-resort guard.
                    try:
                        fut.result()
                    except Exception as e:
                        a = futures[fut]
                        logger.error(
                            f"  [attempt {a['attempt_id']}] worker crashed: {e}"
                        )
        finally:
            self._result_queue.put(None)
            if self._writer_thread is not None:
                self._writer_thread.join()


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def write_run_summary(
    scratch_run_dir, run_id, model, rubric_path, template_path, attempts, results
):
    summary = {
        "run_id": run_id,
        "model": model,
        "rubric_path": rubric_path,
        "template_path": template_path,
        "total_attempts": len(attempts),
        "successful": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "results": results,
    }
    summary_path = Path(scratch_run_dir) / "run_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary, summary_path


def write_scores_matrix(scratch_run_dir, attempts, results):
    """Write a normalized per-attempt scores table as CSV.

    Columns: task_id, attempt_id, agent_name, accuracy_score, format_score,
    formula_score, full_score. One row per result; failed attempts keep
    blank score cells.
    """
    attempt_by_id = {a["attempt_id"]: a for a in attempts}

    rows = []
    for r in results:
        attempt_id = r.get("attempt_id")
        a = attempt_by_id.get(attempt_id)
        if a is None:
            continue
        scores = r.get("scores") or {}
        rows.append(
            {
                "task_id": a["task_id"],
                "attempt_id": attempt_id,
                "agent_name": a.get("agent_model_name", ""),
                "accuracy_score": scores.get("accuracy_grade"),
                "format_score": scores.get("format_grade"),
                "formula_score": scores.get("formula_grade"),
                "full_score": scores.get("final_score"),
            }
        )

    if not rows:
        return None

    rows.sort(key=lambda row: (row["task_id"], row["attempt_id"]))

    cols = [
        "task_id",
        "attempt_id",
        "agent_name",
        "accuracy_score",
        "format_score",
        "formula_score",
        "full_score",
    ]

    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return v

    out_path = Path(scratch_run_dir) / "scores_matrix.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in rows:
            w.writerow([_fmt(row[c]) for c in cols])
    return out_path


# ---------------------------------------------------------------------------
# Log postprocessing
# ---------------------------------------------------------------------------

# Matches the asctime + threadName prefix produced by DEFAULT_LOG_FORMAT in
# utils/logger.py: "%(asctime)s - %(threadName)s - ...".
_LOG_HEADER_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - (\S+) - ")


def split_run_log_by_thread(run_log_path, out_dir):
    """Split a threadName-tagged log into one file per thread.

    Multi-line records (e.g. tracebacks) have no header on continuation lines,
    so we keep the last-seen thread as the active bucket until a new header
    line arrives. Any leading lines without a header go to ``_preamble.log``.

    Returns a dict mapping thread_name -> output file path.
    """
    run_log_path = Path(run_log_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, list[str]] = defaultdict(list)
    current_thread = "_preamble"

    with open(run_log_path, "r") as f:
        for line in f:
            m = _LOG_HEADER_RE.match(line)
            if m:
                current_thread = m.group(1)
            buckets[current_thread].append(line)

    written: dict[str, str] = {}
    for name, lines in buckets.items():
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        path = out_dir / f"{safe}.log"
        with open(path, "w") as f:
            f.writelines(lines)
        written[name] = str(path)

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    load_project_configs()

    JUDGE_MODEL = load_env_var("JUDGE_OPENROUTER_MODEL", required=True)
    DEFAULT_SOLUTION_CHAR_LIMIT = int(
        load_env_var("JUDGE_DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT", required=True)
    )
    DEFAULT_ATTEMPT_CHAR_LIMIT = int(
        load_env_var("JUDGE_DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT", required=True)
    )
    DEFAULT_TOTAL_CHAR_LIMIT = int(
        load_env_var("JUDGE_DEFAULT_TOTAL_CHARACTER_LIMIT", required=True)
    )

    parser = argparse.ArgumentParser(
        description="Grade attempts filtered by model+prompt_version+task, in parallel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Task selection
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument("--task-ids", type=int, nargs="+", default=None)
    task_group.add_argument("--all-tasks", action="store_true", default=False)

    # Model selection
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Override DEFAULT_MODELS.",
    )
    parser.add_argument(
        "--all-models",
        action="store_true",
        default=False,
        help="Ignore model filtering entirely.",
    )
    parser.add_argument(
        "--no-prompt-version-filter",
        action="store_true",
        default=False,
        help="Do not filter by DEFAULT_MODELS_PROMPT_VERSION.",
    )
    parser.add_argument(
        "--prompt-versions-json",
        type=str,
        default=None,
        help="Override prompt-version filter, e.g. '{\"gpt-4o\": 8}'.",
    )

    # Deduplication: keep only the latest attempt per
    # (task_id, agent_model_name, prompt_version) bucket (by max attempt_id).
    parser.add_argument(
        "--no-dedup",
        dest="no_dedup",
        action="store_true",
        default=False,
        help=(
            "Disable deduplication. By default, when a (task, model, "
            "prompt_version) bucket has multiple valid attempts, only the "
            "latest (max attempt_id) is graded."
        ),
    )

    # Parallelism
    parser.add_argument("--workers", type=int, default=4)

    # Judge passthrough
    parser.add_argument("--model", default=JUDGE_MODEL)
    parser.add_argument("--files-base-dir", default=None)
    parser.add_argument(
        "--solution-char-limit", type=int, default=DEFAULT_SOLUTION_CHAR_LIMIT
    )
    parser.add_argument(
        "--attempt-char-limit", type=int, default=DEFAULT_ATTEMPT_CHAR_LIMIT
    )
    parser.add_argument(
        "--total-char-limit", type=int, default=DEFAULT_TOTAL_CHAR_LIMIT
    )
    parser.add_argument("--run-calculation", action="store_true")
    parser.add_argument(
        "--attempt-sheet-name-filter",
        dest="attempt_sheet_name_filter",
        action="store_true",
        default=False,
    )
    ignore_group = parser.add_mutually_exclusive_group()
    ignore_group.add_argument(
        "--ignore-sheets",
        nargs="+",
        default=["cover"],
        help=(
            "Sheet names to drop from both attempt and solution before grading "
            "(case-insensitive). Default: ['cover']."
        ),
    )
    ignore_group.add_argument(
        "--no-ignore-sheets",
        dest="ignore_sheets",
        action="store_const",
        const=[],
        help="Do not ignore any sheets (overrides the default ['cover']).",
    )
    add_agentic_cli_args(parser)
    parser.add_argument(
        "--no-carry-over-context",
        dest="carry_over_context",
        action="store_false",
        default=True,
    )
    parser.add_argument("--max-tool-rounds", type=int, default=20)
    parser.add_argument(
        "--on-overflow",
        choices=["route_to_agentic", "shorten"],
        default="route_to_agentic",
        help=(
            "What the standard judge does when extracted CSVs exceed the char "
            "budget. 'route_to_agentic' (default) hands off to the agentic "
            "judge with the unshortened CSVs as cached input. 'shorten' uses "
            "the legacy lossy CSV-shortening path. Ignored when --agentic is "
            "set or the task is in TASKS_TO_GRADE_WITH_AGENTIC_JUDGE."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="minimal",
        choices=["none", "minimal", "low", "medium", "high"],
        help=(
            "Reasoning/thinking effort passed to the judge model "
            "(default: minimal). Models without thinking support may reject "
            "the kwarg."
        ),
    )

    # Execution modes
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--nocall", action="store_true")
    parser.add_argument("--noupload", action="store_true")
    parser.add_argument("--no-db-write", action="store_true")
    parser.add_argument(
        "--no-s3-upload",
        dest="no_s3_upload",
        action="store_true",
        help="Skip uploading grading artifacts to S3; use local output_dir path.",
    )

    args = parser.parse_args()

    # Resolve paths
    rubric_path = str(
        relative_path_from_project_root(
            load_env_var("JUDGE_RUBRIC", default="./prompts/rubrics/rubric_7.json")
        )
    )
    template_path = str(
        relative_path_from_project_root(
            load_env_var(
                "JUDGE_PROMPT_TEMPLATE",
                default="./prompts/judge_template_6_3.yaml",
            )
        )
    )
    agentic_template_path = str(
        relative_path_from_project_root(
            load_env_var(
                "AGENTIC_JUDGE_PROMPT_TEMPLATE",
                default="./prompts/agentic_judge_template_1.yaml",
            )
        )
    )
    scratch_base = os.environ.get("SCRATCH_PATH") or str(
        relative_path_from_project_root(
            load_env_var("PATHS_SCRATCH_PATH", default="./scratch")
        )
    )

    run_id = time.strftime("%Y%m%d_%H%M%S")
    scratch_run_dir = Path(scratch_base) / "grade_runs" / run_id
    scratch_run_dir.mkdir(parents=True, exist_ok=True)

    run_log_path = str(scratch_run_dir / "run.log")
    add_log_file(run_log_path)

    logger.info(f"Run directory: {scratch_run_dir}")
    logger.info(f"Run log: {run_log_path}")
    logger.info(
        "Args: "
        + json.dumps({k: v for k, v in vars(args).items()}, default=str, indent=2)
    )

    conn = get_db_connection()
    try:
        task_ids, models, prompt_versions = resolve_selection(args)

        if args.all_tasks:
            task_ids = fetch_all_task_ids(conn)
            if not task_ids:
                logger.info("No non-deprecated tasks found.")
                return
            logger.info(f"Using --all-tasks: {len(task_ids)} tasks.")
        else:
            logger.info(f"Tasks: {task_ids}")

        logger.info(f"Models filter: {models}")
        logger.info(f"Prompt-version filter: {prompt_versions}")

        attempts = fetch_valid_attempts(conn, task_ids, models, prompt_versions)
        if not attempts:
            logger.info("No matching valid attempts found. Exiting.")
            return

        logger.info(f"Matched {len(attempts)} attempts.")

        if not args.no_dedup:
            before = len(attempts)
            attempts = dedup_latest_attempt(attempts)
            dropped = before - len(attempts)
            if dropped:
                logger.info(
                    f"Deduped to latest per (task, model, prompt_v): "
                    f"{len(attempts)} attempts ({dropped} older dropped). "
                    f"Use --no-dedup to keep all."
                )
            else:
                logger.info("Dedup: no duplicates found.")
        else:
            logger.info("Dedup disabled (--no-dedup).")
        attempt_ids = [a["attempt_id"] for a in attempts]

        # Re-fetch with full task join for grading.
        hydrated = fetch_attempts_by_ids(conn, attempt_ids)
        if not hydrated:
            logger.info("No hydrated attempt rows. Exiting.")
            return

        # Align ordering with the ids list. fetch_attempts_by_ids orders by id.
        logger.info(f"Hydrated {len(hydrated)} attempts for grading.")

        if args.dry_run:
            print_dry_run(attempts, args.agentic, args.no_agentic)
            logger.info("Dry run complete — no grading performed.")
            return
    finally:
        conn.close()

    if args.workers > 1:
        logger.info(
            f"Running with {args.workers} workers. Per-attempt log files will "
            f"contain interleaved records from all concurrent workers; grep by "
            f"thread name (e.g. 'grader-<attempt_id>') to isolate one attempt."
        )

    orch = GradeOrchestrator(
        workers=args.workers,
        model=args.model,
        rubric_path=rubric_path,
        template_path=template_path,
        agentic_template_path=agentic_template_path,
        scratch_run_dir=scratch_run_dir,
        scratch_base=scratch_base,
        files_base_dir=args.files_base_dir,
        solution_char_limit=args.solution_char_limit,
        attempt_char_limit=args.attempt_char_limit,
        total_char_limit=args.total_char_limit,
        attempt_sheet_name_filter=args.attempt_sheet_name_filter,
        ignore_sheets=args.ignore_sheets,
        run_calculation=args.run_calculation,
        force_agentic=args.agentic,
        force_no_agentic=args.no_agentic,
        carry_over_context=args.carry_over_context,
        max_tool_rounds=args.max_tool_rounds,
        nocall=args.nocall,
        noupload=args.noupload,
        no_db_write=args.no_db_write,
        no_s3_upload=args.no_s3_upload,
        on_overflow=args.on_overflow,
        reasoning_effort=args.reasoning_effort,
    )

    orch.run(hydrated)

    # Write summary + matrix
    summary, summary_path = write_run_summary(
        scratch_run_dir,
        run_id,
        args.model,
        rubric_path,
        template_path,
        hydrated,
        orch.results,
    )
    matrix_path = write_scores_matrix(scratch_run_dir, hydrated, orch.results)

    logger.info(f"\n{'=' * 60}")
    logger.info("GRADING RUN COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Total: {summary['total_attempts']}")
    logger.info(f"  Successful: {summary['successful']}")
    logger.info(f"  Failed: {summary['failed']}")
    total_cost = sum(r.get("cost", 0) or 0 for r in orch.results if r.get("success"))
    logger.info(f"  Total cost: ${total_cost:.6f}")
    logger.info(f"  Summary: {summary_path}")
    if matrix_path:
        logger.info(f"  Scores matrix: {matrix_path}")
    logger.info("=" * 60)

    for r in orch.results:
        status = "OK" if r.get("success") else "FAILED"
        parts = [f"  attempt {r.get('attempt_id')}: [{status}]"]
        if r.get("scores"):
            s = r["scores"]
            parts.append(
                f"A={s.get('accuracy_grade', 0):.2f} "
                f"F={s.get('formula_grade', 0):.2f} "
                f"Fmt={s.get('format_grade', 0):.2f}"
            )
        if r.get("grading_id"):
            parts.append(f"grading_id={r['grading_id']}")
        if r.get("error"):
            parts.append(r["error"])
        logger.info(" | ".join(parts))

    # Close the run.log handler so the file is flushed before we read it back.
    remove_log_file(run_log_path)

    # Postprocess: split run.log by threadName into per-thread files so each
    # worker's trace is isolated. Runs on console only (run.log is closed).
    try:
        split_dir = Path(scratch_run_dir) / "logs"
        written = split_run_log_by_thread(run_log_path, split_dir)
        logger.info(
            f"Split run.log into {len(written)} per-thread files at {split_dir}"
        )
    except Exception as e:
        logger.warning(f"Log postprocess failed: {e}")


if __name__ == "__main__":
    main()
