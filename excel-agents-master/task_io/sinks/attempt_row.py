"""One `task_attempts` row from one AttemptResult — the code both sinks share.

The postgres_s3 sink INSERTs a subset of this row (the DB fills id /
created_at and takes extra_configs in a follow-up UPDATE); the local sink
writes the whole row as one JSON line. Building the row in one place is
what makes an offline run's `task_attempts.jsonl` column-for-column the
same record a cloud run would have produced in the database.

Nothing here touches a database, S3, or the filesystem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from ..base import AttemptResult

# Every column of task_attempts, in table order. A local row carries exactly
# these keys; the DB INSERT covers the subset the sink can supply.
TASK_ATTEMPTS_COLUMNS: tuple[str, ...] = (
    "id",
    "task_id",
    "agent_model_name",
    "agent_model_type",
    "attempt_files",
    "prompt_files",
    "start_time",
    "end_time",
    "time_taken_min",
    "cost",
    "prompt_version",
    "agent_failed",
    "agent_failed_reason",
    "deprecated",
    "created_at",
    "context_reduced",
    "deprecated_reason",
    "updated_at",
    "extra_configs",
)


class AttemptRowConfig(Protocol):
    """What a sink must know to label a row. Both sinks satisfy this with
    their own attributes, so they pass `self`."""

    agent_model_name: str
    agent_model_type: str
    prompt_version: int | str | None
    extra_configs: dict[str, Any]


def task_metadata(result: AttemptResult) -> dict:
    extra = result.extra or {}
    meta = extra.get("task_metadata")
    return meta if isinstance(meta, dict) else {}


def db_task_id(result: AttemptResult) -> int:
    """task_attempts.task_id (INT NOT NULL) for this result."""
    value = task_metadata(result).get("db_task_id")
    if value is not None:
        return int(value)
    try:
        return int(result.task_id)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"task_attempts sink: task_id must resolve to "
            f"an int, got {result.task_id!r}. Ensure the source "
            f"populates spec.metadata['db_task_id'] or yields numeric "
            f"task_ids (this sink writes to task_attempts.task_id "
            f"which is INT NOT NULL)."
        ) from e


def extra_configs_payload(
    result: AttemptResult, base: dict[str, Any] | None
) -> dict[str, Any]:
    """task_attempts.extra_configs: the identity settings (provider,
    ui_model_label, thinking_effort, agent_folder, agent_model_type) merged
    with the runner's per-attempt stamps (cdp_port, infra_tries,
    engine_task_status, house_standards)."""
    payload = dict(base or {})
    per_attempt = (result.extra or {}).get("extra_configs")
    if isinstance(per_attempt, dict):
        payload |= per_attempt
    return payload


def attempt_row(
    result: AttemptResult,
    cfg: AttemptRowConfig,
    *,
    attempt_files: list[str],
    prompt_files: list[str],
) -> dict[str, Any]:
    """The task_attempts row for `result`, keyed by column in table order.

    `attempt_files` / `prompt_files` are the published locations of the
    files (s3:// URIs for the cloud sink, repo-relative paths for the local
    one) in upload order: solution workbook first, then the logs; prompt
    JSON separately. Timestamps stay the datetimes the runner recorded
    (each sink serialises them). `id` and `created_at` are None: the
    database assigns them on INSERT and the local sink fills them in.
    """
    start_dt = datetime.fromisoformat(result.started_at)
    end_dt = datetime.fromisoformat(result.finished_at)
    time_taken_min = (result.duration_seconds or 0.0) / 60.0

    agent_failed = result.status != "success"
    agent_failed_reason: str | None = None
    if agent_failed:
        extra = result.extra or {}
        agent_failed_reason = (
            extra.get("error") or extra.get("failure_reason") or result.status
        )

    row = {
        "id": None,
        "task_id": db_task_id(result),
        "agent_model_name": cfg.agent_model_name,
        "agent_model_type": cfg.agent_model_type,
        "attempt_files": list(attempt_files),
        "prompt_files": list(prompt_files),
        "start_time": start_dt,
        "end_time": end_dt,
        "time_taken_min": time_taken_min,
        "cost": None,
        "prompt_version": cfg.prompt_version,
        "agent_failed": agent_failed,
        "agent_failed_reason": agent_failed_reason,
        "deprecated": False,
        "created_at": None,
        "context_reduced": None,
        "deprecated_reason": None,
        "updated_at": None,
        "extra_configs": extra_configs_payload(result, cfg.extra_configs),
    }
    assert tuple(row) == TASK_ATTEMPTS_COLUMNS
    return row
