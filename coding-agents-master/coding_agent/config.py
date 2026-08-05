"""Run configuration: one YAML file fully describes a run.

Secrets are never stored in configs — they come from the environment (or a
local .env next to this package, loaded if present). Required env vars depend
on the mode:
  internal mode: DATABASE_URL, AWS creds (env or ~/.aws), + the agent's API key
  external mode: only the agent's API key
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"

AGENT_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}

# Egress allowlist per agent CLI: the model API plus the CLI's own required
# service endpoints. Extend per-run via sandbox.network_allow.
DEFAULT_ALLOWED_DOMAINS = {
    "claude": ["api.anthropic.com", "statsig.anthropic.com", "sentry.io"],
    "codex": ["api.openai.com", "chatgpt.com"],
}


def load_dotenv_if_present(path: Path | None = None) -> None:
    """Tiny .env loader (KEY=VALUE lines); never overrides existing env."""
    env_path = path or (PACKAGE_DIR.parent / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class AgentConfig:
    cli: str  # "claude" | "codex"
    model: str
    effort: str | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxConfig:
    mode: str = "docker"  # "docker" | "host" (host = UNSANDBOXED, dev/rung-0 only)
    image: str = "mbabench-coding-agent:v1"
    network_allow: list[str] = field(default_factory=list)
    cpus: int = 4
    memory: str = "8g"


@dataclass
class LimitsConfig:
    wall_clock_seconds: int = 14400  # 4h
    junk_seconds: int = 180


@dataclass
class InternalConfig:
    s3_bucket: str = "mbabench"
    s3_root: str = "BizbenchV1"


@dataclass
class RunConfig:
    run_name: str
    agent: AgentConfig
    identity: str
    mode: str  # "internal" | "external"
    system_prompt: str = "system_prompt_coding_v1.txt"
    template_version: str = "v6"  # "v5" = byte-exact CLI-wave templates; "v6" = coding-agent adaptation
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    internal: InternalConfig = field(default_factory=InternalConfig)
    workspaces_dir: Path = PACKAGE_DIR.parent / "workspaces"

    @property
    def api_key_env(self) -> str:
        return AGENT_KEY_ENV[self.agent.cli]

    @property
    def allowed_domains(self) -> list[str]:
        return DEFAULT_ALLOWED_DOMAINS[self.agent.cli] + self.sandbox.network_allow


def load_config(path: str | Path) -> RunConfig:
    raw = yaml.safe_load(Path(path).read_text())
    agent_raw = raw.get("agent") or {}
    if agent_raw.get("cli") not in AGENT_KEY_ENV:
        raise ValueError(f"agent.cli must be one of {sorted(AGENT_KEY_ENV)}")
    if raw.get("mode") not in ("internal", "external"):
        raise ValueError('mode must be "internal" or "external"')

    cfg = RunConfig(
        run_name=raw["run_name"],
        agent=AgentConfig(
            cli=agent_raw["cli"],
            model=agent_raw["model"],
            effort=agent_raw.get("effort"),
            extra_args=list(agent_raw.get("extra_args") or []),
            env=dict(agent_raw.get("env") or {}),
        ),
        identity=raw["identity"],
        mode=raw["mode"],
        system_prompt=raw.get("system_prompt", "system_prompt_coding_v1.txt"),
        template_version=raw.get("template_version", "v6"),
        sandbox=SandboxConfig(**(raw.get("sandbox") or {})),
        limits=LimitsConfig(**(raw.get("limits") or {})),
        internal=InternalConfig(**(raw.get("internal") or {})),
    )
    if raw.get("workspaces_dir"):
        cfg.workspaces_dir = Path(raw["workspaces_dir"]).expanduser()
    if cfg.template_version not in ("v5", "v6"):
        raise ValueError('template_version must be "v5" or "v6"')
    if cfg.sandbox.mode not in ("docker", "host"):
        raise ValueError('sandbox.mode must be "docker" or "host"')
    return cfg


def require_env(cfg: RunConfig) -> None:
    """Fail fast on missing secrets, before any work is done."""
    missing = []
    if not os.environ.get(cfg.api_key_env):
        missing.append(cfg.api_key_env)
    if cfg.mode == "internal" and not os.environ.get("DATABASE_URL"):
        missing.append("DATABASE_URL")
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)} "
            f"(set them in the environment or a .env next to coding_agent/)"
        )
