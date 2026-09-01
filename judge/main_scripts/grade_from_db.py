"""Grade task attempts from the database using the judge system.

Fetches attempt records from the DB, downloads the required files
(ai_attempt xlsx from tasks.task_starting_files, solution xlsx + context PDFs
from tasks.task_solution_files), sets up task folders under the scratch path,
runs judge_case() for each attempt, and writes grading results back to the DB.

Usage (--benchmark selects the DB, S3 grading root and rubric pair; DB URL,
AWS creds and API keys come from <MBABenchV2>/config/config.yaml):
    python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 2 3
    python judge/main_scripts/grade_from_db.py --benchmark v2 --agentic --task-ids 4 5
    python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 --dry-run
    python judge/main_scripts/grade_from_db.py --attempt-ids 1 --no-db-write
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

# Ensure judge/ directory is in Python path for local imports
_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

import psycopg2
import psycopg2.extras
from utils import repo_config, rubric_suitability
from utils.answer_check import run_answer_check, summary_block
from utils.excel_utils import find_golden_solution_file
from utils.judge_identity import resolve_judge_identity
from utils.llm_utils import get_client
from utils.logger import add_log_file, logger, remove_log_file
from utils.misc_utils import (
    BENCHMARKS,
    add_benchmark_arg,
    current_benchmark,
    get_db_url,
    load_env_var,
    load_project_configs,
    relative_path_from_project_root,
)

# Import judge_case from sibling module (no __init__.py, so use importlib)
_spec = importlib.util.spec_from_file_location(
    "_judge_module", str(Path(__file__).parent / "judge.py")
)
_judge_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_judge_mod)
judge_case = _judge_mod.judge_case
agentic_judge_case = _judge_mod.agentic_judge_case
single_pass_judge_case = _judge_mod.single_pass_judge_case

### Obtain constans
AGENTIC_JUDGE_MAX_ROUNDS = int(
    load_env_var("AGENTIC_JUDGE_MAX_ROUNDS", default=50),
)

# Task IDs that should use the agentic judge by default. Override per-run with
# --agentic (force all on) or --no-agentic (force all off).
# NOTE: these ids are only meaningful for one database — BizbenchV1 and
# MBABenchV2 reuse the same numeric ranges, so re-populate (or clear) this
# list when switching benchmarks.
TASKS_TO_GRADE_WITH_AGENTIC_JUDGE: set[int] = set(
    [
        # 104,
        # 61,
        # 296,
        # 65,
        # 127,
        # 289,
        # 386,
        # 58,
        # 307,
        # 164,
        # 321,
        # 228,
        # 50,
        # 108,
        # 75,
        # 343,
        # 304,
        # 285,
        # 389,
        # 260,
        # 73,
        # 364,
        # 176,
        # 374,
        # 67,
        # 327,
        # 174,
        # 147,
        # 238,
        # 135,
        # 92,
        # 212,
        # 379,
        # 83,
        # 197,
        # 339,
        # 262,
        # 273,
        # 373,
        # 217,
        # 384,
        # 219, 1,712,711 chars
        # 132, 1,685,414 chars
    ]
)


def resolve_agentic_mode(
    task_id: int, force_agentic: bool, force_no_agentic: bool
) -> bool:
    """Decide whether a single task should run under the agentic judge.

    Precedence: force_no_agentic > force_agentic > per-task constant.
    """
    if force_no_agentic:
        return False
    if force_agentic:
        return True
    return task_id in TASKS_TO_GRADE_WITH_AGENTIC_JUDGE


def add_agentic_cli_args(parser):
    """Attach the --agentic / --no-agentic mutex group to an argparse parser."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--agentic",
        action="store_true",
        help=(
            "Force the agentic judge for ALL attempts in this run, overriding "
            "TASKS_TO_GRADE_WITH_AGENTIC_JUDGE. Without --agentic or "
            "--no-agentic, each attempt is routed per-task from that list."
        ),
    )
    group.add_argument(
        "--no-agentic",
        dest="no_agentic",
        action="store_true",
        help=(
            "Force the standard judge for ALL attempts in this run, overriding "
            "TASKS_TO_GRADE_WITH_AGENTIC_JUDGE."
        ),
    )
    group.add_argument(
        "--single-pass",
        dest="single_pass",
        action="store_true",
        help=(
            "Judge v4 experiment: grade ALL rubric checks in one conversation "
            "instead of 12 per-category loops. Implies agentic mode; uses the "
            "single_pass.* template/versions/round budget from "
            "project_configs.yaml. Rows record judge_version "
            "single_pass.version, so they never mix with 12-category rows."
        ),
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_connection():
    """Create a PostgreSQL connection to the selected benchmark's database."""
    return psycopg2.connect(get_db_url())


def cache_namespace() -> str:
    """Namespace for the persistent grade_cache, derived from the DB name.

    task_id / attempt_id are only unique within one database; BizbenchV1 and
    MBABenchV2 reuse the same numeric ranges, so caches keyed by bare ids
    must never be shared across benchmarks.
    """
    try:
        db_name = urlparse(get_db_url()).path.lstrip("/").rsplit("/", 1)[-1]
    except EnvironmentError:
        db_name = ""
    return re.sub(r"[^A-Za-z0-9._-]", "_", db_name) or "default"


V1_CATEGORY_SET = {"Accuracy", "Formula", "Formatting"}


def validate_benchmark_coherence(rubric_path, force_agentic, force_no_agentic):
    """Fail fast when a run mixes v1 and v2 benchmark settings.

    --benchmark pins the rubric pair, check_order and S3 root together
    (utils.misc_utils.BENCHMARKS) and get_db_url() refuses a URL naming the
    other database, so what is left to check is that the rubric file on disk
    still matches its check_order and that a non-v1 rubric goes through the
    agentic judge. Called before any DB or S3 work. Set
    JUDGE_SKIP_BENCHMARK_GUARD=1 to bypass for one-off cross-benchmark
    experiments (logged loudly).
    """
    with open(rubric_path, "r", encoding="utf-8") as f:
        rubric_categories = set(json.load(f).keys()) - {"CategoryWeights"}

    check_order = [
        c.strip()
        for c in load_env_var(
            "JUDGE_CHECK_ORDER", default="Accuracy,Formula,Formatting"
        ).split(",")
    ]
    if set(check_order) != rubric_categories:
        sys.exit(
            f"Benchmark config error: check_order {sorted(set(check_order))} "
            f"does not match the categories of {rubric_path} "
            f"{sorted(rubric_categories)}. Update BENCHMARKS in "
            f"utils/misc_utils.py to the rubric's category list."
        )

    if os.environ.get("JUDGE_SKIP_BENCHMARK_GUARD") == "1":
        logger.warning(
            "JUDGE_SKIP_BENCHMARK_GUARD=1 — skipping rubric<->DB<->S3 pairing "
            "checks. Gradings may be written against the wrong benchmark."
        )
        return

    benchmark = current_benchmark()
    if BENCHMARKS[benchmark]["agentic_required"] or rubric_categories != V1_CATEGORY_SET:
        why = (
            f"The standard judge is blocked on {Path(rubric_path).name} because "
            f"judge_template_7_0.yaml hardcodes one stage per v1 category. "
            f"TODO: a template whose stages are generated from JUDGE_CHECK_ORDER "
            f"would lift this. Until then, grade {benchmark} with --agentic."
        )
        if force_no_agentic:
            sys.exit(f"Benchmark config error: --no-agentic forces the standard judge. {why}")
        if not force_agentic:
            sys.exit(f"Benchmark config error: {why}")

    logger.info(f"Database: {repo_config.describe_database_target(benchmark)}")


def fetch_attempts_by_ids(conn, attempt_ids):
    """Fetch attempt records with joined task info by attempt IDs."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ta.id AS attempt_id,
                ta.task_id,
                ta.prompt_files,
                ta.attempt_files,
                ta.agent_model_name,
                ta.agent_model_type,
                ta.agent_failed,
                t.task_name,
                t.task_starting_files,
                t.task_solution_files
            FROM task_attempts ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE ta.id = ANY(%s)
              AND ta.deprecated = false
            ORDER BY ta.id
        """,
            (attempt_ids,),
        )
        return cur.fetchall()


def fetch_attempts_by_task_ids(conn, task_ids):
    """Fetch all non-deprecated attempts for given task IDs."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ta.id AS attempt_id,
                ta.task_id,
                ta.prompt_files,
                ta.attempt_files,
                ta.agent_model_name,
                ta.agent_model_type,
                ta.agent_failed,
                t.task_name,
                t.task_starting_files,
                t.task_solution_files
            FROM task_attempts ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE ta.task_id = ANY(%s)
              AND ta.deprecated = false
            ORDER BY ta.task_id, ta.id
        """,
            (task_ids,),
        )
        return cur.fetchall()


_GRADING_COLUMNS = (
    "task_id", "attempt_id", "grader_model",
    "grader_prompts", "grader_response",
    "accuracy_grade", "formula_grade", "format_grade",
    "rubric_version", "rubric_weight_version", "prompt_version",
    "scored_results", "time_elapsed_min", "cost",
    "raw_files_path", "raw_files",
    "errors_encountered",
    "failed", "failed_reason",
    "deprecated", "deprecated_reason",
    "solution_context_reduced", "attempt_context_reduced",
    "context_reduced_details",
    "agentic_mode", "judge_version",
)

_HAS_GRADER_REASONING = None


def _has_grader_reasoning_column(conn):
    """gradings.grader_reasoning exists in MBABenchV2 but may be absent in
    older databases; probe once so the INSERT works against both."""
    global _HAS_GRADER_REASONING
    if _HAS_GRADER_REASONING is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'gradings' AND column_name = 'grader_reasoning'"
            )
            _HAS_GRADER_REASONING = cur.fetchone() is not None
    return _HAS_GRADER_REASONING


def insert_grading(conn, data):
    """Insert a grading record and return the new grading ID."""
    columns = list(_GRADING_COLUMNS)
    if _has_grader_reasoning_column(conn):
        columns.append("grader_reasoning")
    sql = (
        f"INSERT INTO gradings ({', '.join(columns)}) "
        f"VALUES ({', '.join(f'%({c})s' for c in columns)}) "
        f"RETURNING id"
    )
    with conn.cursor() as cur:
        cur.execute(sql, data)
        grading_id = cur.fetchone()[0]
    conn.commit()
    return grading_id


# ---------------------------------------------------------------------------
# File resolution helpers
# ---------------------------------------------------------------------------

_EXCEL_EXTS = frozenset({".xlsx", ".xlsm", ".xls"})


def extract_file_refs(json_field):
    """Extract (name, source) pairs from a DB JSON field.

    Handles common formats stored in task_starting_files / task_solution_files:
        - String:       "/path/to/file.xlsx"
        - List[str]:    ["/path/a.xlsx", "https://…/b.pdf"]
        - List[dict]:   [{"name": "a.xlsx", "path": "…"}, …]
        - Dict:         {"a.xlsx": "/path/or/url"}

    Returns:
        list of (filename, source_str) tuples.
    """
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


def _parse_s3_uri(uri):
    """Parse an s3://bucket/key URI into (bucket, key)."""
    path = uri[5:]  # strip "s3://"
    bucket, _, key = path.partition("/")
    return bucket, key


# Lazily-initialised S3 client (one per process)
_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = repo_config.s3_client()
    return _s3_client


def download_file(source, dest_path, base_dir=None):
    """Download or copy *source* to *dest_path*.

    *source* may be an S3 URI (s3://bucket/key), an HTTP(S) URL, or a
    filesystem path (absolute or relative).  Relative paths are resolved
    against *base_dir* when provided.
    """
    source_str = str(source)

    if source_str.startswith("s3://"):
        bucket, key = _parse_s3_uri(source_str)
        logger.info(f"    S3 download s3://{bucket}/{key} -> {dest_path.name}")
        _get_s3_client().download_file(bucket, key, str(dest_path))
        return

    if source_str.startswith(("http://", "https://")):
        logger.info(f"    Downloading {source_str} -> {dest_path.name}")
        urllib.request.urlretrieve(source_str, str(dest_path))
        return

    # Local file path
    src = Path(source_str)
    if not src.is_absolute() and base_dir:
        src = Path(base_dir) / src
    src = src.resolve()

    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    shutil.copy(str(src), str(dest_path))


def upload_dir_to_s3(local_dir, bucket, key_prefix):
    """Upload every file under *local_dir* to ``s3://bucket/key_prefix/<rel>``.

    Preserves the relative directory structure. Returns a sorted list of
    relative POSIX paths that were uploaded (suitable for gradings.raw_files).
    """
    local_dir = Path(local_dir)
    s3 = _get_s3_client()
    uploaded: list[str] = []
    for p in local_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(local_dir).as_posix()
        key = f"{key_prefix}/{rel}"
        s3.upload_file(str(p), bucket, key)
        uploaded.append(rel)
    uploaded.sort()
    return uploaded


def setup_task_folder(attempt, scratch_run_dir, files_base_dir=None):
    """Create a task folder under scratch with the layout judge_case expects.

    Files are sourced from the DB JSON fields:
        - tasks.task_starting_files  →  xlsx  →  ai_attempt.xlsx
        - tasks.task_solution_files  →  xlsx  →  solution/<name>.xlsx
                                     →  pdf   →  <name>.pdf  (context)
                                     →  txt   →  <name>.txt  (context)

    Args:
        attempt: Dict row with attempt + task data from the DB query.
        scratch_run_dir: Run-specific scratch directory.
        files_base_dir: Optional base directory for resolving relative paths.

    Returns:
        Path to the created task folder, or None on failure.
    """
    attempt_id = attempt["attempt_id"]
    task_id = attempt["task_id"]
    task_name = attempt["task_name"] or f"task_{task_id}"
    agent_model_name = attempt.get("agent_model_name") or "unknown"
    safe_agent_model = re.sub(r"[^A-Za-z0-9._-]", "_", agent_model_name)

    task_folder = (
        scratch_run_dir
        / f"{task_name}__task_{task_id}__attempt_{attempt_id}__agent_model={safe_agent_model}"
    )
    task_folder.mkdir(parents=True, exist_ok=True)

    # --- ai_attempt.xlsx from task_attempts.attempt_files ---
    attempt_refs = extract_file_refs(attempt.get("attempt_files"))
    xlsx_refs = [
        (n, s) for n, s in attempt_refs if Path(n).suffix.lower() in _EXCEL_EXTS
    ]

    if not xlsx_refs:
        logger.error(f"  No xlsx found in attempt_files for attempt {attempt_id}")
        logger.error(f"  attempt_files JSON: {attempt.get('attempt_files')}")
        return None

    name, source = xlsx_refs[0]
    try:
        download_file(source, task_folder / "ai_attempt.xlsx", base_dir=files_base_dir)
        logger.info(f"  ai_attempt.xlsx <- {name}")
    except Exception as e:
        logger.error(f"  Failed to download attempt file '{name}': {e}")
        return None

    # --- Solution xlsx + context PDFs from tasks.task_solution_files ---
    solution_refs = extract_file_refs(attempt.get("task_solution_files"))

    if not solution_refs:
        logger.error(f"  No files found in task_solution_files for task '{task_name}'")
        logger.error(
            f"  task_solution_files JSON: {attempt.get('task_solution_files')}"
        )
        return None

    solution_dir = task_folder / "solution"
    solution_dir.mkdir(exist_ok=True)
    has_solution_xlsx = False

    for name, source in solution_refs:
        ext = Path(name).suffix.lower()
        if ext in _EXCEL_EXTS:
            # Solution xlsx → solution/ subdirectory
            try:
                download_file(source, solution_dir / name, base_dir=files_base_dir)
                logger.info(f"  solution/{name} <- {name}")
                has_solution_xlsx = True
            except Exception as e:
                logger.error(f"  Failed to download solution file '{name}': {e}")
        elif ext in (".pdf", ".txt"):
            # Context files → task folder root (judge.py finds them there)
            try:
                download_file(source, task_folder / name, base_dir=files_base_dir)
                logger.info(f"  {name} (context) <- {name}")
            except Exception as e:
                logger.error(f"  Failed to download context file '{name}': {e}")

    if not has_solution_xlsx:
        logger.error(f"  No solution xlsx downloaded for task '{task_name}'")
        return None

    # --- Starting workbook from tasks.task_starting_files (v2) ---
    # Staged as starting/starting_workbook.xlsx: the subdirectory keeps it
    # invisible to find_golden_solution_file's task-folder scan, and the
    # fixed stem gives a deterministic extraction directory. The judge
    # serves it as read_file source='starting' so guidance rules that turn
    # on inherited-vs-agent-authored content can be checked, not guessed.
    # Best-effort: a task without one grades exactly as before.
    if current_benchmark(required=False) == "v2":
        starting_xlsx_refs = [
            (n, s)
            for n, s in extract_file_refs(attempt.get("task_starting_files"))
            if Path(n).suffix.lower() in _EXCEL_EXTS
        ]
        if starting_xlsx_refs:
            name, source = starting_xlsx_refs[0]
            try:
                (task_folder / "starting").mkdir(parents=True, exist_ok=True)
                download_file(
                    source,
                    task_folder / "starting" / "starting_workbook.xlsx",
                    base_dir=files_base_dir,
                )
                logger.info(f"  starting/starting_workbook.xlsx <- {name}")
            except Exception as e:
                logger.warning(
                    f"  Failed to download starting workbook '{name}': {e} "
                    f"— grading without it"
                )

    # --- Context PDFs from tasks.task_starting_files ---
    # Starting files often include a "Questions.pdf" while the solution side
    # has a "Questions with Answers.pdf". Both are staged here; the merger
    # dedupes the question-only variant when an answer PDF is present.
    starting_refs = extract_file_refs(attempt.get("task_starting_files"))
    for name, source in starting_refs:
        if Path(name).suffix.lower() != ".pdf":
            continue
        dest = task_folder / name
        if dest.exists():
            logger.info(f"  Skipping starting context '{name}' (already staged)")
            continue
        try:
            download_file(source, dest, base_dir=files_base_dir)
            logger.info(f"  {name} (context, from starting) <- {name}")
        except Exception as e:
            logger.error(f"  Failed to download starting context file '{name}': {e}")

    return task_folder


# ---------------------------------------------------------------------------
# Main grading logic
# ---------------------------------------------------------------------------


def grade_single_attempt(
    attempt,
    client,
    rubric_path,
    template_path,
    agentic_template_path,
    model,
    scratch_run_dir,
    files_base_dir=None,
    nocall=False,
    noupload=False,
    run_calculation=False,
    solution_char_limit=None,
    attempt_char_limit=None,
    total_char_limit=None,
    cached_solution_csv_dir=None,
    cached_attempt_csv_dir=None,
    cached_starting_csv_dir=None,
    attempt_sheet_name_filter=False,
    ignore_sheets=None,
    agentic=False,
    single_pass=False,
    carry_over_context=True,
    max_tool_rounds=20,
    no_s3_upload=False,
    on_overflow="route_to_agentic",
    reasoning_effort=None,
    suitability_source_path=None,
):
    """Grade a single attempt. Returns a result dict."""
    attempt_id = attempt["attempt_id"]
    task_name = attempt["task_name"] or f"task_{attempt['task_id']}"

    logger.info(f"\n{'=' * 60}")
    logger.info(
        f"Grading attempt {attempt_id} for task '{task_name}' "
        f"(task_id={attempt['task_id']})"
    )
    logger.info(f"Agent: {attempt['agent_model_name']} ({attempt['agent_model_type']})")
    logger.info("=" * 60)

    start_time = time.time()

    # Step 1: Setup task folder
    logger.info("[Setup] Creating task folder...")
    task_folder = setup_task_folder(attempt, scratch_run_dir, files_base_dir)

    if task_folder is None:
        return {
            "attempt_id": attempt_id,
            "task_id": attempt["task_id"],
            "success": False,
            "error": "Failed to set up task folder — missing files",
        }

    logger.info(f"  Task folder: {task_folder}")

    # Stage the task's rubric suitability annotation (Phase A) where the
    # agentic judge looks for it. A missing annotation is not an error here —
    # the judge enforces the v2 refusal rule itself.
    if suitability_source_path:
        try:
            shutil.copy(
                str(suitability_source_path),
                str(task_folder / rubric_suitability.STAGED_FILENAME),
            )
            logger.info(
                f"  Staged {rubric_suitability.STAGED_FILENAME} from "
                f"{Path(suitability_source_path).name}"
            )
        except OSError as e:
            logger.warning(f"  Failed to stage suitability annotation: {e}")

    # Per-attempt log
    log_path = str(task_folder / "grade_from_db.log")
    add_log_file(log_path)

    # Pull rubric weight path
    rubric_weight_path = str(
        relative_path_from_project_root(
            load_env_var(
                "JUDGE_RUBRIC_WEIGHT",
                default="./prompts/rubrics/rubric_6_weights.json",
            )
        )
    )
    try:
        # Step 2: Run judge
        if single_pass:
            logger.info("[Judge] Running single_pass_judge_case...")
            result = single_pass_judge_case(
                task_folder=str(task_folder),
                client=client,
                rubric_path=rubric_path,
                template_path=agentic_template_path,
                rubric_weight_path=rubric_weight_path,
                model=model,
                nocall=nocall,
                noupload=noupload,
                run_calculation=run_calculation,
                attempt_model=attempt["agent_model_name"],
                cached_solution_csv_dir=cached_solution_csv_dir,
                cached_attempt_csv_dir=cached_attempt_csv_dir,
                cached_starting_csv_dir=cached_starting_csv_dir,
                attempt_sheet_name_filter=attempt_sheet_name_filter,
                ignore_sheets=ignore_sheets,
                max_tool_rounds=max_tool_rounds,
                reasoning_effort=reasoning_effort,
            )
        elif agentic:
            logger.info("[Judge] Running agentic_judge_case...")
            agentic_kwargs = dict(
                task_folder=str(task_folder),
                client=client,
                rubric_path=rubric_path,
                template_path=agentic_template_path,
                rubric_weight_path=rubric_weight_path,
                model=model,
                nocall=nocall,
                noupload=noupload,
                run_calculation=run_calculation,
                attempt_model=attempt["agent_model_name"],
                cached_solution_csv_dir=cached_solution_csv_dir,
                cached_attempt_csv_dir=cached_attempt_csv_dir,
                cached_starting_csv_dir=cached_starting_csv_dir,
                attempt_sheet_name_filter=attempt_sheet_name_filter,
                ignore_sheets=ignore_sheets,
                carry_over_context=carry_over_context,
                max_tool_rounds=max_tool_rounds,
                reasoning_effort=reasoning_effort,
            )
            result = agentic_judge_case(**agentic_kwargs)
        else:
            logger.info("[Judge] Running judge_case...")
            judge_kwargs = dict(
                task_folder=str(task_folder),
                client=client,
                rubric_path=rubric_path,
                template_path=template_path,
                rubric_weight_path=rubric_weight_path,
                model=model,
                nocall=nocall,
                noupload=noupload,
                run_calculation=run_calculation,
                attempt_model=attempt["agent_model_name"],
                cached_solution_csv_dir=cached_solution_csv_dir,
                cached_attempt_csv_dir=cached_attempt_csv_dir,
                attempt_sheet_name_filter=attempt_sheet_name_filter,
                ignore_sheets=ignore_sheets,
                on_overflow=on_overflow,
                agentic_template_path=agentic_template_path,
                carry_over_context=carry_over_context,
                max_tool_rounds=max_tool_rounds,
                reasoning_effort=reasoning_effort,
            )
            if solution_char_limit is not None:
                judge_kwargs["solution_context_char_limit"] = solution_char_limit
            if attempt_char_limit is not None:
                judge_kwargs["attempt_context_char_limit"] = attempt_char_limit
            if total_char_limit is not None:
                judge_kwargs["total_character_limit"] = total_char_limit

            result = judge_case(**judge_kwargs)

        if result is None:
            # nocall / noupload mode — nothing to record
            return {
                "attempt_id": attempt_id,
                "task_id": attempt["task_id"],
                "success": True,
                "skipped": True,
                "task_folder": str(task_folder),
            }

        elapsed = time.time() - start_time

        # Step 3: Parse results
        output_dir = Path(result["output_dir"])

        ai_judgement = {}
        ai_judgement_path = output_dir / "ai_judgement.json"
        if ai_judgement_path.exists():
            with open(ai_judgement_path) as f:
                ai_judgement = json.load(f)

        token_tracking = {}
        token_tracking_path = output_dir / "token_tracking.json"
        if token_tracking_path.exists():
            with open(token_tracking_path) as f:
                token_tracking = json.load(f)

        conversation = []
        conversation_path = output_dir / "conversation_messages.json"
        if conversation_path.exists():
            with open(conversation_path) as f:
                conversation = json.load(f)

        # Phase B — harness answer check (score-neutral). Runs on the two
        # workbooks in the task folder, writes its artifact into output_dir
        # (so the S3 upload below carries it), and its summary rides in
        # scored_results.answer_check. Failures never block grading.
        answer_check_summary = None
        try:
            solution_xlsx = find_golden_solution_file(Path(task_folder))
            ac_result = run_answer_check(
                Path(task_folder) / "ai_attempt.xlsx",
                solution_xlsx,
                output_json_path=output_dir / "answer_check.json",
            )
            answer_check_summary = summary_block(ac_result)
            logger.info(f"  [answer_check] {answer_check_summary}")
        except Exception as e:  # noqa: BLE001 — score-neutral, never blocks
            logger.warning(f"  [answer_check] skipped on error: {e}")
            answer_check_summary = {"status": "error", "error": str(e)}

        # Upload the full output_dir tree (files in subfolders included) to S3
        # under a unique {timestamp}_{uuid} prefix. Done before the DB write so
        # the gradings row always references a finalized location.
        if no_s3_upload:
            raw_files_path = str(output_dir)
            raw_files_list = sorted(
                p.relative_to(output_dir).as_posix()
                for p in output_dir.rglob("*")
                if p.is_file()
            )
        else:
            bucket = load_env_var("S3_RAW_FILES_BUCKET", required=True)
            prefix_root = load_env_var("S3_RAW_FILES_PREFIX", required=True)
            folder_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            key_prefix = f"{prefix_root}/{folder_name}"
            logger.info(f"  Uploading raw files -> s3://{bucket}/{key_prefix}/ ...")
            raw_files_list = upload_dir_to_s3(output_dir, bucket, key_prefix)
            raw_files_path = f"s3://{bucket}/{key_prefix}/"
            logger.info(f"  Uploaded {len(raw_files_list)} files to S3")

        # Extract scores from judge_case result
        scores = {
            "accuracy_grade": result.get("accuracy_score") or 0,
            "formula_grade": result.get("formula_score") or 0,
            "format_grade": result.get("formatting_score") or 0,
            "final_score": result.get("final_score") or 0,
        }
        scored_results = result.get("score_results", {})
        if answer_check_summary is not None and isinstance(scored_results, dict):
            scored_results["answer_check"] = answer_check_summary

        # Warn loudly if expected scores are missing from the judge result.
        # Completeness is defined by the configured weights file (via
        # result["missing_categories"], computed in _finalize_case against
        # the weights' CategoryWeights), so this works identically for the
        # v1 3-category rubric and the v2 12-category rubric_9.
        if result.get("score_results"):
            missing_scores = list(result.get("missing_categories") or [])
            if result.get("final_score") is None:
                missing_scores.append("final_score")
        else:
            missing_scores = ["all categories (no score_results)"]
        if missing_scores:
            logger.warning(
                f"  WARNING: judge returned incomplete scores for attempt "
                f"{attempt_id}! Missing: {missing_scores}. "
                f"This likely means rubric_weight_path was not provided, a "
                f"category was skipped on parse failure, or weights "
                f"calculation failed. Scores will default to 0."
            )

        # Categories whose judge output could not be parsed after all retries.
        # judge.py marks these with parse_failures[cat]["success"] = False.
        parse_failures = result.get("parse_failures") or {}
        hard_parse_failures = [
            cat for cat, info in parse_failures.items() if not info.get("success", True)
        ]
        if hard_parse_failures:
            logger.warning(
                f"  WARNING: judge failed to parse output for categories: "
                f"{hard_parse_failures}. Affected categories contribute 0 to the "
                f"score and this grading will be marked failed in the DB."
            )

        # Silent-scoring hazards detected inside calculate_scores: unscored
        # checks, empty categories, duplicates, and total_mistakes/len mismatches.
        # Any non-empty bucket means numeric scores are not fully trustworthy.
        scoring_warnings = result.get("scoring_warnings") or {}
        has_scoring_warnings = any(scoring_warnings.get(k) for k in scoring_warnings)
        if has_scoring_warnings:
            logger.warning(
                f"  WARNING: scoring warnings for attempt {attempt_id}: "
                f"{ {k: v for k, v in scoring_warnings.items() if v} }. "
                f"This grading will be marked failed in the DB."
            )

        return {
            "attempt_id": attempt_id,
            "task_id": attempt["task_id"],
            "success": True,
            "task_folder": str(task_folder),
            "output_dir": str(output_dir),
            "scores": scores,
            "scored_results": scored_results,
            "ai_judgement": ai_judgement,
            "conversation": conversation,
            "token_tracking": token_tracking,
            "elapsed_seconds": round(elapsed, 2),
            "cost": token_tracking.get("total_cost", 0),
            "solution_context_reduced": result.get("solution_context_reduced", False),
            "attempt_context_reduced": result.get("attempt_context_reduced", False),
            "context_reduced_details": result.get("context_reduced_details"),
            "raw_files_path": raw_files_path,
            "raw_files": raw_files_list,
            "parse_failures": result.get("parse_failures"),
            "hard_parse_failures": hard_parse_failures,
            "missing_scores": missing_scores,
            "scoring_warnings": scoring_warnings,
            "has_scoring_warnings": has_scoring_warnings,
            "solution_csv_dir": result.get("solution_csv_dir"),
            "attempt_csv_dir": result.get("attempt_csv_dir"),
            "starting_csv_dir": result.get("starting_csv_dir"),
            # The versions this grading actually ran under (single-pass rows
            # carry their own); write_grading_to_db prefers these over env.
            "versions": result.get("versions"),
            "auto_routed": bool(result.get("auto_routed")),
            # Effective reasoning effort (identity pin, or a --reasoning-effort
            # override). write_grading_to_db reads this for the
            # gradings.grader_reasoning column; without it every row stored
            # NULL and two efforts of one label were indistinguishable.
            "judge_reasoning": result.get("judge_reasoning"),
            "grader_identity": result.get("grader_identity"),
        }

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"  FAILED: {e}")
        traceback.print_exc()
        return {
            "attempt_id": attempt_id,
            "task_id": attempt["task_id"],
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "task_folder": str(task_folder),
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        remove_log_file(log_path)


def write_grading_to_db(conn, attempt, result, model, agentic=False):
    """Persist a grading result to the gradings table."""
    # Prefer the versions the grading itself reports (threaded through
    # _finalize_case from the mode that ran). Re-reading the env here would
    # stamp every agentic row with the 12-category AGENTIC_JUDGE_* values,
    # mislabeling single-pass rows, which carry their own judge/prompt
    # versions. The env fallback covers legacy result dicts.
    versions = result.get("versions") or {}
    if versions.get("PROMPT_VERSION") and versions.get("JUDGE_VERSION"):
        PROMPT_VERSION = versions["PROMPT_VERSION"]
        JUDGE_VERSION = versions["JUDGE_VERSION"]
    elif agentic:
        PROMPT_VERSION = load_env_var("AGENTIC_JUDGE_PROMPT_VERSION", required=True)
        JUDGE_VERSION = load_env_var("AGENTIC_JUDGE_VERSION", required=True)
    else:
        PROMPT_VERSION = load_env_var("JUDGE_PROMPT_VERSION", required=True)
        JUDGE_VERSION = load_env_var("JUDGE_VERSION", required=True)
    RUBRIC_VERSION = load_env_var("JUDGE_RUBRIC_VERSION", required=True)
    RUBRIC_WEIGHT_VERSION = load_env_var("JUDGE_RUBRIC_WEIGHT_VERSION", required=True)

    # Enforce grade's existence
    if not result.get("scores"):
        raise ValueError("Result missing 'scores' field with grading details")
    for key in ("accuracy_grade", "formula_grade", "format_grade"):
        if key not in result["scores"]:
            raise ValueError(f"Result 'scores' missing expected key: {key}")

    # A grading is "failed" whenever we can't trust the numeric scores:
    #   - the judge raised (result["success"] is False)
    #   - any category's output couldn't be parsed after all retries
    #   - calculate_scores skipped a category and left it as None
    #   - calculate_scores flagged a silent-scoring hazard (unscored/empty/
    #     duplicate/mistake-count-mismatch)
    hard_parse_failures = result.get("hard_parse_failures") or []
    missing_scores = result.get("missing_scores") or []
    scoring_warnings = result.get("scoring_warnings") or {}
    has_scoring_warnings = bool(result.get("has_scoring_warnings"))
    is_failed = (
        not result["success"]
        or bool(hard_parse_failures)
        or bool(missing_scores)
        or has_scoring_warnings
    )
    if not result["success"]:
        failed_reason = result.get("error")
    elif hard_parse_failures:
        failed_reason = f"Parse failed for categories: {', '.join(hard_parse_failures)}"
    elif missing_scores:
        failed_reason = f"Missing scores: {', '.join(missing_scores)}"
    elif has_scoring_warnings:
        warn_keys = [k for k, v in scoring_warnings.items() if v]
        failed_reason = f"Scoring warnings: {', '.join(warn_keys)}"
    else:
        failed_reason = None

    # Combine all error signals into a single structured blob for the
    # errors_encountered column. Keys are only included when non-empty.
    errors_blob = {}
    if result.get("parse_failures"):
        errors_blob["parse_failures"] = result["parse_failures"]
    if has_scoring_warnings:
        errors_blob["scoring_warnings"] = {
            k: v for k, v in scoring_warnings.items() if v
        }

    data = {
        "task_id": attempt["task_id"],
        "attempt_id": attempt["attempt_id"],
        "grader_model": model,
        "grader_prompts": json.dumps(result.get("conversation", [])),
        "grader_response": json.dumps(result.get("ai_judgement", {})),
        "accuracy_grade": result.get("scores", {}).get("accuracy_grade", 0),
        "formula_grade": result.get("scores", {}).get("formula_grade", 0),
        "format_grade": result.get("scores", {}).get("format_grade", 0),
        "rubric_version": RUBRIC_VERSION,
        "rubric_weight_version": RUBRIC_WEIGHT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scored_results": json.dumps(result.get("scored_results", {})),
        "time_elapsed_min": round(result.get("elapsed_seconds", 0) / 60, 4),
        "cost": round(result.get("cost", 0), 6),
        "raw_files_path": result.get("raw_files_path", ""),
        "raw_files": json.dumps(result.get("raw_files", [])),
        "errors_encountered": json.dumps(errors_blob) if errors_blob else None,
        "failed": is_failed,
        "failed_reason": failed_reason,
        "deprecated": False,
        "deprecated_reason": None,
        "solution_context_reduced": result.get("solution_context_reduced", False),
        "attempt_context_reduced": result.get("attempt_context_reduced", False),
        "context_reduced_details": (
            json.dumps(result["context_reduced_details"])
            if result.get("context_reduced_details")
            else None
        ),
        "agentic_mode": agentic,
        "judge_version": JUDGE_VERSION,
        # Effective reasoning effort (identity-pinned unless overridden);
        # dropped by insert_grading when the DB lacks the column.
        "grader_reasoning": result.get("judge_reasoning"),
    }

    return insert_grading(conn, data)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(args):
    """Fetch attempts from DB, grade each, and optionally store results."""
    load_project_configs(benchmark=args.benchmark)

    # Resolve config paths
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
    # --single-pass: swap in the single-pass template and round budget, and
    # force agentic routing (single-pass IS an agentic mode; only the loop
    # shape differs). Versions come from the single_pass.* config keys via
    # single_pass_judge_case itself.
    single_pass = bool(getattr(args, "single_pass", False))
    if single_pass:
        args.agentic = True
        agentic_template_path = str(
            relative_path_from_project_root(
                load_env_var(
                    "SINGLE_PASS_PROMPT_TEMPLATE",
                    default="./prompts/agentic_judge_template_7.yaml",
                )
            )
        )
        # The --max-tool-rounds default is sized for one category; if the
        # user didn't override it, use the single-pass budget.
        if args.max_tool_rounds == AGENTIC_JUDGE_MAX_ROUNDS:
            args.max_tool_rounds = int(
                load_env_var("SINGLE_PASS_MAX_ROUNDS", default=500)
            )

    model = args.model
    # Fail fast on an unregistered grader label (also covers --dry-run).
    identity = resolve_judge_identity(model)

    # Refuse to start if rubric, check_order and judge mode don't all belong
    # to the selected benchmark (v1 vs v2).
    validate_benchmark_coherence(rubric_path, args.agentic, args.no_agentic)

    scratch_base = str(
        relative_path_from_project_root(
            load_env_var("PATHS_SCRATCH_PATH", default="./scratch")
        )
    )

    # Second-granular alone collides: two same-second processes grading the
    # same attempt shared one task folder and destroyed each other's
    # artifacts (canary step 1 — one process's trajectory close deleted the
    # file the other was about to seal). The uuid suffix makes every
    # process's scratch tree its own.
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    scratch_run_dir = Path(scratch_base) / "grade_runs" / run_id
    scratch_run_dir.mkdir(parents=True, exist_ok=True)

    run_log_path = str(scratch_run_dir / "run.log")
    add_log_file(run_log_path)

    logger.info(f"Run directory: {scratch_run_dir}")
    logger.info(f"Run log: {run_log_path}")

    # DB connection
    conn = get_db_connection()
    logger.info("Connected to database")

    try:
        # Fetch attempts
        if args.attempt_ids:
            attempts = fetch_attempts_by_ids(conn, args.attempt_ids)
            logger.info(f"Fetched {len(attempts)} attempts by IDs: {args.attempt_ids}")
        elif args.task_ids:
            attempts = fetch_attempts_by_task_ids(conn, args.task_ids)
            logger.info(
                f"Fetched {len(attempts)} attempts for task IDs: {args.task_ids}"
            )
        else:
            logger.error("Must provide --attempt-ids or --task-ids")
            return

        if not attempts:
            logger.info("No matching attempts found in database")
            return

        # Dry run: preview only
        if args.dry_run:
            logger.info(f"\n{'=' * 60}")
            logger.info("DRY RUN — would grade the following attempts:")
            logger.info("=" * 60)
            for a in attempts:
                mode = (
                    "agentic"
                    if resolve_agentic_mode(a["task_id"], args.agentic, args.no_agentic)
                    else "standard"
                )
                logger.info(
                    f"  Attempt {a['attempt_id']}: task='{a['task_name']}' "
                    f"(id={a['task_id']}), model={a['agent_model_name']}, "
                    f"failed={a['agent_failed']}, mode={mode}"
                )
            logger.info(f"\nTotal: {len(attempts)} attempts")
            return

        client = get_client(identity)

        # Grade each attempt
        # Persistent caches for extracted CSVs — avoids re-extracting across
        # runs. Namespaced by database name because task/attempt ids collide
        # across the v1 and v2 databases.
        cache_root = Path(scratch_base) / "grade_cache" / cache_namespace()
        # "_v2" cache generation (2026-08): extraction now also writes the
        # *_data.csv serving variants, so pre-revision caches (which lack
        # them) must never be reused. Old cache dirs are left untouched.
        solution_cache_base = cache_root / "solution_csv_cache_v2"
        solution_cache_base.mkdir(parents=True, exist_ok=True)
        attempt_cache_base = cache_root / "attempt_csv_cache_v2"
        attempt_cache_base.mkdir(parents=True, exist_ok=True)
        # Starting-workbook CSVs are per task, like solution CSVs. New cache
        # family (2026-09) — existing solution/attempt caches stay valid.
        starting_cache_base = cache_root / "starting_csv_cache_v2"
        starting_cache_base.mkdir(parents=True, exist_ok=True)
        # Phase A: per-task suitability annotations, fetched once per run and
        # staged into each task folder (the judge enforces the v2 rule).
        suitability_cache_dir = cache_root / "rubric_suitability"
        suitability_sources: dict[int, Path | None] = {}
        s3_suitability_client = None
        attempt_filter = args.attempt_sheet_name_filter
        # Namespace attempt cache by filter state — filtered extractions are a
        # strict subset/rename of unfiltered ones, so they must not share a path.
        attempt_cache_suffix = "__sheet_filtered" if attempt_filter else ""
        solution_csv_cache = {}  # task_id -> cached dir path (in-memory index)
        attempt_csv_cache = {}  # attempt_id -> cached dir path (in-memory index)
        starting_csv_cache = {}  # task_id -> cached dir path (in-memory index)
        results = []
        for i, attempt in enumerate(attempts):
            task_id = attempt["task_id"]
            attempt_id = attempt["attempt_id"]
            agentic = resolve_agentic_mode(task_id, args.agentic, args.no_agentic)

            # Check in-memory index first, then persistent cache on disk
            cached_dir = solution_csv_cache.get(task_id)
            if not cached_dir:
                task_cache_dir = solution_cache_base / f"task_id={task_id}"
                if task_cache_dir.exists() and list(task_cache_dir.glob("*.csv")):
                    cached_dir = str(task_cache_dir)
                    solution_csv_cache[task_id] = cached_dir
                    logger.info(
                        f"  Found persistent solution CSV cache for task {task_id}: "
                        f"{task_cache_dir}"
                    )

            cached_attempt_dir = attempt_csv_cache.get(attempt_id)
            if not cached_attempt_dir:
                attempt_cache_dir = (
                    attempt_cache_base
                    / f"attempt_id={attempt_id}{attempt_cache_suffix}"
                )
                if attempt_cache_dir.exists() and list(attempt_cache_dir.glob("*.csv")):
                    cached_attempt_dir = str(attempt_cache_dir)
                    attempt_csv_cache[attempt_id] = cached_attempt_dir
                    logger.info(
                        f"  Found persistent attempt CSV cache for attempt "
                        f"{attempt_id}: {attempt_cache_dir}"
                    )

            cached_starting_dir = starting_csv_cache.get(task_id)
            if not cached_starting_dir:
                start_cache_dir = starting_cache_base / f"task_id={task_id}"
                if start_cache_dir.exists() and list(start_cache_dir.glob("*.csv")):
                    cached_starting_dir = str(start_cache_dir)
                    starting_csv_cache[task_id] = cached_starting_dir
                    logger.info(
                        f"  Found persistent starting CSV cache for task "
                        f"{task_id}: {start_cache_dir}"
                    )

            cache_notes = []
            if cached_dir:
                cache_notes.append(f"cached solution CSVs for task {task_id}")
            if cached_attempt_dir:
                cache_notes.append(f"cached attempt CSVs for attempt {attempt_id}")
            mode_note = f"mode={'agentic judge' if agentic else 'non-agentic judge'}"
            if cache_notes:
                logger.info(
                    f"\n[{i + 1}/{len(attempts)}] "
                    f"Processing attempt {attempt_id} ({mode_note})... "
                    f"(using {'; '.join(cache_notes)})"
                )
            else:
                logger.info(
                    f"\n[{i + 1}/{len(attempts)}] "
                    f"Processing attempt {attempt_id} ({mode_note})..."
                )

            # Warn if sheet name filtering is on for human attempts
            if (
                attempt_filter
                and attempt.get("agent_model_type", "").lower() == "human"
            ):
                logger.warning(
                    f"  WARNING: attempt_sheet_name_filter is enabled but attempt "
                    f"{attempt['attempt_id']} has agent_model_type='human'. "
                    f"Sheet name filtering may not be appropriate for human attempts."
                )
                try:
                    response = (
                        input("  Continue grading this attempt? [y/N]: ")
                        .strip()
                        .lower()
                    )
                except EOFError:
                    response = "n"
                if response != "y":
                    logger.info(f"  Skipping attempt {attempt['attempt_id']}")
                    results.append(
                        {
                            "attempt_id": attempt["attempt_id"],
                            "task_id": attempt["task_id"],
                            "success": False,
                            "error": "Skipped — human attempt with sheet name filter enabled",
                        }
                    )
                    continue

            # Fetch + pin the task's suitability annotation (v2 agentic only;
            # memoized per task). Failures are logged and left to the judge's
            # refusal rule so one bad task doesn't kill the run.
            suitability_src = None
            if agentic and current_benchmark() == "v2":
                if task_id not in suitability_sources:
                    try:
                        if s3_suitability_client is None:
                            s3_suitability_client = _get_s3_client()
                        annotation, s3_key = rubric_suitability.fetch_annotation(
                            s3_suitability_client,
                            load_env_var("S3_RAW_FILES_BUCKET", required=True),
                            BENCHMARKS[current_benchmark()]["s3_root"],
                            task_id,
                            suitability_cache_dir,
                        )
                        annotation["_staging"] = {"s3_key": s3_key}
                        staged_src = (
                            suitability_cache_dir / f"task_id={task_id}__staged.json"
                        )
                        staged_src.write_text(json.dumps(annotation, indent=2))
                        suitability_sources[task_id] = staged_src
                        logger.info(
                            f"  Suitability annotation for task {task_id}: {s3_key}"
                        )
                    except Exception as e:  # noqa: BLE001 — judge enforces
                        suitability_sources[task_id] = None
                        logger.warning(
                            f"  No usable suitability annotation for task "
                            f"{task_id}: {e} — the judge will refuse this "
                            f"grading unless JUDGE_SKIP_SUITABILITY=1"
                        )
                suitability_src = suitability_sources[task_id]

            result = grade_single_attempt(
                attempt=attempt,
                client=client,
                rubric_path=rubric_path,
                template_path=template_path,
                agentic_template_path=agentic_template_path,
                model=model,
                scratch_run_dir=scratch_run_dir,
                files_base_dir=args.files_base_dir,
                nocall=args.nocall,
                noupload=args.noupload,
                run_calculation=args.run_calculation,
                solution_char_limit=args.solution_char_limit,
                attempt_char_limit=args.attempt_char_limit,
                total_char_limit=args.total_char_limit,
                cached_solution_csv_dir=cached_dir,
                cached_attempt_csv_dir=cached_attempt_dir,
                cached_starting_csv_dir=cached_starting_dir,
                attempt_sheet_name_filter=attempt_filter,
                ignore_sheets=args.ignore_sheets,
                agentic=agentic,
                single_pass=single_pass,
                carry_over_context=args.carry_over_context,
                max_tool_rounds=args.max_tool_rounds,
                no_s3_upload=args.no_s3_upload,
                on_overflow=args.on_overflow,
                reasoning_effort=args.reasoning_effort,
                suitability_source_path=suitability_src,
            )
            results.append(result)

            # Persist solution CSVs to the cache directory for this task
            if (
                result["success"]
                and not result.get("skipped")
                and result.get("solution_csv_dir")
                and task_id not in solution_csv_cache
            ):
                task_cache_dir = solution_cache_base / f"task_id={task_id}"
                if not task_cache_dir.exists():
                    try:
                        shutil.copytree(result["solution_csv_dir"], str(task_cache_dir))
                        logger.info(
                            f"  Persisted solution CSVs for task {task_id}: "
                            f"{task_cache_dir}"
                        )
                    except (FileExistsError, shutil.Error) as e:
                        # Concurrent graders on different attempts of the same
                        # task race here; the cache is shared and identical, so
                        # losing the race is harmless.
                        logger.info(
                            f"  Solution CSV cache for task {task_id} written "
                            f"concurrently ({e.__class__.__name__}); using it"
                        )
                solution_csv_cache[task_id] = str(task_cache_dir)

            # Persist starting-workbook CSVs (per task, like solution CSVs)
            if (
                result["success"]
                and not result.get("skipped")
                and result.get("starting_csv_dir")
                and task_id not in starting_csv_cache
            ):
                start_cache_dir = starting_cache_base / f"task_id={task_id}"
                if not start_cache_dir.exists():
                    try:
                        shutil.copytree(
                            result["starting_csv_dir"], str(start_cache_dir)
                        )
                        logger.info(
                            f"  Persisted starting CSVs for task {task_id}: "
                            f"{start_cache_dir}"
                        )
                    except (FileExistsError, shutil.Error) as e:
                        logger.info(
                            f"  Starting CSV cache for task {task_id} written "
                            f"concurrently ({e.__class__.__name__}); using it"
                        )
                starting_csv_cache[task_id] = str(start_cache_dir)

            # Persist attempt CSVs to the cache directory for this attempt
            if (
                result["success"]
                and not result.get("skipped")
                and result.get("attempt_csv_dir")
                and attempt_id not in attempt_csv_cache
            ):
                attempt_cache_dir = (
                    attempt_cache_base
                    / f"attempt_id={attempt_id}{attempt_cache_suffix}"
                )
                if not attempt_cache_dir.exists():
                    try:
                        shutil.copytree(
                            result["attempt_csv_dir"], str(attempt_cache_dir)
                        )
                        logger.info(
                            f"  Persisted attempt CSVs for attempt {attempt_id}: "
                            f"{attempt_cache_dir}"
                        )
                    except (FileExistsError, shutil.Error) as e:
                        logger.info(
                            f"  Attempt CSV cache for {attempt_id} written "
                            f"concurrently ({e.__class__.__name__}); using it"
                        )
                attempt_csv_cache[attempt_id] = str(attempt_cache_dir)

            # If judge_case auto-routed to the agentic judge due to context
            # overflow, the run that actually produced these scores was agentic.
            # Reflect that in the DB row so agentic_mode + version columns match
            # what _metadata.json records.
            if result.get("auto_routed"):
                agentic = True

            # Write to DB. We write whenever we have a usable result with scores,
            # even if the grading is marked failed (e.g. parse failures) — that
            # way the failure is visible in the DB instead of being dropped.
            if (
                not args.no_db_write
                and not result.get("skipped")
                and result.get("scores")
            ):
                try:
                    try:
                        grading_id = write_grading_to_db(
                            conn, attempt, result, model, agentic=agentic
                        )
                    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                        # Long grading runs can leave the DB connection idle for
                        # tens of minutes; managed Postgres / NAT middleboxes may
                        # drop the socket silently. Reconnect once and retry.
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
                            conn, attempt, result, model, agentic=agentic
                        )
                    result["grading_id"] = grading_id
                    logger.info(f"  Wrote grading to DB: id={grading_id}")
                except Exception as e:
                    logger.error(f"  Failed to write grading to DB: {e}")
                    result["db_write_error"] = str(e)
            elif args.no_db_write:
                logger.info("  Skipping DB write (--no-db-write)")

        # Save run summary
        summary = {
            "run_id": run_id,
            "model": model,
            "grader_identity": identity.settings(),
            "rubric_path": rubric_path,
            "template_path": template_path,
            "total_attempts": len(attempts),
            "successful": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"]),
            "results": results,
        }
        summary_path = scratch_run_dir / "run_summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        # Print summary
        logger.info(f"\n{'=' * 60}")
        logger.info("GRADING RUN COMPLETE")
        logger.info("=" * 60)
        logger.info(f"  Total: {len(results)}")
        logger.info(f"  Successful: {summary['successful']}")
        logger.info(f"  Failed: {summary['failed']}")
        total_cost = sum(r.get("cost", 0) for r in results if r["success"])
        logger.info(f"  Total cost: ${total_cost:.6f}")
        logger.info(f"  Run directory: {scratch_run_dir}")
        logger.info(f"  Summary: {summary_path}")
        logger.info("=" * 60)

        for r in results:
            if not r["success"]:
                status = "FAILED"
            elif r.get("hard_parse_failures") or r.get("missing_scores"):
                status = "PARSE_FAILED"
            elif r.get("has_scoring_warnings"):
                status = "SCORING_WARN"
            else:
                status = "OK"
            parts = [f"  attempt {r['attempt_id']}: [{status}]"]
            if r.get("scores"):
                s = r["scores"]
                parts.append(
                    f"A={s['accuracy_grade']:.2f} "
                    f"F={s['formula_grade']:.2f} "
                    f"Fmt={s['format_grade']:.2f}"
                )
            if r.get("hard_parse_failures"):
                parts.append(f"parse_failed={r['hard_parse_failures']}")
            if r.get("missing_scores"):
                parts.append(f"missing_scores={r['missing_scores']}")
            if r.get("has_scoring_warnings"):
                sw = r.get("scoring_warnings") or {}
                counts = {
                    "unscored": sum(
                        len(v) for v in (sw.get("unscored_checks") or {}).values()
                    ),
                    "empty_cats": len(sw.get("empty_category_judgements") or []),
                    "dupes": sum(
                        len(v) for v in (sw.get("duplicate_judgements") or {}).values()
                    ),
                    "mismatches": len(sw.get("mistake_count_mismatches") or []),
                }
                parts.append(
                    "scoring_warn="
                    + " ".join(f"{k}={v}" for k, v in counts.items() if v)
                )
            if r.get("grading_id"):
                parts.append(f"grading_id={r['grading_id']}")
            if r.get("error"):
                parts.append(r["error"])
            logger.info(" | ".join(parts))

    finally:
        conn.close()
        remove_log_file(run_log_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_project_configs()

    JUDGE_MODEL = load_env_var("JUDGE_DEFAULT_GRADER", required=True)
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
        description="Grade task attempts from the database using the judge system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Grade specific v1 attempts
  python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 2 3

  # Grade all v2 attempts for given tasks (v2 requires the agentic judge)
  python judge/main_scripts/grade_from_db.py --benchmark v2 --agentic --task-ids 4 5

  # Preview without grading
  python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 --dry-run

  # Grade without writing to DB
  python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 --no-db-write

  # Test file preparation only (no API calls)
  python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 --nocall

  # Resolve relative DB paths against a local directory
  python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 --files-base-dir /data
""",
    )
    add_benchmark_arg(parser)

    # ID selection (mutually exclusive)
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument(
        "--attempt-ids",
        type=int,
        nargs="+",
        help="One or more attempt IDs to grade",
    )
    id_group.add_argument(
        "--task-ids",
        type=int,
        nargs="+",
        help="One or more task IDs (grades all non-deprecated attempts for each task)",
    )

    # File resolution
    parser.add_argument(
        "--files-base-dir",
        type=str,
        default=None,
        help=(
            "Optional base directory for resolving relative file paths "
            "stored in the DB JSON fields (task_starting_files, "
            "task_solution_files). Not needed when the JSON contains "
            "absolute paths or HTTP URLs."
        ),
    )

    # Judge parameters
    parser.add_argument(
        "--model",
        default=JUDGE_MODEL,
        help=f"Grader label from judge_identities.yaml (default: {JUDGE_MODEL})",
    )
    parser.add_argument(
        "--solution-char-limit",
        type=int,
        default=DEFAULT_SOLUTION_CHAR_LIMIT,
        help=f"Char limit for golden solution (default: {DEFAULT_SOLUTION_CHAR_LIMIT:,})",
    )
    parser.add_argument(
        "--attempt-char-limit",
        type=int,
        default=DEFAULT_ATTEMPT_CHAR_LIMIT,
        help=f"Char limit for AI attempt (default: {DEFAULT_ATTEMPT_CHAR_LIMIT:,})",
    )
    parser.add_argument(
        "--total-char-limit",
        type=int,
        default=DEFAULT_TOTAL_CHAR_LIMIT,
        help=f"Total char limit for solution + attempt (default: {DEFAULT_TOTAL_CHAR_LIMIT:,})",
    )
    parser.add_argument(
        "--run-calculation",
        action="store_true",
        help="Run Excel formula calculations via LibreOffice before extracting CSVs",
    )

    parser.add_argument(
        "--attempt-sheet-name-filter",
        dest="attempt_sheet_name_filter",
        action="store_true",
        default=False,
        help="Enable attempt sheet name filtering (disabled by default). "
        "When enabled, only attempt sheets starting with 'answers_' or 'model_' are kept.",
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

    # Agentic mode
    add_agentic_cli_args(parser)
    parser.add_argument(
        "--no-carry-over-context",
        dest="carry_over_context",
        action="store_false",
        default=True,
        help="(Agentic only) Disable carrying over findings between categories",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=AGENTIC_JUDGE_MAX_ROUNDS,
        help=f"(Agentic only) Max tool-calling rounds per category (default: {AGENTIC_JUDGE_MAX_ROUNDS})",
    )
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
        default=None,
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        help=(
            "Override the reasoning effort pinned by the grader's identity "
            "(default: the identity's effort). Models without thinking "
            "support may reject the kwarg."
        ),
    )

    # Execution modes
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which attempts would be graded without actually running",
    )
    parser.add_argument(
        "--nocall",
        action="store_true",
        help="Skip API calls (test file preparation only)",
    )
    parser.add_argument(
        "--noupload",
        action="store_true",
        help="Skip file preparation (test file discovery only)",
    )
    parser.add_argument(
        "--no-db-write",
        action="store_true",
        help="Run grading but do not write results to the database",
    )
    parser.add_argument(
        "--no-s3-upload",
        dest="no_s3_upload",
        action="store_true",
        help=(
            "Skip uploading grading artifacts to S3. raw_files_path will be "
            "set to the local output_dir instead, and raw_files will list "
            "relative paths under it."
        ),
    )

    args = parser.parse_args()

    logger.info(
        f"Running grade_from_db with parameters: "
        f"{json.dumps(vars(args), indent=2, default=str)}"
    )

    main(args)
