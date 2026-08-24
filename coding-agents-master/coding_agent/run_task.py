"""Single-task runner — the core of the coding-agent pipeline.

One invocation = one attempt of one task. Batch sweeps are driven by an
external orchestrator script that simply calls this repeatedly.

Internal mode (a BizbenchV1 or MBABenchV2 task, per the config's `benchmark`):
    python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11

External mode (your own task, your own keys, no MBABench access):
    python -m coding_agent.run_task --config run_configs/example_external.yaml \
        --task-dir ./my_task --results-dir ./results

Exit codes: 0 success | 2 agent_failure | 3 timeout | 4 infra_failure |
            5 needs_review (orchestrators branch on these).
"""
import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .agents import agent_env, build_command
from .config import load_config, load_dotenv_if_present, resolve_secrets
from .prompt_builder import build_prompt, prompt_file_paths
from .recorder import has_extra_configs_column, record
from .repo_config import describe_database_target
from .sandbox import run_in_sandbox
from .task_source import ExternalSource, InternalSource, verify_s3_access
from .telemetry import parse_transcript, write_telemetry
from .validate import validate, write_verdict
from .workspace import create_attempt

EXIT_CODES = {"success": 0, "agent_failure": 2, "timeout": 3,
              "infra_failure": 4, "needs_review": 5}


def _snapshot_run_inputs(cfg, spec, attempt) -> None:
    """Copy the run config and the exact prompt files into the attempt dir,
    so the local record says what the attempt ran with even if the repo
    files change later (and before any upload can fail)."""
    if cfg.config_path and cfg.config_path.exists():
        shutil.copy2(cfg.config_path, attempt.attempt_dir / "run_config.yaml")
    prompts_dir = attempt.attempt_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for path in prompt_file_paths(cfg, spec.task_source):
        shutil.copy2(path, prompts_dir / path.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one coding-agent task attempt")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", type=int, help="internal mode: benchmark task id")
    parser.add_argument("--task-dir", help="external mode: local task folder")
    parser.add_argument("--results-dir", default="results", help="external mode: output folder")
    args = parser.parse_args(argv)

    load_dotenv_if_present()
    cfg = load_config(args.config)
    api_key = resolve_secrets(cfg)

    print(f"▶ {cfg.agent_model_name} | benchmark={cfg.benchmark} | mode={cfg.mode} "
          f"| sandbox={cfg.sandbox.mode}")
    print(f"  pinned by identity: {json.dumps(cfg.identity.settings())}")

    if cfg.mode == "internal":
        if args.task_id is None:
            parser.error("internal mode requires --task-id")
        print(f"  database: {describe_database_target(cfg.benchmark)}")
        print(f"  s3: s3://{cfg.s3_bucket}/{cfg.s3_root}/")
        source = InternalSource(args.task_id, cfg.db_url)
        try:
            verify_s3_access(cfg.s3_bucket)
            extra_ok = has_extra_configs_column(cfg.db_url)
        except Exception as e:  # noqa: BLE001 — classified, reported, non-zero exit
            print(f"❌ infra_failure: S3/DB preflight failed for bucket "
                  f"'{cfg.s3_bucket}' — fix credentials before running "
                  f"(nothing was staged, no trial burned): {e}")
            return EXIT_CODES["infra_failure"]
        print(f"  extra_configs column: {'yes' if extra_ok else 'NO — run settings will only be in the local attempt dir'}")
    else:
        if not args.task_dir:
            parser.error("external mode requires --task-dir")
        source = ExternalSource(args.task_dir)

    # 1. Fetch task + seed workspace (any failure here is infra, no trial burned).
    staging = cfg.workspaces_dir / f"_staging_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}"
    try:
        spec = source.fetch(staging)
        attempt = create_attempt(cfg.workspaces_dir, spec)
        _snapshot_run_inputs(cfg, spec, attempt)
    except Exception as e:  # noqa: BLE001 — classified, reported, non-zero exit
        print(f"❌ infra_failure during task staging: {e}")
        return EXIT_CODES["infra_failure"]
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # inputs are copied into the workspace

    print(f"▶ task {spec.task_id or spec.task_name} ({spec.task_source}) | "
          f"{cfg.agent.cli}/{cfg.agent.model}")
    print(f"  attempt dir: {attempt.attempt_dir}")

    # 2. Prompt.
    prompt_text, prompt_version = build_prompt(cfg, spec, attempt.workspace)
    print(f"  prompt_version={prompt_version} ({len(prompt_text):,} chars)")

    # 3. Run the agent in the sandbox.
    cmd = build_command(cfg.agent, relay=cfg.record_trajectory and cfg.sandbox.mode == "docker")
    env = agent_env(cfg.agent, cfg.api_key_env, api_key)
    sandbox = run_in_sandbox(cfg, cmd, env, attempt.workspace, attempt.attempt_dir)
    print(f"  agent finished: exit={sandbox.exit_code} "
          f"timed_out={sandbox.timed_out} duration={sandbox.duration_seconds:.0f}s")

    # 3b. Compress the trajectory capture (if any) into an attempt artifact.
    traj = attempt.attempt_dir / "trajectory" / "trajectory.jsonl"
    if traj.exists() and traj.stat().st_size:
        with open(traj, "rb") as fin, gzip.open(attempt.attempt_dir / "trajectory.jsonl.gz", "wb") as fout:
            shutil.copyfileobj(fin, fout)
        steps = sum(1 for _ in open(traj, errors="replace"))
        print(f"  trajectory: {steps} API calls captured "
              f"({(attempt.attempt_dir / 'trajectory.jsonl.gz').stat().st_size:,} bytes gz)")

    # 4. Telemetry (best-effort, never fatal).
    telemetry = parse_transcript(sandbox.transcript_path, cfg.agent.cli)
    write_telemetry(attempt.attempt_dir, telemetry)
    if telemetry.get("totals"):
        print(f"  tokens: {telemetry['totals']} | cost: {telemetry.get('cost_usd')}")

    # 5. Verdict.
    verdict = validate(attempt, sandbox, cfg.limits.junk_seconds)
    write_verdict(attempt, verdict, sandbox)
    print(f"  verdict: {verdict.status} — {verdict.reason}")

    # 6. Record.
    try:
        summary = record(cfg, spec, attempt, sandbox, verdict, telemetry,
                         prompt_version, results_dir=Path(args.results_dir))
    except Exception as e:  # noqa: BLE001 — recording failure must be loud but classified
        print(f"❌ infra_failure during recording (attempt artifacts kept at "
              f"{attempt.attempt_dir}): {e}")
        return EXIT_CODES["infra_failure"]

    print(json.dumps(summary, indent=2))
    return EXIT_CODES[verdict.status]


if __name__ == "__main__":
    sys.exit(main())
