"""Judge-reliability driver: grade the toy Pass/Fail pairs with the production single-pass judge.

Why a sibling entry point: grade_from_db.py takes tickets (task_attempts ids) and writes
`gradings`. The toy pairs have no tickets and must never land in the benchmark tables. This
driver reuses grade_from_db's per-attempt pipeline unchanged — setup_task_folder (any s3://
source), the harness answer check, single_pass_judge_case (rubric_9, guidance, template,
identity, cost tracking) and the S3 artifact upload — and swaps only the two ends:

  * input : judge_reliability.toy_tasks rows (or a local manifest JSON) instead of DB attempts.
            The Pass file is the golden solution for both variants; no starting workbook.
  * output: judge_reliability.toy_runs / toy_gradings instead of gradings, with the same
            raw_files_path / raw_files pointers into S3 under
            MBABenchV2/judge_reliability/gradings/<run_id>/<ts>_<uuid>/.

Targeting: a synthesized rubric-suitability annotation marks every check except the toy's
own as not_applicable, so the judge is prompted for that one check only (the existing
suitability mechanism; no custom prompts). --full-rubric grades all 132 instead
(JUDGE_SKIP_SUITABILITY=1) and still records the target check's verdict.

Usage (repo root):
    python judge/main_scripts/grade_toy.py --run-label run1_targeted --dry-run
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

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from utils import repo_config, rubric_guidance  # noqa: E402
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

S3_BUCKET = "mbabench"
TOY_GRADING_PREFIX = "MBABenchV2/judge_reliability/gradings"
SCHEMA = "judge_reliability"


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
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(q, params)
        return [dict(r) for r in cur.fetchall()]


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
            "s3_pass": f"s3://{S3_BUCKET}/{t['files']['Pass']['s3_key']}",
            "s3_fail": f"s3://{S3_BUCKET}/{t['files']['Fail']['s3_key']}",
        })
    return out


def make_attempt(toy: dict, variant: str, repeat_no: int) -> dict:
    """The attempt dict grade_single_attempt expects; sources are s3:// URIs."""
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
# DB writes (toy tables only)
# ---------------------------------------------------------------------------

def insert_run(conn, run_id, args, identity, versions, paths, checks, git_commit):
    data = {
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
    cols = list(data)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into {SCHEMA}.toy_runs ({', '.join(cols)}) values ({', '.join(f'%({c})s' for c in cols)})", data)
    conn.commit()


def insert_grading(conn, run_id, toy, variant, repeat_no, target, result, verdict, versions, model):
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
    ap.add_argument("--manifest", help="read toys from this manifest JSON instead of judge_reliability.toy_tasks")
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
    os.environ[f"{project_prefix()}_S3_RAW_FILES_PREFIX"] = f"{TOY_GRADING_PREFIX}/{run_id}"
    os.environ[f"{project_prefix()}_S3_RAW_FILES_BUCKET"] = S3_BUCKET

    scratch_base = Path(relative_path_from_project_root(load_env_var("PATHS_SCRATCH_PATH", default="./scratch")))
    scratch_run_dir = scratch_base / "toy_runs" / run_id
    scratch_run_dir.mkdir(parents=True, exist_ok=True)
    add_log_file(str(scratch_run_dir / "run.log"))
    logger.info(f"Toy run {run_id}  label={args.run_label!r}  scratch={scratch_run_dir}")
    logger.info(f"Database: {repo_config.describe_database_target('v2')}")
    logger.info(f"Grader: {args.model} -> {identity}")
    logger.info(f"Rubric {rubric_path} sha {rubric_sha[:12]}; template {template_path}; guidance {guidance_path}")
    logger.info(f"Mode: {'FULL RUBRIC' if args.full_rubric else 'TARGETED (one check per grading)'}; "
                f"S3 artifacts -> s3://{S3_BUCKET}/{TOY_GRADING_PREFIX}/{run_id}/")

    conn = None if (args.no_db_write and args.manifest) else gfd.get_db_connection()
    toys = toys_from_manifest(args.manifest, flat, args.checks) if args.manifest else toys_from_db(conn, args.checks)
    if not toys:
        logger.error("No toys selected"); sys.exit(3)
    unknown = sorted(set(args.checks or []) - {t["check_no"] for t in toys})
    if unknown:
        logger.warning(f"No toy for checks {unknown} (not ingested yet)")

    # Plan: repeat-major so a partial run still covers every toy once.
    repeats = range(args.repeat_offset + 1, args.repeat_offset + args.repeats + 1)
    plan = [(r, t, v) for r in repeats for t in toys for v in args.variants]
    done = already_graded(conn, run_id) if (args.resume_run_id and conn is not None) else set()
    plan = [p for p in plan if (p[1]["check_no"], p[2], p[0]) not in done]
    logger.info(f"Plan: {len(plan)} gradings = {len(toys)} toys x {len(args.variants)} variants x {args.repeats} repeats"
                + (f" (skipping {len(done)} already in run)" if done else ""))
    for r, t, v in plan[:400]:
        logger.info(f"  r{r} #{t['check_no']:3d} {v:4s} {t['category']} / {t['check_name']}  <- {t[f'name_{v}']}")
    if args.dry_run:
        logger.info("DRY RUN — nothing graded"); return

    git_commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(_judge_root)).stdout.strip() or None
    write_db = conn is not None and not args.no_db_write and not args.stage_only
    if write_db and not args.resume_run_id:
        insert_run(conn, run_id, args, identity, versions, {"template": template_path, "guidance": guidance_path, "rubric": rubric_path},
                   sorted({t["check_no"] for t in toys}), git_commit)
    client = None if args.stage_only else get_client(identity)
    suitability_dir = scratch_run_dir / "suitability"; suitability_dir.mkdir(exist_ok=True)

    run_cost, n_ok, n_bad, n_correct = 0.0, 0, 0, 0
    for i, (repeat_no, toy, variant) in enumerate(plan, 1):
        check_no = toy["check_no"]; cat, name = flat[check_no]
        if args.max_cost_usd is not None and run_cost >= args.max_cost_usd:
            logger.warning(f"Cost cap {args.max_cost_usd} reached at ${run_cost:.2f}; stopping before {i}/{len(plan)}"); break
        logger.info(f"\n[{i}/{len(plan)}] check {check_no} ({cat} / {name}) variant={variant} repeat={repeat_no}")
        ext = Path(toy[f"name_{variant}"]).suffix.lower()
        if ext not in gfd._EXCEL_EXTS:
            result = {"success": False, "error": f"attempt file is {ext}; the judge pipeline stages .xlsx/.xlsm/.xls only",
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
                no_s3_upload=args.no_s3_upload or args.stage_only, reasoning_effort=args.reasoning_effort,
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
        if write_db:
            gid = insert_grading(conn, run_id, toy, variant, repeat_no, None if args.full_rubric else check_no,
                                 result, verdict, versions, args.model)
            logger.info(f"  toy_gradings id {gid}")
    if write_db:
        with conn.cursor() as cur:
            cur.execute(f"update {SCHEMA}.toy_runs set finished_at = now() where run_id = %s", (run_id,))
        conn.commit()
    logger.info(f"\nRun {run_id}: {n_ok} graded ({n_correct} correct), {n_bad} failed, ${run_cost:.2f}")


if __name__ == "__main__":
    main()
