"""Build TaskSource / AttemptSink instances from a loaded cfg namespace.

Both builders take the full `cfg` (not just the `source`/`sink` subtree) so
backends like `postgres_s3` can reach other top-level blocks (`database`,
`paths`, `agent`, `aws`) without re-plumbing arguments.

CREDENTIAL PRECEDENCE (database url and AWS keys alike), first hit wins:

  1. an explicit value in infra/configs/configs.yaml — the escape hatch.
  2. the monorepo config at <repo>/config/config.yaml. The database url is
     selected there by `cfg.benchmark` (v1 -> database.v1_url, v2 ->
     database.v2_url), which is what makes "wrong benchmark, wrong DB"
     unrepresentable: the run config picks both or neither.
  3. the env var named by database.url_env / aws.*_env.

Layer 2 sits above the env vars deliberately: it is the only benchmark-aware
source. SPREADSHEETSMITHJUDGE_KEYS_DATABASE_URL is a single blind url that cannot
distinguish v1 from v2.

OFFLINE PATH (no database, no S3): `source.kind: bundle` reads the tasks
bundled under <repo>/data/tasks/ and `sink.kind: local` writes runs under
<repo>/outputs/ (data/README.md). Both roots come from the monorepo config's
`local.data_root` / `local.output_root` (env SPREADSHEETSMITH_DATA_ROOT /
SPREADSHEETSMITH_OUTPUT_ROOT win). An explicit kind always stands; a run config that
leaves the postgres_s3 defaults in place while no database url resolves is
switched to bundle + local by `apply_offline_fallback` (the runner logs it).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from infra.configs import resolve_agent_identity

from .base import AttemptSink, TaskSource
from .local_layout import (
    DATA_ROOT_ENV,
    DEFAULT_DATA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    OUTPUT_ROOT_ENV,
    resolve_root,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Points the monorepo-config lookup at a different directory. Used by the
# tests; also an escape hatch for layouts where the installed location is
# wrong. Unset -> Config resolves its own directory via the editable install.
_REPO_CONFIG_DIR_ENV = "SPREADSHEETSMITH_CONFIG_DIR"


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def _repo_value(*path: str) -> str | None:
    """Non-empty string at a path in <repo>/config/config.yaml, else None.

    `from config import Config` works because the root pyproject.toml exposes
    config/python/config.py as a top-level module, so an editable install
    resolves it from any cwd. This wrapper adds the three things that import
    does not give you:

    * It never raises. A standalone checkout has no config/ directory, so
      the import fails there — and that failure IS the signal to fall
      through to the env vars, not an error.
    * It never writes. Config.load() defaults to create_missing=True, which
      seeds a config.yaml from the defaults as a side effect of reading one.
    * A `null` placeholder (config_default.yaml ships `access_key_id: null`)
      yields None, so an unset key falls through to the next layer rather
      than resolving to something falsy-but-present.
    """
    try:
        from config import Config
    except ImportError:
        logger.debug("monorepo `config` module not installed; using env vars")
        return None

    override = os.environ.get(_REPO_CONFIG_DIR_ENV)
    # Config.load warns about every unset ${env:VAR} in the file, including
    # keys gui-agents never reads (gemini_api_key, ...). Not worth a warning
    # to someone starting a benchmark run.
    cfg_log = logging.getLogger("config")
    prev_level = cfg_log.level
    cfg_log.setLevel(logging.ERROR)
    try:
        data = Config.load(
            Path(override).expanduser() if override else None,
            create_missing=False,
            check_required=False,
        ).as_dict()
    except Exception as e:  # noqa: BLE001 — degrade, never break the caller
        logger.warning(
            "could not read the monorepo config (%s: %s); falling back to "
            "environment variables",
            type(e).__name__,
            e,
        )
        return None
    finally:
        cfg_log.setLevel(prev_level)

    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node.strip() if isinstance(node, str) and node.strip() else None


def _benchmark(cfg: SimpleNamespace) -> str:
    return (getattr(cfg, "benchmark", None) or "v2").lower()


def _resolve_db_url_with_source(cfg: SimpleNamespace) -> tuple[str, str]:
    """(url, human-readable provenance). See the module docstring for order."""
    database_cfg = getattr(cfg, "database", None)

    direct = (getattr(database_cfg, "url", "") or "") if database_cfg else ""
    if direct:
        return direct, "configs.yaml database.url"

    # The benchmark picks the database. This is the whole point of the
    # layering: a run config names one experiment and gets that experiment's
    # DB, with no second file to keep in sync.
    bench = _benchmark(cfg)
    repo_url = _repo_value("database", f"{bench}_url")
    if repo_url:
        return repo_url, f"config/config.yaml database.{bench}_url"

    env_name = (getattr(database_cfg, "url_env", "") or "") if database_cfg else ""
    if env_name:
        value = os.environ.get(env_name, "") or ""
        if value:
            return value, f"${env_name}"

    return "", "unresolved"


def _resolve_db_url(cfg: SimpleNamespace) -> str:
    return _resolve_db_url_with_source(cfg)[0]


def _database_name(url: str) -> str:
    """Database name out of a connection string, dropping every credential."""
    tail = url.split("://", 1)[-1].split("/", 1)
    if len(tail) < 2:
        return "?"
    return tail[1].split("?", 1)[0] or "?"


def describe_database_target(cfg: SimpleNamespace) -> str:
    """One safe log line naming the DB a run will read/write, and why.

    Never includes the password — a run.py log is routinely pasted into
    issues and chats.
    """
    url, source = _resolve_db_url_with_source(cfg)
    if not url:
        return f"unresolved ({source})"
    return f"{_database_name(url)} (from {source})"


def _resolve_from_value_or_env(
    cfg: SimpleNamespace | None,
    value_key: str,
    env_key: str,
    repo_section: str | None = None,
) -> str | None:
    """configs.yaml value -> monorepo config -> env var named by `env_key`.

    `repo_section` is the monorepo config block to look `value_key` up in;
    the key names match on both sides, so there is nothing to translate.
    Returns None if no layer yields a non-empty string, so boto3 can fall
    back to its default credential chain where the caller allows it.
    """
    if cfg is not None:
        direct = getattr(cfg, value_key, "") or ""
        if direct:
            return direct

    if repo_section:
        from_repo = _repo_value(repo_section, value_key)
        if from_repo:
            return from_repo

    if cfg is not None:
        env_name = getattr(cfg, env_key, "") or ""
        if env_name:
            v = os.environ.get(env_name, "") or ""
            if v:
                return v
    return None


# --- offline roots and the default-to-offline switch ------------------------


def local_data_root(cfg: SimpleNamespace | None = None) -> Path:
    """Where the bundled benchmark inputs live (data/README.md):
    $SPREADSHEETSMITH_DATA_ROOT -> <repo>/config/config.yaml local.data_root ->
    <repo>/data. Relative values resolve against the monorepo root."""
    value = os.environ.get(DATA_ROOT_ENV) or _repo_value("local", "data_root")
    return resolve_root(value, DEFAULT_DATA_ROOT)


def local_output_root(cfg: SimpleNamespace | None = None) -> Path:
    """Where offline runs are written: $SPREADSHEETSMITH_OUTPUT_ROOT ->
    <repo>/config/config.yaml local.output_root -> <repo>/outputs. An
    explicit, non-null sink.output_dir in the run config overrides both
    (resolved against the excel-agents-master root, like every other path
    in configs.yaml)."""
    sink_cfg = getattr(cfg, "sink", None) if cfg is not None else None
    explicit = getattr(sink_cfg, "output_dir", None) if sink_cfg else None
    if explicit:
        return _resolve(explicit)
    value = os.environ.get(OUTPUT_ROOT_ENV) or _repo_value("local", "output_root")
    return resolve_root(value, DEFAULT_OUTPUT_ROOT)


def _raw_kind(layer: Any, slot: str) -> str | None:
    """`<slot>.kind` as written in one raw config layer (a parsed YAML dict),
    accepting both the shorthand and the `{value: ...}` leaf form."""
    if not isinstance(layer, dict):
        return None
    block = layer.get(slot)
    if not isinstance(block, dict) or "kind" not in block:
        return None
    kind = block["kind"]
    if isinstance(kind, dict):
        kind = kind.get("value")
    return str(kind) if kind else None


def explicit_io_kinds(*layers: Any) -> tuple[str | None, str | None]:
    """(source.kind, sink.kind) as EXPLICITLY written in the override layers
    (configs.yaml, then the run config; later wins). None = left at the
    configs.default.yaml default. The loader collapses the layers, so this
    is the only way to tell "chose postgres_s3" from "never said"."""
    source_kind = sink_kind = None
    for layer in layers:
        source_kind = _raw_kind(layer, "source") or source_kind
        sink_kind = _raw_kind(layer, "sink") or sink_kind
    return source_kind, sink_kind


def apply_offline_fallback(
    cfg: SimpleNamespace,
    explicit_source_kind: str | None = None,
    explicit_sink_kind: str | None = None,
) -> str | None:
    """Switch a run left at the postgres_s3 defaults to bundle + local when
    no database url resolves for its benchmark.

    An explicit `source.kind` / `sink.kind` always wins (a run config that
    asked for postgres_s3 still fails with the credential message). When a
    url resolves nothing changes. Returns the one log line describing the
    switch, or None when nothing was switched.
    """
    if cfg.source.kind != "postgres_s3" and cfg.sink.kind != "postgres_s3":
        return None
    url, _where = _resolve_db_url_with_source(cfg)
    if url:
        return None
    switched: list[str] = []
    if cfg.source.kind == "postgres_s3" and explicit_source_kind is None:
        cfg.source.kind = "bundle"
        cfg.source.schema = None
        switched.append("source.kind -> bundle (tasks from data/tasks/)")
    if cfg.sink.kind == "postgres_s3" and explicit_sink_kind is None:
        cfg.sink.kind = "local"
        cfg.sink.schema = None
        switched.append("sink.kind -> local (attempts under outputs/)")
    if not switched:
        return None
    bench = _benchmark(cfg)
    return (
        f"No database url resolves for benchmark {bench} (config/config.yaml "
        f"database.{bench}_url unset): running offline — {'; '.join(switched)}"
    )


# Valid (kind, schema) combinations. `None` schema means the kind doesn't
# use the schema slot (an explicit schema will raise). Kinds that need a
# schema enumerate every accepted value — listing them here lets the error
# messages on typos point at real alternatives rather than a dump of
# unrelated backends.
_VALID_SOURCE_SCHEMAS: dict[str, set[str | None]] = {
    "yaml": {None},
    "bundle": {None},
    "postgres_s3": {"bizbench", "spreadsheetsmith"},
}
_VALID_SINK_SCHEMAS: dict[str, set[str | None]] = {
    "local": {None},
    "postgres_s3": {"bizbench", "spreadsheetsmith"},
}

# Which postgres_s3 schema each benchmark uses, and the S3 defaults that go
# with it. A mismatched (benchmark, schema) pair — e.g. benchmark v1 writing
# through the spreadsheetsmith sink — would silently record attempts against the
# wrong experiment's DB/bucket, so it is refused at build time.
_BENCHMARK_SCHEMAS = {"v1": "bizbench", "v2": "spreadsheetsmith"}
# Only the prefix is schema-derived. The bucket is named once, in
# <repo>/config/config.yaml aws.s3_bucket (or per run in sink.aws.s3_bucket):
# a built-in name would have a run upload a wave's attempts to whatever
# bucket happened to carry that name.
_SCHEMA_S3_PREFIXES = {
    "bizbench": "BizbenchV1/attempts",
    "spreadsheetsmith": "SpreadsheetSmith/attempts",
}


def _check_benchmark_schema(cfg: SimpleNamespace, slot: str, schema: str) -> None:
    bench = (getattr(cfg, "benchmark", None) or "v2").lower()
    expected = _BENCHMARK_SCHEMAS.get(bench)
    if expected is not None and schema != expected:
        raise ValueError(
            f"{slot}.schema={schema!r} contradicts benchmark={bench!r} "
            f"(expected {expected!r}). A v1 run must use the bizbench "
            f"schema (BizbenchV1 DB) and a v2 run the spreadsheetsmith schema "
            f"(SpreadsheetSmith DB) — a mismatch records attempts against the "
            f"wrong experiment."
        )


def _validate_kind_schema(
    kind_name: str,  # "source.kind" / "sink.kind"
    kind: str,
    schema: str | None,
    valid: dict[str, set[str | None]],
) -> None:
    """Enforce that (kind, schema) is a recognized combination.

    Three failure modes, each with an actionable message:
    1. Unknown kind entirely → list the kinds we know about.
    2. Kind doesn't take a schema but one was set → name the slot.
    3. Kind requires a schema but the given one is unknown/missing →
       list the schemas this kind accepts.
    """
    if kind not in valid:
        raise ValueError(
            f"Unknown {kind_name}: {kind!r}. " f"Available: {sorted(valid.keys())}"
        )
    accepted = valid[kind]
    if accepted == {None}:
        if schema not in (None, ""):
            slot = kind_name.replace(".kind", ".schema")
            raise ValueError(
                f"{slot} is not applicable when {kind_name}={kind!r}; "
                f"got schema={schema!r}. Omit it or set to null."
            )
        return
    if schema in (None, ""):
        slot = kind_name.replace(".kind", ".schema")
        non_none = sorted(s for s in accepted if s is not None)
        raise ValueError(
            f"{slot} is required when {kind_name}={kind!r}. " f"Available: {non_none}"
        )
    if schema not in accepted:
        slot = kind_name.replace(".kind", ".schema")
        non_none = sorted(s for s in accepted if s is not None)
        raise ValueError(
            f"Unknown {slot} {schema!r} for {kind_name}={kind!r}. "
            f"Available: {non_none}"
        )


def build_source(cfg: SimpleNamespace) -> TaskSource:
    kind = cfg.source.kind
    schema = getattr(cfg.source, "schema", None)
    _validate_kind_schema("source.kind", kind, schema, _VALID_SOURCE_SCHEMAS)

    if kind == "yaml":
        from .sources.yaml_source import YamlTaskSource

        return YamlTaskSource(yaml_path=_resolve(cfg.source.yaml_path))

    if kind == "bundle":
        from .sources.bundle_source import BundleTaskSource

        # The bundle is the SpreadsheetSmith (v2) task set; there is no v1 bundle.
        bench = _benchmark(cfg)
        if bench != "v2":
            raise ValueError(
                f"source.kind='bundle' holds the SpreadsheetSmith task set and "
                f"needs benchmark=v2, got benchmark={bench!r}."
            )
        filters = getattr(cfg.source, "filters", None)
        identity = resolve_agent_identity(cfg)
        return BundleTaskSource(
            data_root=local_data_root(cfg),
            output_root=local_output_root(cfg),
            agent_model_name=identity.model_name,
            prompt_version=cfg.agent.prompt_version,
            task_ids=list(getattr(filters, "task_ids", []) or []),
            task_sources=list(getattr(filters, "task_sources", []) or []),
            skip_deprecated=bool(getattr(filters, "skip_deprecated", True)),
            skip_already_attempted=bool(
                getattr(filters, "skip_already_attempted", True)
            ),
        )

    if kind == "postgres_s3" and schema in ("bizbench", "spreadsheetsmith"):
        from .sources.postgres_s3 import (
            BizbenchPostgresS3TaskSource,
            SpreadsheetSmithPostgresS3TaskSource,
        )

        _check_benchmark_schema(cfg, "source", schema)
        source_cls = (
            BizbenchPostgresS3TaskSource
            if schema == "bizbench"
            else SpreadsheetSmithPostgresS3TaskSource
        )
        db_url = _resolve_db_url(cfg)
        scratch_dir = _resolve(cfg.paths.scratch_dir)
        filters = cfg.source.filters
        aws_cfg = getattr(cfg, "aws", None)
        region = getattr(aws_cfg, "region", None) if aws_cfg is not None else None
        access_key = _resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env", "aws"
        )
        secret_key = _resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env", "aws"
        )
        # No session_token in the monorepo config — env-only by design.
        session_token = _resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        )
        identity = resolve_agent_identity(cfg)
        return source_cls(
            db_url=db_url,
            scratch_dir=scratch_dir,
            agent_model_name=identity.model_name,
            prompt_version=cfg.agent.prompt_version,
            task_ids=list(getattr(filters, "task_ids", []) or []),
            task_sources=list(getattr(filters, "task_sources", []) or []),
            skip_deprecated=bool(getattr(filters, "skip_deprecated", True)),
            skip_already_attempted=bool(
                getattr(filters, "skip_already_attempted", True)
            ),
            aws_region=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    # _validate_kind_schema already covered every rejection path; reaching
    # here means the dispatch table is out of sync with the if-ladder.
    raise AssertionError(
        f"Unhandled (source.kind, source.schema) = ({kind!r}, {schema!r})"
    )


def build_sink(cfg: SimpleNamespace) -> AttemptSink:
    kind = cfg.sink.kind
    schema = getattr(cfg.sink, "schema", None)
    _validate_kind_schema("sink.kind", kind, schema, _VALID_SINK_SCHEMAS)

    if kind == "local":
        from .sinks.local_sink import LocalAttemptSink

        identity = resolve_agent_identity(cfg)
        return LocalAttemptSink(
            output_root=local_output_root(cfg),
            agent_model_name=identity.model_name,
            agent_model_type=identity.agent_model_type,
            prompt_version=cfg.agent.prompt_version,
            extra_configs=identity.settings(),
        )

    if kind == "postgres_s3" and schema in ("bizbench", "spreadsheetsmith"):
        from .sinks.postgres_s3 import (
            BizbenchPostgresS3AttemptSink,
            SpreadsheetSmithPostgresS3AttemptSink,
        )

        _check_benchmark_schema(cfg, "sink", schema)
        sink_cls = (
            BizbenchPostgresS3AttemptSink
            if schema == "bizbench"
            else SpreadsheetSmithPostgresS3AttemptSink
        )
        db_url = _resolve_db_url(cfg)
        aws_cfg = getattr(cfg, "aws", None)
        region = getattr(aws_cfg, "region", None) if aws_cfg is not None else None
        access_key = _resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env", "aws"
        )
        secret_key = _resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env", "aws"
        )
        # No session_token in the monorepo config — env-only by design.
        session_token = _resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        )
        default_prefix = _SCHEMA_S3_PREFIXES[schema]
        # The prefix stays schema-derived — it encodes which experiment's
        # attempts these are, so it must never come from a credentials file.
        s3_bucket = (
            getattr(aws_cfg, "s3_bucket", None)
            or _repo_value("aws", "s3_bucket")
        )
        if not s3_bucket:
            raise ValueError(
                "sink.kind=postgres_s3 needs an object store: set "
                "aws.s3_bucket in <repo>/config/config.yaml, or "
                "sink.aws.s3_bucket in the run config. Runs that read and "
                "write locally (source.kind: bundle, sink.kind: local) never "
                "need it."
            )
        s3_prefix = getattr(aws_cfg, "s3_prefix", None) or default_prefix
        # Local mirror of everything uploaded, laid out under the same key
        # path as in the bucket. paths.output_dir is the run-wide output
        # root (sink.output_dir is the local sink's own ndjson location and
        # only applies when sink.kind=local). Empty/null disables mirroring.
        paths_cfg = getattr(cfg, "paths", None)
        mirror_raw = getattr(paths_cfg, "output_dir", "") if paths_cfg else ""
        mirror_dir = _resolve(mirror_raw) if mirror_raw else None
        identity = resolve_agent_identity(cfg)
        return sink_cls(
            db_url=db_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            mirror_dir=mirror_dir,
            agent_folder=identity.agent_folder,
            agent_model_name=identity.model_name,
            agent_model_type=identity.agent_model_type,
            extra_configs=identity.settings(),
            prompt_version=cfg.agent.prompt_version,
            aws_region=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    raise AssertionError(f"Unhandled (sink.kind, sink.schema) = ({kind!r}, {schema!r})")
