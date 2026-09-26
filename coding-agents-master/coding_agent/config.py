"""Run configuration: one YAML file fully describes a run.

The config names its cohort with `agent_model_name`; the entry in
agent_identities.yaml supplies cli/model/effort/extra_args/env (see
agent_identity.py). `benchmark: v1|v2` selects the database, the S3 root and
the prompt-template default together.

Secrets are never stored in run configs:
  * DB URL and AWS creds come from <SpreadsheetSmith>/config/config.yaml
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
from urllib.parse import urlparse

import yaml

from . import repo_config
from .agent_identity import AgentIdentity, resolve_agent_identity

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"

# The trajectory relay runs from the repo's copy, bind-mounted read-only over
# the one baked into the image (sandbox.run_in_sandbox), so a relay fix never
# needs a new image tag: the tag is the recorded CLI pin, and v2 has to stay
# byte-identical for the Fable 5.1 cohort's banked rows. 2026-09-20: the baked
# relay answered a failed upstream connection with a 502 that never ended and
# hung Claude Code for 68 min. Rows record the mounted file's hash
# (extra_configs.relay); rows without that key ran the image's own relay.
RELAY_SOURCE = PACKAGE_DIR.parent / "docker" / "traj_relay.py"
RELAY_TARGET = "/usr/local/bin/traj_relay.py"

# Codex model catalog for model ids Codex does not know (the TensorBlock Forge
# cohorts). Codex gives an unknown id "fallback metadata" with a 272,000-token
# window it will not raise (model_context_window is capped at the model's
# max_context_window), so it compacts at ~245k however large the model's real
# window is. Each entry here reproduces Codex 0.155.1's fallback metadata
# exactly — same instructions, tools and request fields, checked request by
# request against the fallback on 2026-09-21 — and changes only
# context_window / max_context_window to the model's own limit. Mounted
# read-only when an identity names it in extra_args (-c model_catalog_json=...);
# rows record the file's hash (extra_configs.codex_model_catalog). Tied to the
# Codex version of image v3: regenerate it for another Codex version.
CODEX_CATALOG_SOURCE = PACKAGE_DIR.parent / "docker" / "codex_model_catalog.json"
CODEX_CATALOG_TARGET = "/etc/codex/model_catalog.json"

# Env var name, and the config/config.yaml keys.* fallback, per agent CLI.
AGENT_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
AGENT_KEY_CONFIG = {"claude": "anthropic_api_key", "codex": "openai_api_key"}

# A TensorBlock Forge identity (its env.TRAJ_UPSTREAM points the relay at the
# gateway) is keyed on its own and never falls back to the vendor key — that
# fallback would send the OpenAI key to TensorBlock. Same rule as the CLI
# harness. The key still enters the container under the CLI's usual name
# (OPENAI_API_KEY): `codex login` and the traj provider's env_key read it.
FORGE_KEY_ENV = "FORGE_API_KEY"
FORGE_KEY_CONFIG = "forge_api_key"

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
    image: str = "spreadsheetsmith-coding-agent:v2"
    network_allow: list[str] = field(default_factory=list)
    cpus: int = 4
    memory: str = "8g"


@dataclass
class LimitsConfig:
    wall_clock_seconds: int = 14400  # 4h
    junk_seconds: int = 180
    # 2026-09-25: when true, time the relay spends waiting out provider 429 / quota
    # refusals (its upstream_retries delays) does not count against the wall clock -
    # the agent still gets wall_clock_seconds of working time. Off by default; rows
    # record it in extra_configs.limits when on.
    exclude_provider_waits: bool = False


@dataclass
class TaskRange:
    """The inclusive task-id window a batch driver walks (`tasks:` in a run
    config). The driver applies the benchmark's own filters on top — task
    source, not deprecated, ascending id — exactly as the database query
    does; this only bounds the ids."""
    first: int = 1
    last: int = 101

    def __post_init__(self):
        if self.first < 1 or self.last < self.first:
            raise ValueError(f"tasks must be an ascending 1-based range, got {self.first}-{self.last}")

    def __contains__(self, task_id: int) -> bool:
        return self.first <= task_id <= self.last

    def __iter__(self):
        return iter(range(self.first, self.last + 1))

    def __str__(self) -> str:
        return f"{self.first}-{self.last}"


# Per-benchmark wiring. `benchmark` in the run config selects the experiment:
# the DB URL (config/config.yaml database.{v1,v2}_url), the S3 root under
# aws.s3_bucket, and the prompt-template default.
BENCHMARKS = {
    "v1": {"root": "BizbenchV1", "db_name": "BizbenchV1", "template": "v7"},
    "v2": {"root": "SpreadsheetSmith", "db_name": "SpreadsheetSmith", "template": "v13"},
}

# Per-benchmark wiring, offline half: where under local.data_root this
# benchmark's bundled inputs sit and where under local.output_root its runs
# write. None = not bundled — v2 is the set data/ ships (data/README.md);
# the v1 206-task inputs were never exported, so an offline v1 run is
# refused rather than silently reading an empty tree.
BENCHMARK_LOCAL_ROOTS = {
    "v1": {"data_root": None, "output_root": None},
    "v2": {"data_root": ".", "output_root": "."},
}
for _b, _roots in BENCHMARK_LOCAL_ROOTS.items():
    BENCHMARKS[_b].update(_roots)

# Where a task's definition comes from and where its attempt goes.
#   "cloud" — the benchmark database + object store (the production path)
#   "local" — the bundled data/ tree + outputs/ (the offline path)
# A run config may name either explicitly; "auto" (the default) picks local
# only when no database URL resolves for the benchmark. See resolve_io().
IO_CHOICES = ("auto", "local", "cloud")

TEMPLATE_VERSIONS = ("v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15")

# Files a template promises the agent, monorepo-root-relative. They are
# seeded into starting_files/ beside the task inputs, snapshotted with the
# prompt files and uploaded to the prompts prefix. Declared on the template
# — never in a run config — so the recorded prompt_version and the file the
# agent saw cannot disagree (<SpreadsheetSmith>/house_standards/README.md).
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
    template_version: str = "v7"  # v14/v15 = Stage 5 ablation re-cuts (2026-09-23): v14 = v9 text (rubric back, no house standards), v15 = v10 text (no house standards); v13 = rubric-free + house standards (v2 default, = v11 text); v12 = v9 + house standards (rubric-bearing, superseded); v10/v11 = rubric-scrubbed experiment (2026-09-08); v9 = v2 Questions-sheet mirror; v8 = v2-rubric mirror; v7 = GUI-pv9 mirror (v1 default); v6 = CLI adaptation; v5 = byte-exact CLI templates
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    workspaces_dir: Path = PACKAGE_DIR.parent / "workspaces"
    config_path: Path | None = None  # the YAML this was loaded from (copied into the attempt dir)
    # Offline wiring (internal mode only; see IO_CHOICES and resolve_io()).
    source: str = "auto"  # where the task comes from
    sink: str = "auto"    # where the attempt goes
    tasks: "TaskRange | None" = None  # the sweep range a batch driver walks
    # Filled by resolve_secrets() (internal mode only).
    db_url: str = ""
    db_source: str = "unresolved"
    io_source: str = "unresolved"  # what resolve_io() picked: "local" | "cloud"
    io_sink: str = "unresolved"
    io_reason: str = ""            # why, for the startup log

    @property
    def api_key_env(self) -> str:
        return AGENT_KEY_ENV[self.agent.cli]

    @property
    def allowed_domains(self) -> list[str]:
        # A Forge run reaches TensorBlock only: without the vendor's API in the
        # allowlist, nothing in the container can send the Forge key there.
        if is_forge(self.agent):
            host = urlparse(self.agent.env["TRAJ_UPSTREAM"]).hostname
            base = [host] if host else []
        else:
            base = DEFAULT_ALLOWED_DOMAINS[self.agent.cli]
        return list(dict.fromkeys(base + self.sandbox.network_allow))

    @property
    def s3_bucket(self) -> str:
        """The object-store bucket for the cloud path, from the config only.

        There is deliberately no default: a wrong guess would have a run
        upload attempts to somebody else's bucket, or fail halfway through
        with a 403. Only the cloud source/sink reads this, so an offline run
        never reaches it.
        """
        bucket = repo_config.repo_value("aws", "s3_bucket")
        if not bucket:
            raise ValueError(
                "no object store configured: set aws.s3_bucket in "
                "<repo>/config/config.yaml. Runs that read and write locally "
                "(source/sink local, run_configs/offline/) never need it."
            )
        return bucket

    @property
    def s3_root(self) -> str:
        return BENCHMARKS[self.benchmark]["root"]

    @property
    def offline_source(self) -> bool:
        """True once resolve_io() has put this run's inputs on the bundle."""
        return self.io_source == "local"

    @property
    def offline_sink(self) -> bool:
        """True once resolve_io() has put this run's output under outputs/."""
        return self.io_sink == "local"

    @property
    def local_data_root(self) -> Path:
        """The bundled inputs for this benchmark (local.data_root + preset)."""
        rel = BENCHMARKS[self.benchmark]["data_root"]
        if rel is None:
            raise ValueError(
                f"{self.benchmark} inputs are not bundled: data/ ships the v2 "
                f"101-task set only (data/README.md). Run benchmark={self.benchmark} "
                f"against the database and object store, or export its inputs first."
            )
        return (repo_config.data_root() / rel).resolve()

    @property
    def local_output_root(self) -> Path:
        """Where this benchmark's offline attempts are written."""
        rel = BENCHMARKS[self.benchmark]["output_root"]
        if rel is None:
            raise ValueError(
                f"{self.benchmark} inputs are not bundled, so there is no offline "
                f"output layout for it (data/README.md)."
            )
        return (repo_config.output_root() / rel).resolve()

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
        if relay and RELAY_SOURCE.is_file():
            out["relay"] = {"source": "docker/traj_relay.py",
                            "sha256": hashlib.sha256(RELAY_SOURCE.read_bytes()).hexdigest()}
        if self.limits.exclude_provider_waits:
            out["limits"] = {"wall_clock_seconds": self.limits.wall_clock_seconds,
                             "exclude_provider_waits": True}
        if uses_codex_catalog(self.agent) and CODEX_CATALOG_SOURCE.is_file():
            out["codex_model_catalog"] = {
                "source": "docker/codex_model_catalog.json",
                "sha256": hashlib.sha256(CODEX_CATALOG_SOURCE.read_bytes()).hexdigest()}
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
            f"template {cfg.template_version} needs {rels} from the SpreadsheetSmith "
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
            "benchmark is required (v1 = BizbenchV1 wave, v2 = SpreadsheetSmith task "
            "set): it selects the database, the S3 root and the prompt-template "
            "default together"
        )
    if benchmark not in BENCHMARKS:
        raise ValueError(
            f'benchmark must be one of {sorted(BENCHMARKS)} '
            f'(v1 = BizbenchV1 wave, v2 = SpreadsheetSmith task set)'
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
        source=str(raw.get("source", "auto")).lower(),
        sink=str(raw.get("sink", "auto")).lower(),
        tasks=TaskRange(**raw["tasks"]) if raw.get("tasks") else None,
        config_path=path.resolve(),
    )
    if raw.get("workspaces_dir"):
        cfg.workspaces_dir = Path(raw["workspaces_dir"]).expanduser()
    if cfg.template_version not in TEMPLATE_VERSIONS:
        raise ValueError(f"template_version must be one of {', '.join(TEMPLATE_VERSIONS)}")
    if cfg.sandbox.mode not in ("docker", "host"):
        raise ValueError('sandbox.mode must be "docker" or "host"')
    for key in ("source", "sink"):
        if getattr(cfg, key) not in IO_CHOICES:
            raise ValueError(f'{key} must be one of {", ".join(IO_CHOICES)}')
    return cfg


def is_forge(agent: AgentConfig) -> bool:
    """True when the identity sends its calls to TensorBlock Forge."""
    return "tensorblock" in agent.env.get("TRAJ_UPSTREAM", "").lower()


def uses_codex_catalog(agent: AgentConfig) -> bool:
    """True when the identity points Codex at the mounted model catalog."""
    return agent.cli == "codex" and any(CODEX_CATALOG_TARGET in a for a in agent.extra_args)


def resolve_api_key(cfg: RunConfig) -> str:
    """The agent's API key: environment first, then config/config.yaml keys.*.
    A Forge identity resolves the Forge key or nothing (see FORGE_KEY_ENV)."""
    if is_forge(cfg.agent):
        return (os.environ.get(FORGE_KEY_ENV)
                or repo_config.repo_value("keys", FORGE_KEY_CONFIG)
                or "")
    return (os.environ.get(cfg.api_key_env)
            or repo_config.repo_value("keys", AGENT_KEY_CONFIG[cfg.agent.cli])
            or "")


def resolve_io(cfg: RunConfig) -> None:
    """Decide where this run reads its task from and writes its attempt to,
    and fill cfg.io_source / cfg.io_sink / cfg.io_reason.

    An explicit `source:` / `sink:` in the run config always wins. "auto"
    (the default) goes local ONLY when the benchmark has no database URL at
    all — neither config/config.yaml database.<benchmark>_url nor
    DATABASE_URL. A configured URL therefore always keeps today's behaviour:
    no run silently stops recording because a credential went missing.
    """
    if cfg.mode != "internal":
        cfg.io_source = cfg.io_sink = "local"
        cfg.io_reason = "external mode: task folder in, results folder out"
        return

    url, url_source = repo_config.resolve_db_url(cfg.benchmark)
    auto = "cloud" if url else "local"
    cfg.io_source = auto if cfg.source == "auto" else cfg.source
    cfg.io_sink = auto if cfg.sink == "auto" else cfg.sink

    explicit = [f"{k}={v} (from the run config)"
                for k, v in (("source", cfg.source), ("sink", cfg.sink)) if v != "auto"]
    if cfg.source == "auto" or cfg.sink == "auto":
        explicit.append(
            f"auto -> {auto}: database.{cfg.benchmark}_url / $DATABASE_URL "
            + (f"resolved from {url_source}" if url else "resolved to nothing")
        )
    cfg.io_reason = "; ".join(explicit)

    # Fail before any work when the offline half was asked for and the
    # benchmark has no bundle (v1): the tree would simply be empty.
    if cfg.io_source == "local":
        cfg.local_data_root  # noqa: B018 — raises with the "not bundled" message
    if cfg.io_sink == "local":
        cfg.local_output_root  # noqa: B018


def resolve_secrets(cfg: RunConfig) -> str:
    """Fail fast on missing secrets, before any work is done.

    Returns the agent API key and, in internal mode, fills cfg.db_url /
    cfg.db_source from the benchmark-keyed ladder in repo_config.
    """
    forge = is_forge(cfg.agent)
    # Only the relay reads TRAJ_UPSTREAM. Without it the CLI would call its
    # vendor's API directly — carrying the Forge key.
    if forge and not (cfg.record_trajectory and cfg.sandbox.mode == "docker"):
        raise SystemExit(
            f"{cfg.agent_model_name} reaches TensorBlock Forge only through the "
            f"trajectory relay: it needs sandbox.mode docker and record_trajectory on"
        )
    api_key = resolve_api_key(cfg)
    if not api_key:
        if forge:
            raise SystemExit(
                f"Missing {FORGE_KEY_ENV}: set it in the environment, a .env next "
                f"to coding_agent/, or <SpreadsheetSmith>/config/config.yaml "
                f"keys.{FORGE_KEY_CONFIG} (a Forge identity never falls back to "
                f"{cfg.api_key_env})"
            )
        raise SystemExit(
            f"Missing {cfg.api_key_env}: set it in the environment, a .env next "
            f"to coding_agent/, or <SpreadsheetSmith>/config/config.yaml "
            f"keys.{AGENT_KEY_CONFIG[cfg.agent.cli]}"
        )
    resolve_io(cfg)
    if cfg.mode == "internal" and not (cfg.offline_source and cfg.offline_sink):
        cfg.db_url, cfg.db_source = repo_config.resolve_db_url(cfg.benchmark)
        if not cfg.db_url:
            raise SystemExit(
                f"No database URL for benchmark={cfg.benchmark}: set "
                f"database.{cfg.benchmark}_url in <SpreadsheetSmith>/config/config.yaml "
                f"(or DATABASE_URL on a standalone checkout)"
            )
        # The $DATABASE_URL fallback is benchmark-blind: a v1 config writing
        # to the SpreadsheetSmith DB (or vice versa) would record attempts against
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
