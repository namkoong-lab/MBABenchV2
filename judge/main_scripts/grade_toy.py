"""Judge-reliability driver: grade the toy Pass/Fail pairs with the production single-pass judge.

Why a sibling entry point: grade_from_db.py takes tickets (task_attempts ids) and writes
`gradings`. The toy pairs have no tickets and must never land in the benchmark tables. This
driver reuses grade_from_db's per-attempt pipeline unchanged — setup_task_folder (any s3://
source), the harness answer check, single_pass_judge_case (rubric_9, guidance, template,
identity, cost tracking) and the S3 artifact upload — and swaps only the two ends:

  * input : the toy_tasks rows — from data/judge_reliability/toy_tasks.jsonl (--source
            local), the judge_reliability.toy_tasks table (--source db), or a manifest
            JSON. The Pass file is the golden solution for both variants; no starting
            workbook.
  * output: toy_runs / toy_gradings instead of gradings, either as rows in the
            judge_reliability schema with their artifacts in the object store
            (--sink db), or as outputs/judge_reliability/{toy_runs,toy_gradings}.jsonl
            with the artifacts under outputs/judge_reliability/<run_id>/<grading_id>/
            (--sink local). Same row shapes either way.

Given neither flag, the ends follow what the machine has: local when no database URL
resolves, otherwise the database — the run log says which.

Targeting: a synthesized rubric-suitability annotation marks every check except the toy's
own as not_applicable, so the judge is prompted for that one check only (the existing
suitability mechanism; no custom prompts). --full-rubric grades all 132 instead
(JUDGE_SKIP_SUITABILITY=1) and still records the target check's verdict.

Usage (repo root):
    python judge/main_scripts/grade_toy.py --run-label run1_targeted --dry-run
    python judge/main_scripts/grade_toy.py --run-label run1 --source local --sink local --repeats 3
    python judge/main_scripts/grade_toy.py --run-label run1_targeted --repeats 3
    python judge/main_scripts/grade_toy.py --run-label run1_targeted --checks 5 47 --variants fail --repeats 1
    python judge/main_scripts/grade_toy.py --resume-run-id <run_id>          # skip what that run already graded
    python judge/main_scripts/grade_toy.py --run-label smoke --checks 122 --stage-only   # no API calls
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_judge_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_judge_root))

from utils import local_store, repo_config, rubric_guidance, rubric_suitability  # noqa: E402
from utils.judge_identity import resolve_judge_identity  # noqa: E402
from utils.llm_utils import get_client  # noqa: E402
from utils.logger import add_log_file, logger  # noqa: E402
from utils.misc_utils import (  # noqa: E402
    load_env_var,
    load_project_configs,
    project_prefix,
    relative_path_from_project_root,
)

_spec = importlib.util.spec_from_file_location(
    "grade_from_db", _judge_root / "main_scripts" / "grade_from_db.py"
)
gfd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gfd)

# The object store is only touched by the db sink; the bucket comes from
# config/config.yaml so no bucket name is pinned here.
TOY_GRADING_PREFIX = "SpreadsheetSmith/judge_reliability/gradings"
SCHEMA = "judge_reliability"


def psycopg2_module():
    """psycopg2, imported on first use — the local ends never need it."""
    return gfd.psycopg2_module()


# ---------------------------------------------------------------------------
# Rubric / suitability helpers
# ---------------------------------------------------------------------------

def load_rubric(rubric_path: str) -> dict:
    with open(rubric_path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if k != "CategoryWeights"}


def flatten(rubric: dict) -> dict[int, tuple[str, str]]:
    out, no = {}, 0
    for cat, checks in rubric.items():
        for c in checks:
            no += 1
            out[no] = (cat, c["name"])
    return out


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def synth_annotation(rubric: dict, target: int, toy: dict, rubric_sha: str) -> dict:
    """Suitability annotation with only `target` applicable (rubric_suitability schema)."""
    rubrics, no = [], 0
    for cat, checks in rubric.items():
        for c in checks:
            no += 1
            rubrics.append({
                "no": no, "category": cat, "name": c["name"],
                "verdict": "applicable" if no == target else "not_applicable",
                "conditional": False, "note": "",
            })
    return {
        "task_id": toy["check_no"], "task_name": toy["folder"], "annotator": "grade_toy",
        "created_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "rubric_version": rubric_sha[:12], "rubric_count": no, "answered": no, "complete": True,
        "rubrics": rubrics, "overall_note": f"toy targeted grading: check {target} only",
        "_staging": {"s3_key": None},
    }


# ---------------------------------------------------------------------------
# Toy sources
# ---------------------------------------------------------------------------

def toys_from_db(conn, checks: list[int] | None) -> list[dict]:
    q = (f"select id, check_no, category, check_name, folder, name_pass, name_fail, s3_pass, s3_fail "
         f"from {SCHEMA}.toy_tasks where not deprecated and coalesce(alignment_verdict, '') <> 'excluded'")
    params = ()
    if checks:
        q += " and check_no = any(%s)"; params = (checks,)
    q += " order by check_no"
    with conn.cursor(cursor_factory=psycopg2_module().extras.RealDictCursor) as cur:
        cur.execute(q, params)
        return [dict(r) for r in cur.fetchall()]


def toys_from_local(checks: list[int] | None) -> list[dict]:
    """The bundled toy rows, filtered like the database query filters them.

    The s3_pass / s3_fail columns are repo-relative paths in the bundle; they
    are resolved here so grade_single_attempt stages them as plain files. A
    workbook the sidecar archive has not delivered still resolves — the plan
    prints it as missing and only that one grading fails.
    """
    rows = []
    for toy in local_store.load_toy_tasks("v2"):
        if checks and toy["check_no"] not in checks:
            continue
        row = dict(toy)
        for variant in ("pass", "fail"):
            row[f"s3_{variant}"] = str(local_store.toy_workbook(toy, variant, "v2"))
        rows.append(row)
    return rows


def toys_from_manifest(path: str, rubric_flat: dict, checks: list[int] | None) -> list[dict]:
    man = json.load(open(path))
    out = []
    for t in man["toys"]:
        n = t["check_no"]
        if checks and n not in checks:
            continue
        cat, name = rubric_flat[n]
        out.append({
            "id": None, "check_no": n, "category": cat, "check_name": name, "folder": t["folder"],
            "name_pass": t["files"]["Pass"]["name"], "name_fail": t["files"]["Fail"]["name"],
            "s3_pass": f"s3://{repo_config.require_s3_bucket()}/{t['files']['Pass']['s3_key']}",
            "s3_fail": f"s3://{repo_config.require_s3_bucket()}/{t['files']['Fail']['s3_key']}",
        })
    return out


def make_attempt(toy: dict, variant: str, repeat_no: int) -> dict:
    """The attempt dict grade_single_attempt expects.

    Sources are whatever the toy source produced: s3:// URIs from the database
    or a manifest, absolute paths from the bundle. setup_task_folder stages
    both the same way."""
    name = toy[f"name_{variant}"]
    src = toy[f"s3_{variant}"]
    return {
        "attempt_id": f"{toy['check_no']}_{variant}_r{repeat_no}",
        "task_id": toy["check_no"],
        "task_name": f"toy{toy['check_no']:03d}",
        "agent_model_name": f"toy_{variant}",
        "agent_model_type": "toy",
        "agent_failed": False,
        "attempt_files": [{"name": name, "path": src}],
        "task_solution_files": [{"name": toy["name_pass"], "path": toy["s3_pass"]}],
        "task_starting_files": None,
    }


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------

def extract_verdict(result: dict, cat: str, name: str, check_no: int) -> dict:
    """Target-check verdict from the recorded artifacts (scores.json is authoritative —
    it already carries any harness overlay); the LLM's own item from ai_judgement."""
    out = {"verdict": None, "check_score": None, "llm_decision": None, "llm_summary": None, "mistakes": None}
    out_dir = Path(result.get("output_dir") or "")
    scores_path = out_dir / "scores.json"
    if scores_path.exists():
        try:
            cs = json.load(open(scores_path)).get("check_scores", {}).get(cat, {}).get(name)
            if cs and not cs.get("unscored"):
                out["check_score"] = cs.get("score")
                out["verdict"] = "pass" if (cs.get("score") or 0) >= 0.999 else "fail"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"  scores.json unreadable: {e}")
    items = (result.get("ai_judgement") or {}).get(cat) or []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("name") == name or str(it.get("check")) == str(check_no):
            out["llm_decision"] = it.get("llm_decision") or it.get("decision")
            out["llm_summary"] = it.get("llm_summary") or it.get("summary")
            out["mistakes"] = it.get("mistakes")
            break
    if out["verdict"] is None and out["llm_decision"] in ("pass", "fail"):
        out["verdict"] = out["llm_decision"]
    return out


# ---------------------------------------------------------------------------
# Writes (toy records only — never the benchmark tables)
#
# Each row is built once, natively, then handed to whichever sink is selected:
# the judge_reliability schema, or outputs/judge_reliability/*.jsonl in the
# same shape. The local rows additionally carry the columns the database fills
# in for itself (id, created_at, the run's counters), so a bundled row and a
# locally produced one read identically.
# ---------------------------------------------------------------------------

def build_run_row(run_id, args, identity, versions, paths, checks, git_commit):
    return {
        "run_id": run_id, "run_label": args.run_label, "purpose": "full" if args.full_rubric else "targeted",
        "grader_model": args.model, "grader_reasoning": args.reasoning_effort or identity.effort,
        "judge_version": versions.get("judge_version"), "prompt_version": versions.get("prompt_version"),
        "rubric_version": versions.get("rubric_version"),
        "template_path": os.path.relpath(paths["template"], _judge_root),
        "template_sha256": sha256_file(paths["template"]), "guidance_sha256": sha256_file(paths["guidance"]),
        "rubric_sha256": sha256_file(paths["rubric"]), "git_commit": git_commit,
        "repeats": args.repeats, "checks": checks, "variants": args.variants,
        "args": json.dumps(vars(args), default=str), "notes": args.notes,
    }


def insert_run(conn, run_id, args, identity, versions, paths, checks, git_commit):
    data = build_run_row(run_id, args, identity, versions, paths, checks, git_commit)
    cols = list(data)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into {SCHEMA}.toy_runs ({', '.join(cols)}) values ({', '.join(f'%({c})s' for c in cols)})", data)
    conn.commit()


def write_run_locally(run_id, args, identity, versions, paths, checks, git_commit):
    row = build_run_row(run_id, args, identity, versions, paths, checks, git_commit)
    row.update(started_at=local_store.now_iso(), finished_at=None,
               n_graded=0, total_cost=0.0)
    path = local_store.append_reliability_row(
        local_store.TOY_RUNS_FILENAME, row, "v2")
    logger.info(f"  toy run row -> {local_store.repo_relative(path)}")


def finish_run_locally(run_id, n_graded, total_cost):
    """Close the local run: one final row carrying the counters the DB updates.

    JSONL is append-only, so the run's totals arrive as a second row for the
    same run_id rather than an edit to the first. A reader takes the last row
    per run_id, which is what read_reliability_rows's order gives it."""
    rows = [r for r in local_store.read_reliability_rows(
        local_store.TOY_RUNS_FILENAME, "v2") if r.get("run_id") == run_id]
    if not rows:
        return
    row = dict(rows[-1])
    row.update(finished_at=local_store.now_iso(), n_graded=n_graded,
               total_cost=round(total_cost, 6))
    local_store.append_reliability_row(local_store.TOY_RUNS_FILENAME, row, "v2")


def build_grading_row(run_id, toy, variant, repeat_no, target, result, verdict, versions, model):
    is_failed = (not result.get("success")) or bool(result.get("hard_parse_failures")) or bool(result.get("has_scoring_warnings"))
    if not result.get("success"):
        failed_reason = result.get("error")
    elif result.get("hard_parse_failures"):
        failed_reason = f"Parse failed: {result['hard_parse_failures']}"
    elif result.get("has_scoring_warnings"):
        failed_reason = f"Scoring warnings: {[k for k, v in (result.get('scoring_warnings') or {}).items() if v]}"
    else:
        failed_reason = None
    errors = {}
    if result.get("parse_failures"):
        errors["parse_failures"] = result["parse_failures"]
    if result.get("has_scoring_warnings"):
        errors["scoring_warnings"] = {k: v for k, v in result["scoring_warnings"].items() if v}
    if result.get("missing_scores"):
        errors["missing_scores"] = result["missing_scores"]  # expected in targeted mode (11 empty categories)
    rv = result.get("versions") or {}
    data = {
        "run_id": run_id, "toy_task_id": toy["id"], "check_no": toy["check_no"], "variant": variant,
        "repeat_no": repeat_no, "target_check": target, "expected": variant,
        "verdict": verdict["verdict"], "correct": (verdict["verdict"] == variant) if verdict["verdict"] else None,
        "check_score": verdict["check_score"], "llm_decision": verdict["llm_decision"],
        "llm_summary": verdict["llm_summary"], "mistakes": json.dumps(verdict["mistakes"]) if verdict["mistakes"] is not None else None,
        "grader_model": model, "grader_reasoning": result.get("judge_reasoning"),
        "judge_version": rv.get("JUDGE_VERSION") or versions.get("judge_version"),
        "prompt_version": rv.get("PROMPT_VERSION") or versions.get("prompt_version"),
        "rubric_version": versions.get("rubric_version"),
        "scored_results": json.dumps(result.get("scored_results") or {}),
        "grader_response": json.dumps(result.get("ai_judgement") or {}),
        "cost": round(result.get("cost") or 0, 6), "time_elapsed_min": round((result.get("elapsed_seconds") or 0) / 60, 4),
        "raw_files_path": result.get("raw_files_path"), "raw_files": json.dumps(result.get("raw_files") or []),
        "errors_encountered": json.dumps(errors) if errors else None,
        "failed": is_failed, "failed_reason": failed_reason,
    }
    return data


def insert_grading(conn, run_id, toy, variant, repeat_no, target, result, verdict, versions, model):
    data = build_grading_row(run_id, toy, variant, repeat_no, target, result, verdict, versions, model)
    cols = list(data)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into {SCHEMA}.toy_gradings ({', '.join(cols)}) values ({', '.join(f'%({c})s' for c in cols)}) returning id", data)
        gid = cur.fetchone()[0]
        cur.execute(
            f"update {SCHEMA}.toy_runs set n_graded = n_graded + 1, total_cost = total_cost + %s where run_id = %s",
            (data["cost"], run_id))
    conn.commit()
    return gid


# The toy_gradings columns the database serialises as JSON.
_JSON_TOY_COLUMNS = ("mistakes", "scored_results", "grader_response", "raw_files",
                     "errors_encountered")


def write_grading_locally(run_id, toy, variant, repeat_no, target, result, verdict, versions, model):
    """Append one toy_gradings-shaped row and stage its artifacts under outputs/."""
    gid = local_store.new_local_id()
    row = build_grading_row(run_id, toy, variant, repeat_no, target, result, verdict, versions, model)
    # The row builder serialises the JSON columns for psycopg2; the bundle
    # keeps them nested (data/README.md), so undo that here.
    for col in _JSON_TOY_COLUMNS:
        if isinstance(row.get(col), str):
            row[col] = json.loads(row[col])
    output_dir = result.get("output_dir")
    if output_dir and Path(output_dir).is_dir():
        raw_files_path, raw_files = local_store.stage_toy_grading_files(
            output_dir, run_id, gid, "v2")
        row["raw_files_path"], row["raw_files"] = raw_files_path, raw_files
        result["raw_files_path"], result["raw_files"] = raw_files_path, raw_files
    row.update(id=gid, deprecated=False, deprecated_reason=None,
               created_at=local_store.now_iso())
    local_store.append_reliability_row(local_store.TOY_GRADINGS_FILENAME, row, "v2")
    return gid


def _with_reconnect(conn, fn):
    """Run fn(conn); on a dropped connection reconnect once and retry. Returns (fn result, live conn)."""
    try:
        return fn(conn), conn
    except (psycopg2_module().OperationalError, psycopg2_module().InterfaceError) as e:
        logger.warning(f"  DB connection lost ({str(e).strip()}); reconnecting and retrying once")
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn = gfd.get_db_connection()
        return fn(conn), conn


def already_graded_local(run_id) -> set:
    """(check_no, variant, repeat_no) already recorded locally for this run."""
    return {
        (r["check_no"], r["variant"], r["repeat_no"])
        for r in local_store.read_reliability_rows(local_store.TOY_GRADINGS_FILENAME, "v2")
        if r.get("run_id") == run_id and not r.get("failed")
    }


def already_graded(conn, run_id) -> set:
    with conn.cursor() as cur:
        cur.execute(f"select check_no, variant, repeat_no from {SCHEMA}.toy_gradings where run_id=%s and not failed", (run_id,))
        return {tuple(r) for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-label", help="human label for this run (required unless --resume-run-id)")
    ap.add_argument("--resume-run-id", help="append to an existing toy_runs row; skips (check, variant, repeat) already graded")
    ap.add_argument("--model", required=True, help="grader label from judge_identities.yaml (e.g. openai/gpt-5.6-sol — the production grader; no default on purpose)")
    ap.add_argument("--reasoning-effort", default=None, choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--checks", type=int, nargs="+", help="rubric check numbers to grade (default: every toy_tasks row)")
    ap.add_argument("--variants", nargs="+", default=["pass", "fail"], choices=["pass", "fail"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--repeat-offset", type=int, default=0, help="first repeat_no is offset+1 (to add repeats to a run)")
    ap.add_argument("--full-rubric", action="store_true", help="grade all 132 checks (JUDGE_SKIP_SUITABILITY=1) instead of the target only")
    ap.add_argument("--manifest", help="read toys from this manifest JSON instead of the toy_tasks rows")
    ap.add_argument("--source", choices=["local", "db"], default=None,
                    help="where the toy rows come from: 'local' = data/judge_reliability/toy_tasks.jsonl "
                         "(workbooks from the sidecar archive), 'db' = judge_reliability.toy_tasks. "
                         "Default: local when no database URL resolves, else db. --manifest overrides both.")
    ap.add_argument("--sink", choices=["local", "db"], default=None,
                    help="where the run and its gradings are recorded: 'local' = "
                         "outputs/judge_reliability/{toy_runs,toy_gradings}.jsonl with artifacts under "
                         "outputs/judge_reliability/<run_id>/, 'db' = the judge_reliability schema plus "
                         "the object store. Same default rule as --source.")
    ap.add_argument("--accuracy-check", default="harness", choices=["harness", "llm"])
    ap.add_argument("--run-calculation", action="store_true")
    ap.add_argument("--max-tool-rounds", type=int, default=None)
    ap.add_argument("--max-forced-rounds", type=int, default=None)
    ap.add_argument("--max-cost-usd", type=float, default=None, help="stop launching new gradings once the run's cost passes this")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--stage-only", action="store_true", help="stage files + suitability + answer check, no LLM call, no writes")
    ap.add_argument("--no-db-write", action="store_true")
    ap.add_argument("--no-s3-upload", action="store_true")
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()
    if not args.run_label and not args.resume_run_id:
        ap.error("--run-label is required (or --resume-run-id)")

    # Where the toys come from and where the records go, decided once and logged.
    args.benchmark = "v2"
    source, sink, why = gfd.resolve_io_mode(args)
    if args.manifest:
        source = "manifest"

    # Production judge configuration for v2, then redirect the artifact prefix to the toy area.
    load_project_configs(benchmark="v2")
    rubric_path = str(relative_path_from_project_root(load_env_var("JUDGE_RUBRIC", required=True)))
    template_path = str(relative_path_from_project_root(load_env_var("SINGLE_PASS_PROMPT_TEMPLATE", required=True)))
    guidance_path = str(rubric_guidance.guidance_path_for(rubric_path))
    versions = {
        "judge_version": load_env_var("SINGLE_PASS_VERSION", default=None),
        "prompt_version": load_env_var("SINGLE_PASS_PROMPT_VERSION", default=None),
        "rubric_version": load_env_var("JUDGE_RUBRIC_VERSION", default=None),
    }
    max_tool_rounds = args.max_tool_rounds or int(load_env_var("SINGLE_PASS_MAX_ROUNDS", default=500))
    identity = resolve_judge_identity(args.model)
    rubric = load_rubric(rubric_path)
    flat = flatten(rubric)
    rubric_sha = sha256_file(rubric_path)
    if args.full_rubric:
        os.environ["JUDGE_SKIP_SUITABILITY"] = "1"

    run_id = args.resume_run_id or f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    if sink == "db":
        # Redirect the artifact prefix to the toy area; the local sink uploads
        # nothing and reads neither var.
        os.environ[f"{project_prefix()}_S3_RAW_FILES_PREFIX"] = f"{TOY_GRADING_PREFIX}/{run_id}"
        os.environ[f"{project_prefix()}_S3_RAW_FILES_BUCKET"] = repo_config.require_s3_bucket()
    else:
        args.no_s3_upload = True

    scratch_base = Path(relative_path_from_project_root(load_env_var("PATHS_SCRATCH_PATH", default="./scratch")))
    scratch_run_dir = scratch_base / "toy_runs" / run_id
    scratch_run_dir.mkdir(parents=True, exist_ok=True)
    add_log_file(str(scratch_run_dir / "run.log"))
    logger.info(f"Toy run {run_id}  label={args.run_label!r}  scratch={scratch_run_dir}")
    logger.info(f"Source: {source}; sink: {sink} ({why})")
    if sink == "local":
        logger.info(f"  records -> {local_store.repo_relative(local_store.reliability_output_dir('v2'))}/")
    else:
        logger.info(f"Database: {repo_config.describe_database_target('v2')}")
    logger.info(f"Grader: {args.model} -> {identity}")
    logger.info(f"Rubric {rubric_path} sha {rubric_sha[:12]}; template {template_path}; guidance {guidance_path}")
    artifacts = (f"s3://<bucket>/{TOY_GRADING_PREFIX}/{run_id}/" if sink == "db"
                 else f"{local_store.repo_relative(local_store.reliability_output_dir('v2'))}/{run_id}/")
    logger.info(f"Mode: {'FULL RUBRIC' if args.full_rubric else 'TARGETED (one check per grading)'}; "
                f"artifacts -> {artifacts}")

    needs_db = (source == "db" or (sink == "db" and not args.no_db_write))
    conn = gfd.get_db_connection() if needs_db else None
    if args.manifest:
        toys = toys_from_manifest(args.manifest, flat, args.checks)
    elif source == "local":
        toys = toys_from_local(args.checks)
    else:
        toys = toys_from_db(conn, args.checks)
    # Retired checks (judge v9): the judge forces them not_applicable on every
    # grading, so a targeted toy for one would prompt for nothing. Skip them.
    retired = rubric_suitability.retired_check_numbers(rubric)
    dropped = [t["check_no"] for t in toys if t["check_no"] in retired]
    if dropped:
        logger.warning(f"Skipping toys for RETIRED checks {dropped} (project_configs judge.retired_checks)")
        toys = [t for t in toys if t["check_no"] not in retired]
    if not toys:
        logger.error("No toys selected"); sys.exit(3)
    unknown = sorted(set(args.checks or []) - {t["check_no"] for t in toys})
    if unknown:
        logger.warning(f"No toy for checks {unknown} (not ingested yet)")

    # Plan: repeat-major so a partial run still covers every toy once.
    repeats = range(args.repeat_offset + 1, args.repeat_offset + args.repeats + 1)
    plan = [(r, t, v) for r in repeats for t in toys for v in args.variants]
    done = already_graded_local(run_id) if (args.resume_run_id and sink == "local") else (
        already_graded(conn, run_id) if (args.resume_run_id and conn is not None) else set())
    plan = [p for p in plan if (p[1]["check_no"], p[2], p[0]) not in done]
    logger.info(f"Plan: {len(plan)} gradings = {len(toys)} toys x {len(args.variants)} variants x {args.repeats} repeats"
                + (f" (skipping {len(done)} already in run)" if done else ""))
    for r, t, v in plan[:400]:
        gap = "" if (source != "local" or Path(t[f"s3_{v}"]).is_file()) else "  [MISSING]"
        logger.info(f"  r{r} #{t['check_no']:3d} {v:4s} {t['category']} / {t['check_name']}  <- {t[f'name_{v}']}{gap}")
    if source == "local":
        absent = {t["check_no"] for _, t, v in plan if not Path(t[f"s3_{v}"]).is_file()}
        if absent:
            logger.warning(
                f"{len(absent)} of {len(toys)} toy(s) have no workbook on disk. The "
                f"toy rows are tracked but their workbooks are not — unpack "
                f"{local_store.TOY_ARCHIVE_NAME} into place first; "
                f"scripts/verify_offline_bundle.py checks it. Those gradings will "
                f"be recorded as failures.")
    if args.dry_run:
        logger.info("DRY RUN — nothing graded"); return

    git_commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(_judge_root)).stdout.strip() or None
    record = not args.no_db_write and not args.stage_only and (sink == "local" or conn is not None)
    write_db = record and sink == "db"
    paths = {"template": template_path, "guidance": guidance_path, "rubric": rubric_path}
    if record and not args.resume_run_id:
        checks_covered = sorted({t["check_no"] for t in toys})
        if sink == "local":
            write_run_locally(run_id, args, identity, versions, paths, checks_covered, git_commit)
        else:
            insert_run(conn, run_id, args, identity, versions, paths, checks_covered, git_commit)
    client = None if args.stage_only else get_client(identity)
    suitability_dir = scratch_run_dir / "suitability"; suitability_dir.mkdir(exist_ok=True)

    run_cost, n_ok, n_bad, n_correct = 0.0, 0, 0, 0
    for i, (repeat_no, toy, variant) in enumerate(plan, 1):
        check_no = toy["check_no"]; cat, name = flat[check_no]
        if args.max_cost_usd is not None and run_cost >= args.max_cost_usd:
            logger.warning(f"Cost cap {args.max_cost_usd} reached at ${run_cost:.2f}; stopping before {i}/{len(plan)}"); break
        logger.info(f"\n[{i}/{len(plan)}] check {check_no} ({cat} / {name}) variant={variant} repeat={repeat_no}")
        ext = Path(toy[f"name_{variant}"]).suffix.lower()
        src = Path(toy[f"s3_{variant}"])
        if ext not in gfd._EXCEL_EXTS:
            result = {"success": False, "error": f"attempt file is {ext}; the judge pipeline stages .xlsx/.xlsm/.xls only",
                      "elapsed_seconds": 0, "cost": 0}
            logger.error(f"  {result['error']}")
        elif source == "local" and not src.is_file():
            result = {"success": False, "error": local_store.missing_toy_message(src),
                      "elapsed_seconds": 0, "cost": 0}
            logger.error(f"  {result['error']}")
        else:
            suit_path = None
            if not args.full_rubric:
                suit_path = suitability_dir / f"check_{check_no}.json"
                suit_path.write_text(json.dumps(synth_annotation(rubric, check_no, toy, rubric_sha), indent=1))
            result = gfd.grade_single_attempt(
                attempt=make_attempt(toy, variant, repeat_no), client=client, rubric_path=rubric_path,
                template_path=template_path, agentic_template_path=template_path, model=args.model,
                scratch_run_dir=scratch_run_dir, nocall=args.stage_only, run_calculation=args.run_calculation,
                agentic=True, single_pass=True, max_tool_rounds=max_tool_rounds,
                no_s3_upload=args.no_s3_upload or args.stage_only or sink == "local",
                reasoning_effort=args.reasoning_effort,
                suitability_source_path=suit_path, accuracy_check=args.accuracy_check,
                max_forced_rounds=args.max_forced_rounds,
            )
        if args.stage_only:
            logger.info(f"  staged: {result.get('task_folder')} (no LLM call)"); continue
        verdict = extract_verdict(result, cat, name, check_no) if result.get("success") else {
            "verdict": None, "check_score": None, "llm_decision": None, "llm_summary": None, "mistakes": None}
        run_cost += result.get("cost") or 0
        ok = verdict["verdict"] == variant
        if result.get("success"):
            n_ok += 1; n_correct += int(ok)
        else:
            n_bad += 1
        logger.info(f"  -> verdict={verdict['verdict']} expected={variant} {'OK' if ok else 'MISS'}  "
                    f"cost=${result.get('cost') or 0:.3f}  run=${run_cost:.2f}  {result.get('raw_files_path') or ''}")
        if record and sink == "local":
            gid = write_grading_locally(run_id, toy, variant, repeat_no,
                                        None if args.full_rubric else check_no,
                                        result, verdict, versions, args.model)
            logger.info(f"  toy grading {gid} -> {result.get('raw_files_path') or 'no artifacts'}")
        elif write_db:
            # A single grading can run 40+ minutes (65 MB workbooks); a managed
            # Postgres drops idle SSL sessions well before that, so reconnect and
            # retry once. If the retry also fails, park the row locally
            # (pending_toy_grading.json in the bundle) instead of aborting the run.
            try:
                gid, conn = _with_reconnect(conn, lambda c: insert_grading(
                    c, run_id, toy, variant, repeat_no, None if args.full_rubric else check_no,
                    result, verdict, versions, args.model))
                logger.info(f"  toy_gradings id {gid}")
            except psycopg2_module().Error as e:
                pend = Path(result.get("output_dir") or scratch_run_dir) / "pending_toy_grading.json"
                pend.write_text(json.dumps({"run_id": run_id, "toy": toy, "variant": variant, "repeat_no": repeat_no,
                                            "target": None if args.full_rubric else check_no, "result": result,
                                            "verdict": verdict, "versions": versions, "model": args.model}, default=str))
                logger.error(f"  DB insert failed twice ({e}); row parked at {pend} — backfill with operation_scripts/backfill_toy_grading.py")
    if record and sink == "local":
        finish_run_locally(run_id, n_ok + n_bad, run_cost)
    elif write_db:
        def _finish(c):
            with c.cursor() as cur:
                cur.execute(f"update {SCHEMA}.toy_runs set finished_at = now() where run_id = %s", (run_id,))
            c.commit()
        _, conn = _with_reconnect(conn, _finish)
    logger.info(f"\nRun {run_id}: {n_ok} graded ({n_correct} correct), {n_bad} failed, ${run_cost:.2f}")


if __name__ == "__main__":
    main()
