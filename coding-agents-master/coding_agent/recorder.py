"""Recorder: persist a validated attempt.

Internal mode — MBABench V1 conventions (same as the CLI wave):
  S3:  s3://<bucket>/BizbenchV1/attempts/<identity>/task_source=<src>/task_id=<id>/<ts>_<file>
       s3://<bucket>/BizbenchV1/prompts/<identity>/<ts>_<promptfile>
  DB:  INSERT INTO task_attempts (...)  — solution.xlsx is listed FIRST in
       attempt_files (the judge grades the first xlsx in the list).
  Verdicts infra_failure / needs_review write NO row (no trial burned; held
  locally); success / timeout / agent_failure write a row.

External mode — everything stays local in a results folder; no DB, no S3.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from .config import PROMPTS_DIR, RunConfig
from .prompt_builder import prompt_file_paths
from .sandbox import SandboxResult
from .task_source import TaskSpec
from .validate import Verdict
from .workspace import Attempt

RECORDABLE = {"success", "timeout", "agent_failure"}


def record(cfg: RunConfig, spec: TaskSpec, attempt: Attempt, sandbox: SandboxResult,
           verdict: Verdict, telemetry: dict, prompt_version: int,
           results_dir: Path | None = None) -> dict:
    summary = {
        "identity": cfg.identity,
        "task_id": spec.task_id,
        "task_name": spec.task_name,
        "status": verdict.status,
        "reason": verdict.reason,
        "duration_min": round(sandbox.duration_seconds / 60.0, 2),
        "prompt_version": prompt_version,
        "cost_usd": telemetry.get("cost_usd"),
        "tokens": telemetry.get("totals"),
        "recorded": False,
    }

    if cfg.mode == "external":
        dest = (results_dir or Path("results")) / attempt.attempt_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("transcript.jsonl", "telemetry.json", "verdict.json", "manifest.json"):
            src = attempt.attempt_dir / name
            if src.exists():
                shutil.copy2(src, dest / name)
        if verdict.solution_path and verdict.solution_path.exists():
            shutil.copy2(verdict.solution_path, dest / "solution.xlsx")
        shutil.copy2(attempt.workspace / "PROMPT.md", dest / "PROMPT.md")
        summary["recorded"] = True
        summary["results_dir"] = str(dest)
        (dest / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary

    if verdict.status not in RECORDABLE:
        summary["note"] = "not recorded to DB (infra_failure/needs_review are held locally)"
        (attempt.attempt_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary

    import boto3
    import psycopg2
    from psycopg2.extras import Json

    s3 = boto3.client("s3")
    bucket, root = cfg.internal.s3_bucket, cfg.internal.s3_root
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Prompt snapshot (system + the template actually used).
    prompt_uris = []
    for path in prompt_file_paths(cfg, spec.task_source):
        key = f"{root}/prompts/{cfg.identity}/{ts}_{path.name}"
        s3.upload_file(str(path), bucket, key)
        prompt_uris.append(f"s3://{bucket}/{key}")

    # Attempt artifacts — solution first (the judge takes the first xlsx).
    base = f"{root}/attempts/{cfg.identity}/task_source={spec.task_source}/task_id={spec.task_id}/{ts}"
    uploads = []
    if verdict.solution_path and verdict.solution_path.exists():
        uploads.append((verdict.solution_path, f"{base}_solution.xlsx"))
    uploads.append((attempt.workspace / "PROMPT.md", f"{base}_PROMPT.md"))
    for name in ("transcript.jsonl", "telemetry.json", "verdict.json"):
        path = attempt.attempt_dir / name
        if path.exists():
            uploads.append((path, f"{base}_{name}"))

    attempt_files = []
    for local, key in uploads:
        s3.upload_file(str(local), bucket, key)
        attempt_files.append(f"s3://{bucket}/{key}")

    agent_failed = verdict.status != "success"
    conn = psycopg2.connect(__import__("os").environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_attempts
                  (task_id, prompt_files, start_time, end_time, agent_model_name,
                   agent_model_type, attempt_files, time_taken_min, cost,
                   agent_failed, agent_failed_reason, deprecated, prompt_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    spec.task_id,
                    Json(prompt_uris),
                    attempt.started_at,
                    datetime.now(),
                    cfg.identity,
                    "coding_cli",
                    Json(attempt_files),
                    round(sandbox.duration_seconds / 60.0, 2),
                    telemetry.get("cost_usd"),
                    agent_failed,
                    None if not agent_failed else verdict.reason,
                    False,
                    prompt_version,
                ),
            )
            summary["attempt_id"] = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    summary["recorded"] = True
    summary["attempt_files"] = attempt_files
    (attempt.attempt_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
