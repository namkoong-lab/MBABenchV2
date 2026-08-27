"""Run configuration: one YAML file fully describes a run.

The config names its cohort with `agent_model_name`; the entry in
agent_identities.yaml supplies cli/model/effort/extra_args/env (see
agent_identity.py). `benchmark: v1|v2` selects the database, the S3 root and
the prompt-template default together.

Secrets are never stored in run configs:
  * DB URL and AWS creds come from <MBABenchV2>/config/config.yaml
    (database.{v1,v2}_url, aws.*) — falling back to DATABASE_URL / boto3's
    default chain on a standalone checkout (see repo_config.py).
  * The agent's API key comes from the environment (or a local .env next to
    this package), falling back to config/config.yaml keys.*.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import repo_config
from .agent_identity import AgentIdentity, resolve_agent_identity

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"

# Env var name, and the config/config.yaml keys.* fallback, per agent CLI.
AGENT_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
AGENT_KEY_CONFIG = {"claude": "anthropic_api_key", "codex": "openai_api_key"}

# Egress allowlist per agent CLI: the model API only — CLI telemetry is
# disabled via env, and the firewall fails closed on unresolvable domains.
# Extend per-run via sandbox.network_allow if a CLI needs another endpoint.
DEFAULT_ALLOWED_DOMAINS = {
    "claude": ["api.anthropic.com"],
    "codex": ["api.openai.com"],
}

# Keys older run configs carried that now live elsewhere. Refused (not
# ignored) so a stale prod config gets migrated deliberately.
STALE_KEYS = {
    "identity": "renamed to agent_model_name (a label registered in agent_identities.yaml)",
    "agent": "pinned by the agent_model_name entry in agent_identities.yaml",
    "internal": "s3 bucket comes from config/config.yaml aws.s3_bucket; the root follows `benchmark`",
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

    @classmethod
    def from_identity(cls, identity: AgentIdentity) -> "AgentConfig":
        return cls(cli=identity.cli, model=identity.model, effort=identity.effort,
                   extra_args=list(identity.extra_args), env=dict(identity.env))


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


# Per-benchmark wiring. `benchmark` in the run config selects the experiment:
# the DB URL (config/config.yaml database.{v1,v2}_url), the S3 root under
# aws.s3_bucket, and the prompt-template default.
BENCHMARKS = {
    "v1": {"root": "BizbenchV1", "db_name": "BizbenchV1", "template": "v7"},
    "v2": {"root": "MBABenchV2", "db_name": "MBABenchV2", "template": "v9"},
}
DEFAULT_S3_BUCKET = "mbabench"


@dataclass
class RunConfig:
    agent_model_name: str  # cohort label (task_attempts.agent_model_name, S3 prefix)
    identity: AgentIdentity
    agent: AgentConfig
    mode: str  # "internal" | "external"
    benchmark: str = "v1"  # "v1" (BizbenchV1 wave) | "v2" (MBABenchV2 task set)
    record_trajectory: bool = True  # per-step API request/response capture (docker mode only)
    system_prompt: str = "system_prompt_coding_v1.txt"
    template_version: str = "v7"  # v9 = v2 Questions-sheet mirror (v2 default); v8 = v2-rubric mirror; v7 = GUI-pv9 mirror (v1 default); v6 = CLI adaptation; v5 = byte-exact CLI templates
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    workspaces_dir: Path = PACKAGE_DIR.parent / "workspaces"
    config_path: Path | None = None  # the YAML this was loaded from (copied into the attempt dir)
    # Filled by resolve_secrets() (internal mode only).
    db_url: str = ""
    db_source: str = "unresolved"

    @property
    def api_key_env(self) -> str:
        return AGENT_KEY_ENV[self.agent.cli]

    @property
    def allowed_domains(self) -> list[str]:
        return DEFAULT_ALLOWED_DOMAINS[self.agent.cli] + self.sandbox.network_allow

    @property
    def s3_bucket(self) -> str:
        return repo_config.repo_value("aws", "s3_bucket") or DEFAULT_S3_BUCKET

    @property
    def s3_root(self) -> str:
        return BENCHMARKS[self.benchmark]["root"]

    def extra_configs(self) -> dict:
        """What task_attempts.extra_configs records: the identity's pinned
        settings plus the sandbox image (it pins the CLI version)."""
        return {**self.identity.extra_configs(), "sandbox_image": self.sandbox.image}


def load_config(path: str | Path) -> RunConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    stale = [k for k in STALE_KEYS if k in raw]
    if stale:
        raise ValueError(
            "run config carries key(s) that no longer belong there:\n"
            + "\n".join(f"  - {k}: {STALE_KEYS[k]}" for k in stale)
        )
    if raw.get("mode") not in ("internal", "external"):
        raise ValueError('mode must be "internal" or "external"')

    benchmark = str(raw.get("benchmark", "v1")).lower()
    if benchmark not in BENCHMARKS:
        raise ValueError(
            f'benchmark must be one of {sorted(BENCHMARKS)} '
            f'(v1 = BizbenchV1 wave, v2 = MBABenchV2 task set)'
        )

    identity = resolve_agent_identity(raw)
    cfg = RunConfig(
        agent_model_name=identity.agent_model_name,
        identity=identity,
        agent=AgentConfig.from_identity(identity),
        mode=raw["mode"],
        benchmark=benchmark,
        record_trajectory=bool(raw.get("record_trajectory", True)),
        system_prompt=raw.get("system_prompt", "system_prompt_coding_v1.txt"),
        template_version=raw.get("template_version", BENCHMARKS[benchmark]["template"]),
        sandbox=SandboxConfig(**(raw.get("sandbox") or {})),
        limits=LimitsConfig(**(raw.get("limits") or {})),
        config_path=path.resolve(),
    )
    if raw.get("workspaces_dir"):
        cfg.workspaces_dir = Path(raw["workspaces_dir"]).expanduser()
    if cfg.template_version not in ("v5", "v6", "v7", "v8", "v9"):
        raise ValueError('template_version must be "v5", "v6", "v7", "v8", or "v9"')
    if cfg.sandbox.mode not in ("docker", "host"):
        raise ValueError('sandbox.mode must be "docker" or "host"')
    return cfg


def resolve_api_key(cfg: RunConfig) -> str:
    """The agent's API key: environment first, then config/config.yaml keys.*."""
    return (os.environ.get(cfg.api_key_env)
            or repo_config.repo_value("keys", AGENT_KEY_CONFIG[cfg.agent.cli])
            or "")


def resolve_secrets(cfg: RunConfig) -> str:
    """Fail fast on missing secrets, before any work is done.

    Returns the agent API key and, in internal mode, fills cfg.db_url /
    cfg.db_source from the benchmark-keyed ladder in repo_config.
    """
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise SystemExit(
            f"Missing {cfg.api_key_env}: set it in the environment, a .env next "
            f"to coding_agent/, or <MBABenchV2>/config/config.yaml "
            f"keys.{AGENT_KEY_CONFIG[cfg.agent.cli]}"
        )
    if cfg.mode == "internal":
        cfg.db_url, cfg.db_source = repo_config.resolve_db_url(cfg.benchmark)
        if not cfg.db_url:
            raise SystemExit(
                f"No database URL for benchmark={cfg.benchmark}: set "
                f"database.{cfg.benchmark}_url in <MBABenchV2>/config/config.yaml "
                f"(or DATABASE_URL on a standalone checkout)"
            )
        # The $DATABASE_URL fallback is benchmark-blind: a v1 config writing
        # to the MBABenchV2 DB (or vice versa) would record attempts against
        # the wrong experiment — refuse before any work runs.
        expected_db = BENCHMARKS[cfg.benchmark]["db_name"]
        if repo_config.database_name(cfg.db_url) != expected_db:
            raise SystemExit(
                f"benchmark={cfg.benchmark} expects the {expected_db} database, "
                f"but the URL from {cfg.db_source} points at "
                f"{repo_config.database_name(cfg.db_url)}. Fix the config's "
                f"benchmark key or the connection string."
            )
    return api_key
