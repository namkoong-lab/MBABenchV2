"""Per-CLI invocation: how to launch each coding agent headless.

Both agents receive PROMPT.md on stdin and must emit machine-readable
transcripts on stdout (captured by the sandbox layer). Exact flags for model /
effort pinning are verified empirically at rung 0; agent.extra_args in the run
config exists so rung-0 corrections never require code changes.
"""
from .config import AgentConfig


def build_command(agent: AgentConfig, relay: bool = False) -> list[str]:
    if agent.cli == "claude":
        cmd = [
            "claude",
            "-p",  # print (headless) mode; prompt read from stdin
            "--output-format", "stream-json",
            "--verbose",
            "--model", agent.model,
            "--dangerously-skip-permissions",  # the container is the safety boundary
        ]
        if agent.effort:
            cmd += ["--effort", agent.effort]  # verified in Claude Code 2.1.223 (low|medium|high|xhigh|max)
    elif agent.cli == "codex":
        import shlex
        args = [
            "codex", "exec",
            "--json",
            "--model", agent.model,
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",  # container is the boundary
        ]
        if agent.effort:
            args += ["-c", f"model_reasoning_effort={agent.effort}"]
        if relay:
            # codex ignores OPENAI_BASE_URL; route through the trajectory relay
            # via a custom provider (env_key keeps API-key billing).
            args += ["-c", "model_providers.traj.name=traj-relay",
                     "-c", 'model_providers.traj.base_url="http://127.0.0.1:9877/v1"',
                     "-c", "model_providers.traj.wire_api=responses",
                     "-c", "model_providers.traj.env_key=OPENAI_API_KEY",
                     "-c", "model_provider=traj"]
        args += list(agent.extra_args) + ["-"]  # read prompt from stdin
        # codex 0.146 does not read OPENAI_API_KEY implicitly: an explicit
        # `codex login --with-api-key` (stdin) must store it first. The login
        # pipeline consumes only its own pipe; the exec'd codex inherits the
        # outer stdin (PROMPT.md). auth.json lives in the container's
        # ephemeral HOME and dies with it.
        cmd = ["bash", "-c",
               "printenv OPENAI_API_KEY | codex login --with-api-key >/dev/null 2>&1 && exec "
               + shlex.join(args)]
        return cmd
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
