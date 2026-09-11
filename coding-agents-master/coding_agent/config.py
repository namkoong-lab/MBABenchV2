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
import hashlib
import os
import re
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
    # The tag is the CLI-version pin recorded per attempt
    # (extra_configs.sandbox_image); retag any rebuild that changes contents.
    image: str = "mbabench-coding-agent:v2"
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
    "v2": {"root": "MBABenchV2", "db_name": "MBABenchV2", "template": "v13"},
}
DEFAULT_S3_BUCKET = "mbabench"

TEMPLATE_VERSIONS = ("v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13")

# Files a template promises the agent, monorepo-root-relative. They are
# seeded into starting_files/ beside the task inputs, snapshotted with the
# prompt files and uploaded to the prompts prefix. Declared on the template
# — never in a run config — so the recorded prompt_version and the file the
# agent saw cannot disagree (<MBABenchV2>/house_standards/README.md).
# v11/v13 deliver the standards differently — staged into the WORKSPACE ROOT
# as HOUSE_STANDARDS.md from prompts/house_standards_v1.md (a byte-identical
# copy of the canonical file, pinned by tests/test_smoke.py); see
# prompt_builder.TEMPLATE_EXTRAS. Both routes record house_standards below.
TEMPLATE_ATTACHMENTS = {
    "v12": ["house_standards/House_Standards_v1.md"],
}
_HOUSE_STANDARDS_RE = re.compile(r"^House_Standards_v(\d+)\.md$")


@dataclass
class RunConfig:
    agent_model_name: str  # cohort label (task_attempts.agent_model_name, S3 prefix)
    identity: AgentIdentity
    agent: AgentConfig
    mode: str  # "internal" | "external"
    benchmark: str = "v1"  # "v1" | "v2"; required in internal run configs (no default)
    record_trajectory: bool = True  # per-step API request/response capture (docker mode only)
    system_prompt: str = "system_prompt_coding_v1.txt"
    template_version: str = "v7"  # v13 = rubric-free + house standards (v2 default, = v11 text); v12 = v9 + house standards (rubric-bearing, superseded); v10/v11 = rubric-scrubbed experiment (2026-09-08); v9 = v2 Questions-sheet mirror; v8 = v2-rubric mirror; v7 = GUI-pv9 mirror (v1 default); v6 = CLI adaptation; v5 = byte-exact CLI templates
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
        settings, the sandbox image (it pins the CLI version), the harness
        defaults applied under the identity (agents.harness_defaults) and,
        when the template ships house standards, which text the agent saw."""
        out = {**self.identity.extra_configs(), "sandbox_image": self.sandbox.image}
        out.update(house_standards_provenance(self))
        from .agents import harness_defaults  # local: agents imports this module
        relay = self.record_trajectory and self.sandbox.mode == "docker"
        out["harness_defaults"] = harness_defaults(self.agent, relay)
        return out


def template_attachments(cfg: RunConfig) -> list[Path]:
    """Absolute paths of the template's declared attachments (empty for
    templates that declare none).

    Raises FileNotFoundError when the monorepo root or a file is missing: a
    template whose directive names a file the agent will never find must
    not run, and the caller treats this as infra_failure (no row).
    """
    rels = TEMPLATE_ATTACHMENTS.get(cfg.template_version, [])
    if not rels:
        return []
    root = repo_config.monorepo_root()
    if root is None:
        raise FileNotFoundError(
            f"template {cfg.template_version} needs {rels} from the MBABenchV2 "
            f"root, which could not be located (install the workspace, or "
            f"check out coding-agents-master inside the monorepo)"
        )
    paths = [root / rel for rel in rels]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"template {cfg.template_version} declares attachment(s) that do "
            f"not exist: {missing}"
        )
    return paths


def house_standards_provenance(cfg: RunConfig) -> dict:
    """{"house_standards": {version, file, sha256[, delivered_as]}} for the
    standards file the template delivers, else {}. The hash is taken at run
    time so the row records the text actually delivered, not the version the
    name claims. Two delivery routes: a declared attachment seeded into
    starting_files/ (v12), or a workspace-root extra (v11/v13 stage
    prompts/house_standards_v1.md as HOUSE_STANDARDS.md — `delivered_as`
    records that name; `file` keeps the versioned source name)."""
    for path in template_attachments(cfg):
        m = _HOUSE_STANDARDS_RE.match(path.name)
        if m:
            return {"house_standards": {
                "version": int(m.group(1)),
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }}
    from .prompt_builder import prompt_extra_paths  # local: prompt_builder imports this module
    for src, ws_name in prompt_extra_paths(cfg):
        m = _HOUSE_STANDARDS_RE.match(src.name.replace("house_standards_v", "House_Standards_v", 1))
        if m:
            return {"house_standards": {
                "version": int(m.group(1)),
                "file": f"House_Standards_v{m.group(1)}.md",
                "delivered_as": ws_name,
                "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
            }}
    return {}


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

    # Required for internal runs: a silent default would let a config that
    # omits the key record its rows against whichever benchmark the default
    # names. External mode has no DB/S3; v1 only sets the template default.
    if "benchmark" in raw:
        benchmark = str(raw["benchmark"]).lower()
    elif raw["mode"] == "external":
        benchmark = "v1"
    else:
        raise ValueError(
            "benchmark is required (v1 = BizbenchV1 wave, v2 = MBABenchV2 task "
            "set): it selects the database, the S3 root and the prompt-template "
            "default together"
        )
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
    if cfg.template_version not in TEMPLATE_VERSIONS:
        raise ValueError(f"template_version must be one of {', '.join(TEMPLATE_VERSIONS)}")
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
