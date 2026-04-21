"""Build TaskSource / AttemptSink instances from a loaded cfg namespace.

Both builders take the full `cfg` (not just the `source`/`sink` subtree) so
backends like `postgres_s3` can reach other top-level blocks (`database`,
`paths`, `agent`, `aws`) without re-plumbing arguments.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from .base import AttemptSink, TaskSource

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def _resolve_db_url(database_cfg: SimpleNamespace | None) -> str:
    if database_cfg is None:
        return ""
    direct = getattr(database_cfg, "url", "") or ""
    if direct:
        return direct
    env_name = getattr(database_cfg, "url_env", "") or ""
    if env_name:
        return os.environ.get(env_name, "") or ""
    return ""


def _resolve_from_value_or_env(
    cfg: SimpleNamespace | None, value_key: str, env_key: str
) -> str | None:
    """Direct `value_key` wins; otherwise read env var named by `env_key`.
    Returns None if neither yields a non-empty string, so boto3 can fall
    back to its default credential chain."""
    if cfg is None:
        return None
    direct = getattr(cfg, value_key, "") or ""
    if direct:
        return direct
    env_name = getattr(cfg, env_key, "") or ""
    if env_name:
        v = os.environ.get(env_name, "") or ""
        if v:
            return v
    return None


def build_source(cfg: SimpleNamespace) -> TaskSource:
    kind = cfg.source.kind
    if kind == "yaml":
        from .sources.yaml_source import YamlTaskSource
        return YamlTaskSource(yaml_path=_resolve(cfg.source.yaml_path))
    if kind == "postgres_s3":
        from .sources.postgres_s3 import BizbenchPostgresS3TaskSource

        db_url = _resolve_db_url(getattr(cfg, "database", None))
        scratch_dir = _resolve(cfg.paths.scratch_dir)
        filters = cfg.source.filters
        aws_cfg = getattr(cfg, "aws", None)
        region = getattr(aws_cfg, "region", None) if aws_cfg is not None else None
        access_key = _resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env"
        )
        secret_key = _resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env"
        )
        session_token = _resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        )
        return BizbenchPostgresS3TaskSource(
            db_url=db_url,
            scratch_dir=scratch_dir,
            agent_model_name=cfg.agent.model_name,
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
    raise ValueError(f"Unknown source.kind: {kind!r}")


def build_sink(cfg: SimpleNamespace) -> AttemptSink:
    kind = cfg.sink.kind
    if kind == "local":
        from .sinks.local_sink import LocalAttemptSink
        return LocalAttemptSink(output_dir=_resolve(cfg.sink.output_dir))
    if kind == "postgres_s3":
        raise NotImplementedError(
            "sink kind 'postgres_s3' is a Phase 2+ deliverable; see infra/plan.md"
        )
    raise ValueError(f"Unknown sink.kind: {kind!r}")
