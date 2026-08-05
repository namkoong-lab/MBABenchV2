"""Single-task runner — the core of the coding-agent pipeline.

One invocation = one attempt of one task. Batch sweeps are driven by an
external orchestrator script that simply calls this repeatedly.

Internal mode (MBABench V1 benchmark):
    python -m coding_agent.run_task --config run_configs/example_fable.yaml --task-id 83

External mode (your own task, your own keys, no MBABench access):
    python -m coding_agent.run_task --config run_configs/example_external.yaml \
        --task-dir ./my_task --results-dir ./results

Exit codes: 0 success | 2 agent_failure | 3 timeout | 4 infra_failure |
            5 needs_review (orchestrators branch on these).
"""
import argparse
import json
import sys
from pathlib import Path

from .agents import agent_env, build_command
from .config import load_config, load_dotenv_if_present, require_env
from .prompt_builder import build_prompt
from .recorder import record
from .sandbox import run_in_sandbox
from .task_source import ExternalSource, InternalSource
from .telemetry import parse_transcript, write_telemetry
from .validate import validate, write_verdict
from .workspace import create_attempt

EXIT_CODES = {"success": 0, "agent_failure": 2, "timeout": 3,
              "infra_failure": 4, "needs_review": 5}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one coding-agent task attempt")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", type=int, help="internal mode: benchmark task id")
    parser.add_argument("--task-dir", help="external mode: local task folder")
    parser.add_argument("--results-dir", default="results", help="external mode: output folder")
    args = parser.parse_args(argv)

    load_dotenv_if_present()
    cfg = load_config(args.config)
    require_env(cfg)

    if cfg.mode == "internal":
        if args.task_id is None:
            parser.error("internal mode requires --task-id")
        source = InternalSource(args.task_id)
    else:
        if not args.task_dir:
            parser.error("external mode requires --task-dir")
        source = ExternalSource(args.task_dir)

    import os

    # 1. Fetch task + seed workspace (any failure here is infra, no trial burned).
    try:
        staging = cfg.workspaces_dir / "_staging"
        spec = source.fetch(staging)
        attempt = create_attempt(cfg.workspaces_dir, spec)
    except Exception as e:  # noqa: BLE001 — classified, reported, non-zero exit
        print(f"❌ infra_failure during task staging: {e}")
        return EXIT_CODES["infra_failure"]

    print(f"▶ task {spec.task_id or spec.task_name} ({spec.task_source}) | "
          f"{cfg.agent.cli}/{cfg.agent.model} | sandbox={cfg.sandbox.mode}")
    print(f"  attempt dir: {attempt.attempt_dir}")

    # 2. Prompt.
    prompt_text, prompt_version = build_prompt(cfg, spec, attempt.workspace)
    print(f"  prompt_version={prompt_version} ({len(prompt_text):,} chars)")

    # 3. Run the agent in the sandbox.
    cmd = build_command(cfg.agent)
    env = agent_env(cfg.agent, cfg.api_key_env, os.environ[cfg.api_key_env])
    sandbox = run_in_sandbox(cfg, cmd, env, attempt.workspace, attempt.attempt_dir)
    print(f"  agent finished: exit={sandbox.exit_code} "
          f"timed_out={sandbox.timed_out} duration={sandbox.duration_seconds:.0f}s")

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
