"""Sandbox: run the agent command in an isolated container (or, for rung-0
debugging only, directly on the host — UNSANDBOXED).

Docker mode, per attempt:
  - fresh container from the pinned image, removed afterwards
  - only the workspace is mounted (at /workspace, the working dir)
  - env carries exactly one secret: the model API key
  - entrypoint applies a default-deny egress firewall (allowlist = model API);
    a firewall failure aborts the attempt (fail-safe) rather than running open
  - hard wall-clock kill takes down the whole container

The agent's stdout (its machine-readable transcript) streams to
attempt_dir/transcript.jsonl; stderr to attempt_dir/agent_stderr.log.
"""
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import RunConfig


@dataclass
class SandboxResult:
    exit_code: int | None  # None if we killed it
    duration_seconds: float
    timed_out: bool
    transcript_path: Path
    stderr_path: Path
    infra_error: str | None = None  # set when the sandbox itself failed


def run_in_sandbox(cfg: RunConfig, agent_cmd: list, agent_env: dict,
                   workspace: Path, attempt_dir: Path) -> SandboxResult:
    transcript = attempt_dir / "transcript.jsonl"
    stderr_log = attempt_dir / "agent_stderr.log"
    prompt_file = workspace / "PROMPT.md"

    if cfg.sandbox.mode == "docker":
        name = f"coding-agent-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "-i",
            "--name", name,
            "-v", f"{workspace.resolve()}:/workspace",
            "-w", "/workspace",
            "--cap-add", "NET_ADMIN",  # entrypoint needs it to install the firewall, then drops to the agent user
            "--memory", cfg.sandbox.memory,
            "--cpus", str(cfg.sandbox.cpus),
            "--pids-limit", "1024",
            "-e", f"ALLOWED_DOMAINS={','.join(cfg.allowed_domains)}",
        ]
        for key, value in agent_env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [cfg.sandbox.image] + agent_cmd
        env = None  # secrets go into the container only via -e, not the docker client env
        kill = lambda: subprocess.run(["docker", "kill", name], capture_output=True)
    elif cfg.sandbox.mode == "host":
        print("⚠️  sandbox.mode=host — UNSANDBOXED run (dev/rung-0 only). "
              "No isolation, no network restrictions.")
        cmd = agent_cmd
        import os
        env = {**os.environ, **agent_env}
        kill = None  # process-group kill handled below
        name = None
    else:
        raise ValueError(cfg.sandbox.mode)

    start = time.monotonic()
    try:
        with open(prompt_file, "rb") as stdin, \
             open(transcript, "wb") as out, open(stderr_log, "wb") as err:
            proc = subprocess.Popen(
                cmd, stdin=stdin, stdout=out, stderr=err, env=env,
                cwd=None if cfg.sandbox.mode == "docker" else str(workspace),
                start_new_session=(cfg.sandbox.mode == "host"),
            )
            try:
                proc.wait(timeout=cfg.limits.wall_clock_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                if kill:
                    kill()
                else:
                    import os, signal
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=60)
    except FileNotFoundError as e:
        # docker (or the agent CLI in host mode) is not installed
        return SandboxResult(None, time.monotonic() - start, False,
                             transcript, stderr_log,
                             infra_error=f"Sandbox launch failed: {e}")

    duration = time.monotonic() - start
    exit_code = None if timed_out else proc.returncode

    # Docker's own failures (bad image, daemon down, firewall abort) surface as
    # exit codes 125-127 from `docker run`, or our entrypoint's dedicated 97.
    infra_error = None
    if cfg.sandbox.mode == "docker" and exit_code not in (0, None):
        tail = ""
        try:
            tail = stderr_log.read_text(errors="replace")[-2000:]
        except OSError:
            pass
        if exit_code in (97, 125, 126, 127):
            infra_error = f"Container/infra failure (exit {exit_code}): {tail}"
        elif "docker" in tail.lower() and ("daemon" in tail.lower() or "docker.sock" in tail.lower()):
            infra_error = f"Docker daemon unreachable (is Docker Desktop running?): {tail[:300]}"

    return SandboxResult(exit_code, duration, timed_out, transcript, stderr_log,
                         infra_error=infra_error)
