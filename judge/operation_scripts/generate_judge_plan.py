"""Generate a judge-grading plan for a (tasks x agents) grid.

For each agent x task cell:
  1. Find the latest valid task_attempts row matching the agent + prompt_version filter.
  2. Decide whether the standard or agentic judge should grade it, by reading the
     persistent CSV cache and applying the same threshold logic that ``judge_case``
     uses at run time (see ``judge/operation_scripts/check_context.py``).
     If the cache is missing, optionally extract the CSVs first via ``_prepare_case``
     from ``judge/main_scripts/judge.py`` and publish them into the persistent
     cache the same way ``grade_with_orchestration.py`` does on a real run.
  3. Pull the planned judge config (model, judge_version, prompt_version,
     rubric_version) from project_configs.yaml, picking standard- vs agentic-judge
     versions per row.
  4. Mark ``graded: true`` when a non-failed, non-deprecated row already exists in
     ``gradings`` matching ``(attempt_id, grader_model, agentic_mode, judge_version,
     prompt_version, rubric_version)``.

Outputs two files under
``judge/operation_scripts/judge_plan/{plan_id:04d}_{timestamp}/``:
  - ``plan.yaml``    Per-attempt grading plan (consumed by future graders).
  - ``report.yaml``  Selection echo, counts, and gap analysis (which agent x task
                     cells are missing, and why).

Usage:
    source judge/project_configs.sh

    # By default uses DEFAULT_TASK_IDS / DEFAULT_AGENTS at the top of this file.
    python judge/operation_scripts/generate_judge_plan.py

    # Pull tasks from one or more sources, restrict to specific agents.
    python judge/operation_scripts/generate_judge_plan.py \\
        --sources wsp modeloff \\
        --agents claude_web@8 chatgpt_excel_agent@8 'openpyxl_openai/gpt-5.4@1105'

    # Skip cache generation; rows whose cache is missing become judge_mode=unknown.
    python judge/operation_scripts/generate_judge_plan.py --warm-cache never

    # Force one judge mode for every row (skips the cache classification).
    python judge/operation_scripts/generate_judge_plan.py --agentic
    python judge/operation_scripts/generate_judge_plan.py --no-agentic
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml

_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

from utils.excel_utils import calculate_message_size_for_files
from utils.logger import logger, remove_log_file
from utils.misc_utils import (
    load_env_var,
    load_project_configs,
    relative_path_from_project_root,
)

load_project_configs()


def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_main_scripts = _judge_root / "main_scripts"
_orch = _import_module("_orch_module", _main_scripts / "grade_with_orchestration.py")
_gfd = _import_module("_gfd_module", _main_scripts / "grade_from_db.py")
_jdg = _import_module("_judge_module", _main_scripts / "judge.py")
_chk = _import_module(
    "_check_context_module",
    _judge_root / "operation_scripts" / "check_context.py",
)

fetch_valid_attempts = _orch.fetch_valid_attempts
dedup_latest_attempt = _orch.dedup_latest_attempt
publish_cache_dir = _orch.publish_cache_dir
fetch_attempts_by_ids = _gfd.fetch_attempts_by_ids
get_db_connection = _gfd.get_db_connection
setup_task_folder = _gfd.setup_task_folder
add_agentic_cli_args = _gfd.add_agentic_cli_args
extract_file_refs = _gfd.extract_file_refs
_prepare_case = _jdg._prepare_case
_verdict_for = _chk._verdict_for


# ---------------------------------------------------------------------------
# Defaults — keep mirrored with grade_with_orchestration.DEFAULT_* by hand.
# ---------------------------------------------------------------------------

DEFAULT_TASK_IDS: list[int] = [
    61,
    108,
    166,
    168,
    169,
    187,
    222,
    355,
]

DEFAULT_SOURCES: list[str] = []

# Token form: "name" or "name@prompt_version".
DEFAULT_AGENTS: list[str] = [
    "claude_web@9",
    "chatgpt_excel_agent@8",
    "chatgpt_web_pro@9",
    "claude_excel_agent@8",
    "chatgpt_agent@9",
    "openpyxl_anthropic/claude-opus-4-6@1105",
    "openpyxl_x-ai/grok-4.20@1105",
    "openpyxl_moonshotai/kimi-k2.5@1105",
    "openpyxl_openai/gpt-5.4@1105",
    "openpyxl_google/gemini-3.1-pro-preview@1105",
    "openpyxl_qwen/qwen3.6-plus@1105",
    "openpyxl_allenai/olmo-3.1-32b-instruct@1105",
    # "perturbations",
]


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def parse_agent_token(token: str) -> tuple[str, int | None]:
    """Split ``name@version`` into (name, version_int_or_None)."""
    if "@" not in token:
        return token, None
    name, _, ver = token.rpartition("@")
    if not ver:
        return name, None
    try:
        return name, int(ver)
    except ValueError:
        raise SystemExit(
            f"Invalid agent token '{token}': prompt_version after '@' must be an integer."
        )


def resolve_agents(agent_tokens: list[str]) -> tuple[list[str], dict[str, int]]:
    """Return (model_set, prompt_versions_dict) suitable for fetch_valid_attempts."""
    models: list[str] = []
    prompt_versions: dict[str, int] = {}
    for tok in agent_tokens:
        name, ver = parse_agent_token(tok)
        if name not in models:
            models.append(name)
        if ver is not None:
            prompt_versions[name] = ver
    return models, prompt_versions


def fetch_tasks_for_sources(conn, sources: list[str]) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM tasks
            WHERE (deprecated IS NULL OR deprecated = false)
              AND task_source = ANY(%s)
            ORDER BY id
            """,
            (sources,),
        )
        return [row[0] for row in cur.fetchall()]


def fetch_all_task_ids(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM tasks WHERE deprecated IS NULL OR deprecated = false ORDER BY id"
        )
        return [row[0] for row in cur.fetchall()]


def resolve_task_ids(args, conn) -> tuple[list[int], list[str]]:
    """Resolve the task selection: returns (task_ids, sources_used).

    Union of --task-ids and --sources (and --all-tasks), minus --exclude-task-ids.
    """
    if args.all_tasks:
        return fetch_all_task_ids(conn), []

    explicit = list(args.task_ids) if args.task_ids else []
    sources = list(args.sources) if args.sources else []

    if not explicit and not sources:
        explicit = list(DEFAULT_TASK_IDS)
        sources = list(DEFAULT_SOURCES)
        if not explicit and not sources:
            raise SystemExit(
                "No tasks specified. Provide --task-ids, --sources, --all-tasks, "
                "or set DEFAULT_TASK_IDS / DEFAULT_SOURCES at the top of this file."
            )

    via_sources = fetch_tasks_for_sources(conn, sources) if sources else []
    union = sorted(set(explicit) | set(via_sources))

    excluded = set(args.exclude_task_ids or [])
    return [t for t in union if t not in excluded], sources


# ---------------------------------------------------------------------------
# Cache classification
# ---------------------------------------------------------------------------


def cache_paths_for(
    scratch_base: Path, attempt_id: int, task_id: int, sheet_filtered: bool
) -> tuple[Path, Path]:
    """Persistent cache dirs the orchestrator publishes to."""
    suffix = "__sheet_filtered" if sheet_filtered else ""
    sol = scratch_base / "grade_cache" / "solution_csv_cache" / f"task_id={task_id}"
    att = (
        scratch_base
        / "grade_cache"
        / "attempt_csv_cache"
        / f"attempt_id={attempt_id}{suffix}"
    )
    return sol, att


# Char-count sidecars live OUTSIDE the cache dirs so nothing copying or
# globbing the cache (e.g. the agentic judge's read_file tool) ever sees them.
_CHAR_COUNT_INDEX_SUBDIR = Path("grade_cache_meta") / "char_counts"


def _sidecar_path(cache_dir: Path, scratch_base: Path) -> Path:
    """Map a cache dir to its sidecar location in the index folder.

    cache_dir = .../grade_cache/<namespace>/<dirname>
    sidecar   = <scratch>/grade_cache_meta/char_counts/<namespace>__<dirname>.txt
    """
    namespace = cache_dir.parent.name  # solution_csv_cache | attempt_csv_cache
    dirname = cache_dir.name  # task_id=N | attempt_id=M[__sheet_filtered]
    return scratch_base / _CHAR_COUNT_INDEX_SUBDIR / f"{namespace}__{dirname}.txt"


def _read_char_count_sidecar(sidecar: Path) -> int | None:
    if not sidecar.exists():
        return None
    try:
        return int(sidecar.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_char_count_sidecar(sidecar: Path, count: int) -> None:
    """Persist ``count`` so future planner runs can skip the (expensive) full
    read of every CSV. Failures are non-fatal.
    """
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(str(count))
    except OSError as e:
        logger.warning(f"Failed to write char-count sidecar at {sidecar}: {e}")


def _measure_one(cache_dir: Path, scratch_base: Path) -> int | None:
    """Char count for one cache dir, sidecar-cached. Returns None if the dir
    is empty / missing CSVs.
    """
    if not cache_dir.exists() or not any(cache_dir.glob("*.csv")):
        return None
    sidecar = _sidecar_path(cache_dir, scratch_base)
    cached = _read_char_count_sidecar(sidecar)
    if cached is not None:
        return cached
    chars = calculate_message_size_for_files(cache_dir)["total"]
    _write_char_count_sidecar(sidecar, chars)
    return chars


def measure_cache(
    sol_dir: Path, att_dir: Path, scratch_base: Path
) -> tuple[int | None, int | None]:
    return _measure_one(sol_dir, scratch_base), _measure_one(att_dir, scratch_base)


def warm_one_cache(
    attempt: dict,
    scratch_run_dir: Path,
    scratch_base: Path,
    rubric_path: str,
    sheet_filtered: bool,
    ignore_sheets: list[str],
    run_calculation: bool,
) -> tuple[bool, str | None]:
    """Stage files + extract CSVs + publish to persistent cache.

    Returns (ok, error_str_or_None). Idempotent: skips extraction for sides
    that are already present in the persistent cache.
    """
    attempt_id = attempt["attempt_id"]
    task_id = attempt["task_id"]
    sol_final, att_final = cache_paths_for(
        scratch_base, attempt_id, task_id, sheet_filtered
    )

    if sol_final.exists() and any(sol_final.glob("*.csv")):
        cached_solution = str(sol_final)
    else:
        cached_solution = None
    if att_final.exists() and any(att_final.glob("*.csv")):
        cached_attempt = str(att_final)
    else:
        cached_attempt = None

    if cached_solution and cached_attempt:
        return True, None

    prepared = None
    try:
        task_folder = setup_task_folder(attempt, scratch_run_dir)
        if task_folder is None:
            # setup_task_folder swallows downloader exceptions (incl. OSError
            # from fd exhaustion) and returns None. Reword to avoid claiming
            # "missing files" when the real cause was a download/staging error.
            return False, (
                "setup_task_folder returned None — see worker logs for the "
                "underlying error (commonly: download failure, fd exhaustion, "
                "or genuinely missing files)"
            )

        prepared = _prepare_case(
            task_folder=str(task_folder),
            rubric_path=rubric_path,
            rubric_weight_path=None,
            use_existing=True,
            run_calculation=run_calculation,
            cached_solution_csv_dir=cached_solution,
            cached_attempt_csv_dir=cached_attempt,
            attempt_sheet_name_filter=sheet_filtered,
            ignore_sheets=ignore_sheets,
            agentic=False,
        )

        sol_src = prepared.get("golden_solution_dir")
        att_src = prepared.get("ai_attempt_dir")

        if sol_src and not cached_solution:
            try:
                publish_cache_dir(sol_src, sol_final)
            except Exception as e:
                logger.warning(f"  [attempt {attempt_id}] solution publish failed: {e}")
        if att_src and not cached_attempt:
            try:
                publish_cache_dir(att_src, att_final)
            except Exception as e:
                logger.warning(f"  [attempt {attempt_id}] attempt publish failed: {e}")

        return True, None
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"
    finally:
        # _prepare_case adds a per-call log handler at judge.py:1231 but does
        # not remove it itself (callers do). We call _prepare_case directly,
        # so close it here to avoid leaking a file descriptor per attempt.
        if prepared is not None:
            cache_log_path = prepared.get("cache_log_path")
            if cache_log_path:
                try:
                    remove_log_file(cache_log_path)
                except Exception:
                    pass


def warm_caches_parallel(
    attempts: list[dict],
    scratch_run_dir: Path,
    scratch_base: Path,
    rubric_path: str,
    sheet_filtered: bool,
    ignore_sheets: list[str],
    run_calculation: bool,
    workers: int,
) -> dict[int, str]:
    """Warm caches for attempts whose persistent cache is incomplete.

    Returns a dict of attempt_id -> error_string for attempts that failed.
    """
    needs_warm: list[dict] = []
    for a in attempts:
        sol_final, att_final = cache_paths_for(
            scratch_base, a["attempt_id"], a["task_id"], sheet_filtered
        )
        sol_ok = sol_final.exists() and any(sol_final.glob("*.csv"))
        att_ok = att_final.exists() and any(att_final.glob("*.csv"))
        if not (sol_ok and att_ok):
            needs_warm.append(a)

    if not needs_warm:
        logger.info("Cache warm-up: all required caches already present.")
        return {}

    logger.info(
        f"Cache warm-up: extracting {len(needs_warm)} attempt(s) "
        f"with {workers} worker(s)..."
    )

    errors: dict[int, str] = {}
    with ThreadPoolExecutor(
        max_workers=max(1, workers), thread_name_prefix="warm-cache"
    ) as ex:
        fut_to_attempt = {
            ex.submit(
                warm_one_cache,
                a,
                scratch_run_dir,
                scratch_base,
                rubric_path,
                sheet_filtered,
                ignore_sheets,
                run_calculation,
            ): a
            for a in needs_warm
        }
        for fut in as_completed(fut_to_attempt):
            a = fut_to_attempt[fut]
            try:
                ok, err = fut.result()
            except Exception as e:
                ok, err = False, f"uncaught: {e}\n{traceback.format_exc()}"
            if not ok:
                errors[a["attempt_id"]] = err or "unknown"
                logger.error(f"  [attempt {a['attempt_id']}] warm failed: {err}")

    if errors:
        logger.warning(f"Cache warm-up finished with {len(errors)} failures.")
    else:
        logger.info("Cache warm-up complete.")
    return errors


def classify_judge_mode(
    sol_chars: int | None,
    att_chars: int | None,
    solution_limit: int,
    attempt_limit: int,
    total_limit: int,
    force_agentic: bool,
    force_no_agentic: bool,
) -> tuple[str, str]:
    """Return (mode, reason). mode in {agentic, standard, unknown}.

    --force-agentic / --force-no-agentic short-circuit the cache check.
    """
    if force_agentic:
        return "agentic", "forced via --agentic"
    if force_no_agentic:
        return "standard", "forced via --no-agentic"
    verdict, reason = _verdict_for(
        sol_chars, att_chars, solution_limit, attempt_limit, total_limit
    )
    if verdict == "AGENTIC":
        return "agentic", reason
    if verdict == "STANDARD":
        return "standard", reason
    return "unknown", reason


# ---------------------------------------------------------------------------
# Existing-grading lookup
# ---------------------------------------------------------------------------


def fetch_existing_gradings(conn, attempt_ids: list[int]) -> list[dict]:
    """Fetch all non-failed, non-deprecated gradings for the given attempts."""
    if not attempt_ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT attempt_id, grader_model, agentic_mode,
                   judge_version, prompt_version, rubric_version,
                   grader_reasoning
            FROM gradings
            WHERE attempt_id = ANY(%s)
              AND failed = false
              AND deprecated = false
            """,
            (attempt_ids,),
        )
        return [dict(r) for r in cur.fetchall()]


def _norm_reasoning(v) -> str:
    """Treat NULL and the empty string as equivalent so older rows without a
    recorded reasoning effort don't spuriously fail the equality check."""
    if v is None:
        return ""
    return str(v)


def is_already_graded(
    existing: list[dict],
    attempt_id: int,
    judge_model: str,
    agentic: bool,
    judge_version: int,
    prompt_version: str,
    rubric_version: str,
    reasoning_effort: str | None,
) -> bool:
    target_reasoning = _norm_reasoning(reasoning_effort)
    for r in existing:
        if (
            r["attempt_id"] == attempt_id
            and r["grader_model"] == judge_model
            and bool(r["agentic_mode"]) == bool(agentic)
            and int(r["judge_version"]) == int(judge_version)
            and str(r["prompt_version"]) == str(prompt_version)
            and str(r["rubric_version"]) == str(rubric_version)
            and _norm_reasoning(r.get("grader_reasoning")) == target_reasoning
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Gap analysis (missing cells)
# ---------------------------------------------------------------------------

_MISSING_REASON_PRIORITY = [
    "no_attempt",
    "agent_failed",
    "deprecated",
    "empty_attempt_files",
    "no_xlsx_in_attempt_files",
    "prompt_version_mismatch",
]

_EXCEL_EXTS = (".xlsx", ".xlsm", ".xls")


def fetch_missing_diagnostics(
    conn, task_ids: list[int], models: list[str]
) -> dict[tuple[int, str], list[dict]]:
    """Fetch every task_attempts row for the (task, agent) grid, no strict filters.

    Used to classify why a cell is missing from the planned attempts.
    """
    if not task_ids or not models:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id AS attempt_id, task_id, agent_model_name,
                   prompt_version, agent_failed, deprecated, attempt_files
            FROM task_attempts
            WHERE task_id = ANY(%s) AND agent_model_name = ANY(%s)
            ORDER BY task_id, agent_model_name, id
            """,
            (task_ids, models),
        )
        rows = cur.fetchall()

    bucket: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        bucket.setdefault((r["task_id"], r["agent_model_name"]), []).append(dict(r))
    return bucket


def _attempt_files_empty(af) -> bool:
    if af is None:
        return True
    if isinstance(af, (list, dict)):
        return len(af) == 0
    s = str(af).strip()
    return s in ("", "null", "[]", "{}")


def _attempt_files_has_xlsx(af) -> bool:
    """Mirror the xlsx check in setup_task_folder so the planner can drop
    attempts that would fail at staging time."""
    for name, _source in extract_file_refs(af):
        if Path(name).suffix.lower() in _EXCEL_EXTS:
            return True
    return False


def classify_missing(rows: list[dict], required_prompt_version: int | None) -> dict:
    """Determine why no valid attempt was planned for a (task, agent) cell."""
    if not rows:
        return {"reason": "no_attempt"}

    latest = max(rows, key=lambda r: r["attempt_id"])
    seen = {
        "latest_attempt_id_seen": latest["attempt_id"],
        "seen_prompt_version": latest.get("prompt_version"),
    }

    if latest.get("agent_failed"):
        return {"reason": "agent_failed", **seen}
    if latest.get("deprecated"):
        return {"reason": "deprecated", **seen}
    if _attempt_files_empty(latest.get("attempt_files")):
        return {"reason": "empty_attempt_files", **seen}
    if not _attempt_files_has_xlsx(latest.get("attempt_files")):
        return {"reason": "no_xlsx_in_attempt_files", **seen}
    if (
        required_prompt_version is not None
        and latest.get("prompt_version") != required_prompt_version
    ):
        return {"reason": "prompt_version_mismatch", **seen}
    # Fallback: latest row looks valid but somehow didn't make the planned set.
    return {"reason": "no_attempt", **seen}


# ---------------------------------------------------------------------------
# Plan-id allocation
# ---------------------------------------------------------------------------


_PLAN_DIR_RE = re.compile(r"^(\d{4})_")


def next_plan_id(plan_root: Path) -> int:
    if not plan_root.exists():
        return 1
    max_id = 0
    for child in plan_root.iterdir():
        m = _PLAN_DIR_RE.match(child.name)
        if m:
            max_id = max(max_id, int(m.group(1)))
    return max_id + 1


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _ordered_dump(data, stream):
    """yaml.safe_dump that preserves dict insertion order."""
    yaml.safe_dump(
        data,
        stream,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def write_plan_yaml(out_path: Path, generated_at: str, plan_rows: list[dict]):
    grading_plan = []
    for row in plan_rows:
        grading_plan.append(
            {
                row["attempt_id"]: {
                    "judge_model": row["judge_model"],
                    "judge_mode": row["judge_mode"],
                    "judge_version": row["judge_version"],
                    "judge_prompt_version": row["judge_prompt_version"],
                    "rubric_version": row["rubric_version"],
                    "attempt_additional_info": {
                        "task_id": row["task_id"],
                        "agent_model_name": row["agent_model_name"],
                        "prompt_version": row["agent_prompt_version"],
                    },
                    "graded": row["graded"],
                }
            }
        )
    payload = {
        "generated_at": generated_at,
        "attempt_grading_plan": grading_plan,
    }
    with open(out_path, "w") as f:
        _ordered_dump(payload, f)


def collect_boundary_attempts(
    plan_rows: list[dict],
    total_limit: int,
    margin_fraction: float,
) -> list[dict]:
    """Flag attempts whose combined sol+att chars sit within ``margin_fraction``
    of ``total_limit`` on either side. These are the rows where a small change
    in cache size could flip the standard/agentic verdict.
    """
    if not total_limit or total_limit <= 0 or margin_fraction <= 0:
        return []
    band = int(round(total_limit * margin_fraction))
    flagged: list[dict] = []
    for r in plan_rows:
        sol = r.get("cache_solution_chars")
        att = r.get("cache_attempt_chars")
        if sol is None or att is None:
            continue
        combined = sol + att
        delta = combined - total_limit
        if abs(delta) <= band:
            flagged.append(
                {
                    "attempt_id": r["attempt_id"],
                    "task_id": r["task_id"],
                    "agent_model_name": r.get("agent_model_name"),
                    "judge_mode": r["judge_mode"],
                    "combined_chars": combined,
                    "solution_chars": sol,
                    "attempt_chars": att,
                    "total_limit": total_limit,
                    "delta_chars": delta,
                }
            )
    flagged.sort(key=lambda e: abs(e["delta_chars"]))
    return flagged


def write_report_yaml(
    out_path: Path,
    plan_id: int,
    generated_at: str,
    selection: dict,
    judge_config: dict,
    plan_rows: list[dict],
    missing: list[dict],
    warm_errors: dict[int, str],
    boundary: list[dict],
    boundary_margin: float,
):
    by_mode = {"agentic": 0, "standard": 0, "unknown": 0}
    by_graded = {"graded": 0, "to_grade": 0}
    to_grade_ids: list[int] = []
    for r in plan_rows:
        by_mode[r["judge_mode"]] = by_mode.get(r["judge_mode"], 0) + 1
        if r["graded"]:
            by_graded["graded"] += 1
        else:
            by_graded["to_grade"] += 1
            to_grade_ids.append(r["attempt_id"])

    by_reason: dict[str, int] = {}
    for m in missing:
        by_reason[m["reason"]] = by_reason.get(m["reason"], 0) + 1

    expected_cells = len(selection["task_ids"]) * len(selection["agents"])

    payload = {
        "plan_id": plan_id,
        "generated_at": generated_at,
        "selection": selection,
        "judge_config": judge_config,
        "counts": {
            "expected_cells": expected_cells,
            "attempts_resolved": len(plan_rows),
            "by_judge_mode": by_mode,
            "by_graded_status": by_graded,
            "missing_cells": len(missing),
            "missing_by_reason": by_reason,
            "warm_cache_errors": len(warm_errors),
            "boundary_attempts": len(boundary),
        },
        "boundary": {
            "margin_fraction": boundary_margin,
            "attempts": boundary,
        },
        "missing": missing,
        "warm_cache_errors": [
            {"attempt_id": aid, "error": err} for aid, err in warm_errors.items()
        ],
        "to_grade_attempt_ids": sorted(to_grade_ids),
    }
    with open(out_path, "w") as f:
        _ordered_dump(payload, f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate a judge-grading plan: filter attempts by (tasks x agents), "
            "classify each into standard vs agentic, mark already-graded rows, "
            "and write plan.yaml + report.yaml."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Task selection
    parser.add_argument("--task-ids", type=int, nargs="+", default=None)
    parser.add_argument(
        "--sources",
        nargs="+",
        default=None,
        help="Pull all non-deprecated tasks whose task_source is in this list.",
    )
    parser.add_argument("--all-tasks", action="store_true", default=False)
    parser.add_argument(
        "--exclude-task-ids",
        type=int,
        nargs="+",
        default=None,
        help="Subtract these task ids from the resolved selection.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help=(
            "Cap the resolved task list to the first N tasks (after sources / "
            "exclusions are applied). Tasks are ordered by id."
        ),
    )

    # Agent selection
    parser.add_argument(
        "--agents",
        nargs="+",
        default=None,
        help=(
            "Agents to plan for. Tokens are 'name' or 'name@prompt_version'. "
            "If omitted, DEFAULT_AGENTS (top of this file) is used."
        ),
    )

    # Dedup
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        default=False,
        help=(
            "Disable deduplication. By default, when multiple valid attempts share "
            "(task_id, agent_model_name, prompt_version), only the latest "
            "(max attempt_id) is kept."
        ),
    )

    # Force-mode flags (reuses orchestrator semantics)
    add_agentic_cli_args(parser)

    # Cache warm-up
    parser.add_argument(
        "--warm-cache",
        choices=["auto", "never"],
        default="auto",
        help=(
            "When 'auto' (default), extract CSVs for any attempt whose persistent "
            "cache is missing and publish them so the agentic-routing decision can "
            "be made. 'never' leaves missing-cache rows as judge_mode=unknown."
        ),
    )
    parser.add_argument("--warm-workers", type=int, default=4)
    parser.add_argument(
        "--max-warm",
        type=int,
        default=None,
        help=(
            "Optional safety cap on how many missing caches to generate in this "
            "run. Excess attempts stay as judge_mode=unknown."
        ),
    )
    parser.add_argument(
        "--attempt-sheet-name-filter",
        action="store_true",
        default=False,
        help=(
            "Match the '__sheet_filtered' attempt-cache namespace used by graders "
            "passing --attempt-sheet-name-filter. Filtered and unfiltered "
            "extractions live in separate cache directories."
        ),
    )
    parser.add_argument(
        "--ignore-sheets",
        nargs="+",
        default=["cover"],
        help=(
            "Sheets to drop during extraction (case-insensitive). Default: ['cover']. "
            "Pass an empty list with --ignore-sheets to keep all sheets."
        ),
    )
    parser.add_argument("--run-calculation", action="store_true", default=False)

    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="minimal",
        choices=["none", "minimal", "low", "medium", "high"],
        help=(
            "Reasoning effort the planned grading run will use. Folded into "
            "the existing-grading filter so a row already graded under a "
            "different reasoning effort is not considered a match (default: minimal)."
        ),
    )

    # Other knobs
    parser.add_argument(
        "--ignore-existing-gradings",
        action="store_true",
        default=False,
        help="Force graded=false for every row even if a matching gradings row exists.",
    )
    parser.add_argument(
        "--out-root",
        type=str,
        default=None,
        help="Override output root directory (default: judge/operation_scripts/judge_plan/).",
    )
    parser.add_argument(
        "--boundary-margin",
        type=float,
        default=0.1,
        help=(
            "Fraction of total_limit defining the 'boundary band' for the report. "
            "Attempts whose combined sol+att chars are within this fraction of the "
            "total limit (over or under) are flagged in report.yaml under 'boundary'. "
            "Set to 0 to disable. Default: 0.1 (±10%%)."
        ),
    )

    args = parser.parse_args()

    # Resolve env-driven config
    judge_model = load_env_var("JUDGE_OPENROUTER_MODEL", required=True)
    rubric_version = load_env_var("JUDGE_RUBRIC_VERSION", required=True)
    judge_version_std = int(load_env_var("JUDGE_VERSION", required=True))
    prompt_version_std = str(load_env_var("JUDGE_PROMPT_VERSION", required=True))
    judge_version_agt = int(load_env_var("AGENTIC_JUDGE_VERSION", required=True))
    prompt_version_agt = str(
        load_env_var("AGENTIC_JUDGE_PROMPT_VERSION", required=True)
    )
    solution_limit = int(
        load_env_var("JUDGE_DEFAULT_SOLUTION_CONTEXT_CHAR_LIMIT", required=True)
    )
    attempt_limit = int(
        load_env_var("JUDGE_DEFAULT_ATTEMPT_CONTEXT_CHAR_LIMIT", required=True)
    )
    total_limit = int(
        load_env_var("JUDGE_DEFAULT_TOTAL_CHARACTER_LIMIT", required=True)
    )

    rubric_path = str(
        relative_path_from_project_root(
            load_env_var("JUDGE_RUBRIC", default="./prompts/rubrics/rubric_7.json")
        )
    )

    scratch_base = Path(
        os.environ.get("SCRATCH_PATH")
        or str(
            relative_path_from_project_root(
                load_env_var("PATHS_SCRATCH_PATH", default="./scratch")
            )
        )
    )

    # Plan output dir
    plan_root = (
        Path(args.out_root)
        if args.out_root
        else Path(__file__).resolve().parent / "judge_plan"
    )
    plan_root.mkdir(parents=True, exist_ok=True)
    plan_id = next_plan_id(plan_root)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    plan_dir = plan_root / f"{plan_id:04d}_{timestamp}"
    plan_dir.mkdir(parents=True, exist_ok=False)
    logger.info(f"Plan directory: {plan_dir}")

    # Resolve agents
    agent_tokens = list(args.agents) if args.agents else list(DEFAULT_AGENTS)
    if not agent_tokens:
        raise SystemExit(
            "No agents specified. Provide --agents or set DEFAULT_AGENTS at top of file."
        )
    models, prompt_versions = resolve_agents(agent_tokens)
    logger.info(f"Agents: {models}")
    logger.info(f"Prompt-version filter: {prompt_versions}")

    conn = get_db_connection()
    try:
        task_ids, sources_used = resolve_task_ids(args, conn)
        if not task_ids:
            raise SystemExit("Resolved task list is empty.")
        if args.max_tasks is not None and len(task_ids) > args.max_tasks:
            logger.info(
                f"--max-tasks={args.max_tasks}: capping task list "
                f"(dropping {len(task_ids) - args.max_tasks} task(s))."
            )
            task_ids = task_ids[: args.max_tasks]
        logger.info(f"Tasks ({len(task_ids)}): {task_ids}")
        if sources_used:
            logger.info(f"Sources used: {sources_used}")

        attempts = fetch_valid_attempts(conn, task_ids, models, prompt_versions)
        logger.info(f"Matched {len(attempts)} valid attempts.")

        if not args.no_dedup:
            before = len(attempts)
            attempts = dedup_latest_attempt(attempts)
            logger.info(
                f"Deduped to latest per bucket: {len(attempts)} attempts "
                f"({before - len(attempts)} dropped)."
            )

        # Capture the agent's prompt_version from the unhydrated rows;
        # fetch_attempts_by_ids does not select that column.
        agent_pv_by_id = {a["attempt_id"]: a.get("prompt_version") for a in attempts}

        # Hydrate with full task join (needed for setup_task_folder).
        hydrated = (
            fetch_attempts_by_ids(conn, [a["attempt_id"] for a in attempts])
            if attempts
            else []
        )

        # Drop attempts whose attempt_files contain no xlsx — they would fail
        # at setup_task_folder anyway. Letting them through pollutes
        # warm_cache_errors with opaque "setup_task_folder returned None"
        # messages on every run. The dropped cells are picked up by the gap
        # analysis as 'no_xlsx_in_attempt_files'.
        before = len(hydrated)
        hydrated = [
            h for h in hydrated if _attempt_files_has_xlsx(h.get("attempt_files"))
        ]
        dropped_no_xlsx = before - len(hydrated)
        if dropped_no_xlsx:
            logger.info(
                f"Filtered out {dropped_no_xlsx} attempt(s) with no xlsx in "
                f"attempt_files (will appear in missing_cells)."
            )
            kept_ids = {h["attempt_id"] for h in hydrated}
            attempts = [a for a in attempts if a["attempt_id"] in kept_ids]
            agent_pv_by_id = {aid: agent_pv_by_id[aid] for aid in kept_ids}

        existing_gradings = fetch_existing_gradings(
            conn, [a["attempt_id"] for a in attempts]
        )
        logger.info(
            f"Found {len(existing_gradings)} prior non-failed gradings for these attempts."
        )

        # Gap analysis input
        miss_diag = fetch_missing_diagnostics(conn, task_ids, models)
    finally:
        conn.close()

    # Warm caches if requested
    force_agentic = bool(args.agentic)
    force_no_agentic = bool(args.no_agentic)
    do_warm = (
        args.warm_cache == "auto"
        and not force_agentic
        and not force_no_agentic
        and bool(hydrated)
    )

    warm_errors: dict[int, str] = {}
    if do_warm:
        # Apply --max-warm cap.
        warm_targets: list[dict] = []
        sheet_filtered = bool(args.attempt_sheet_name_filter)
        for a in hydrated:
            sol_final, att_final = cache_paths_for(
                scratch_base, a["attempt_id"], a["task_id"], sheet_filtered
            )
            sol_ok = sol_final.exists() and any(sol_final.glob("*.csv"))
            att_ok = att_final.exists() and any(att_final.glob("*.csv"))
            if not (sol_ok and att_ok):
                warm_targets.append(a)

        if args.max_warm is not None and len(warm_targets) > args.max_warm:
            logger.warning(
                f"--max-warm={args.max_warm}: capping warm-up "
                f"(skipping {len(warm_targets) - args.max_warm} attempts)."
            )
            warm_targets = warm_targets[: args.max_warm]

        scratch_run_dir = (
            scratch_base / "judge_plan_runs" / f"{plan_id:04d}_{timestamp}"
        )
        scratch_run_dir.mkdir(parents=True, exist_ok=True)

        warm_errors = warm_caches_parallel(
            warm_targets,
            scratch_run_dir=scratch_run_dir,
            scratch_base=scratch_base,
            rubric_path=rubric_path,
            sheet_filtered=sheet_filtered,
            ignore_sheets=args.ignore_sheets,
            run_calculation=args.run_calculation,
            workers=args.warm_workers,
        )

    # Build plan rows
    plan_rows: list[dict] = []
    sheet_filtered = bool(args.attempt_sheet_name_filter)
    for a in hydrated:
        attempt_id = a["attempt_id"]
        task_id = a["task_id"]
        sol_final, att_final = cache_paths_for(
            scratch_base, attempt_id, task_id, sheet_filtered
        )
        sol_chars, att_chars = measure_cache(sol_final, att_final, scratch_base)

        mode, mode_reason = classify_judge_mode(
            sol_chars,
            att_chars,
            solution_limit,
            attempt_limit,
            total_limit,
            force_agentic,
            force_no_agentic,
        )

        if mode == "agentic":
            judge_version = judge_version_agt
            judge_pv = prompt_version_agt
        elif mode == "standard":
            judge_version = judge_version_std
            judge_pv = prompt_version_std
        else:
            judge_version = None
            judge_pv = None

        if mode == "unknown" or args.ignore_existing_gradings:
            graded = False
        else:
            graded = is_already_graded(
                existing_gradings,
                attempt_id=attempt_id,
                judge_model=judge_model,
                agentic=(mode == "agentic"),
                judge_version=judge_version,
                prompt_version=judge_pv,
                rubric_version=rubric_version,
                reasoning_effort=args.reasoning_effort,
            )

        plan_rows.append(
            {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "agent_model_name": a.get("agent_model_name"),
                "agent_prompt_version": agent_pv_by_id.get(attempt_id),
                "judge_model": judge_model,
                "judge_mode": mode,
                "judge_mode_reason": mode_reason,
                "judge_version": judge_version,
                "judge_prompt_version": judge_pv,
                "rubric_version": rubric_version,
                "graded": graded,
                "cache_solution_chars": sol_chars,
                "cache_attempt_chars": att_chars,
            }
        )

    # Compute missing cells (gap analysis)
    planned_cells: set[tuple[int, str]] = set()
    for r in plan_rows:
        planned_cells.add((r["task_id"], r["agent_model_name"]))

    missing: list[dict] = []
    for tid in task_ids:
        for model in models:
            if (tid, model) in planned_cells:
                continue
            rows = miss_diag.get((tid, model), [])
            diag = classify_missing(rows, prompt_versions.get(model))
            entry = {
                "task_id": tid,
                "agent_model_name": model,
                "reason": diag["reason"],
            }
            if "latest_attempt_id_seen" in diag:
                entry["latest_attempt_id_seen"] = diag["latest_attempt_id_seen"]
            if "seen_prompt_version" in diag:
                entry["seen_prompt_version"] = diag["seen_prompt_version"]
            missing.append(entry)

    # Sort by reason priority then (task_id, agent).
    reason_rank = {r: i for i, r in enumerate(_MISSING_REASON_PRIORITY)}
    missing.sort(
        key=lambda m: (
            reason_rank.get(m["reason"], 999),
            m["task_id"],
            m["agent_model_name"],
        )
    )

    # Write outputs
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    selection = {
        "task_ids": task_ids,
        "task_sources_used": sources_used,
        "agents": [
            {"name": m, "prompt_version": prompt_versions.get(m)} for m in models
        ],
        "dedup": "latest_per_bucket" if not args.no_dedup else "none",
        "force_agentic": force_agentic,
        "force_no_agentic": force_no_agentic,
        "attempt_sheet_name_filter": sheet_filtered,
    }
    judge_config = {
        "judge_model": judge_model,
        "rubric_version": rubric_version,
        "standard": {
            "judge_version": judge_version_std,
            "prompt_version": prompt_version_std,
        },
        "agentic": {
            "judge_version": judge_version_agt,
            "prompt_version": prompt_version_agt,
        },
        "char_limits": {
            "solution": solution_limit,
            "attempt": attempt_limit,
            "total": total_limit,
        },
    }

    boundary = collect_boundary_attempts(plan_rows, total_limit, args.boundary_margin)

    plan_yaml = plan_dir / "plan.yaml"
    report_yaml = plan_dir / "report.yaml"
    write_plan_yaml(plan_yaml, generated_at, plan_rows)
    write_report_yaml(
        report_yaml,
        plan_id,
        generated_at,
        selection,
        judge_config,
        plan_rows,
        missing,
        warm_errors,
        boundary,
        args.boundary_margin,
    )

    # Console summary
    by_mode = {"agentic": 0, "standard": 0, "unknown": 0}
    graded_n = 0
    for r in plan_rows:
        by_mode[r["judge_mode"]] = by_mode.get(r["judge_mode"], 0) + 1
        if r["graded"]:
            graded_n += 1
    print()
    print("=" * 70)
    print(f"Plan written: {plan_dir}")
    print("=" * 70)
    print(f"  attempts planned:  {len(plan_rows)}")
    print(
        f"  by judge_mode:     agentic={by_mode['agentic']} "
        f"standard={by_mode['standard']} unknown={by_mode['unknown']}"
    )
    print(f"  already graded:    {graded_n}")
    if boundary:
        margin_pct = args.boundary_margin * 100
        print(
            f"  boundary attempts: {len(boundary)} within ±{margin_pct:.0f}% "
            f"of total_limit={total_limit:,}"
        )
    print(f"  missing cells:     {len(missing)}")
    if missing:
        bucket: dict[str, int] = {}
        for m in missing:
            bucket[m["reason"]] = bucket.get(m["reason"], 0) + 1
        print(f"  missing by reason: {bucket}")
    if warm_errors:
        print(f"  warm-cache errors: {len(warm_errors)}")
    print(f"  plan.yaml:         {plan_yaml}")
    print(f"  report.yaml:       {report_yaml}")


if __name__ == "__main__":
    main()
