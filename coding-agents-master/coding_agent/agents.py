"""Per-CLI invocation: how to launch each coding agent headless.

Both agents receive PROMPT.md on stdin and must emit machine-readable
transcripts on stdout (captured by the sandbox layer). Exact flags for model /
effort pinning are verified empirically at rung 0; agent.extra_args in the run
config exists so rung-0 corrections never require code changes.
"""
from .config import AgentConfig


def build_command(agent: AgentConfig) -> list[str]:
    if agent.cli == "claude":
        cmd = [
            "claude",
            "-p",  # print (headless) mode; prompt read from stdin
            "--output-format", "stream-json",
            "--verbose",
            "--model", agent.model,
            "--dangerously-skip-permissions",  # the container is the safety boundary
        ]
        # Effort pinning for claude is settings/env-dependent; pass via
        # extra_args/env once rung 0 establishes the working flag.
    elif agent.cli == "codex":
        cmd = [
            "codex", "exec",
            "--json",
            "--model", agent.model,
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",  # container is the boundary
        ]
        if agent.effort:
            cmd += ["-c", f"model_reasoning_effort={agent.effort}"]
        cmd += ["-"]  # read prompt from stdin
    else:
        raise ValueError(f"Unknown agent cli: {agent.cli}")
    return cmd + list(agent.extra_args)


def agent_env(agent: AgentConfig, api_key_env: str, api_key: str) -> dict:
    env = {
        api_key_env: api_key,
        # Reduce nonessential phoning-home where the CLIs support it.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }
    env.update(agent.env)
    return env
