"""Source / sink abstractions for gui-agents.

The engine pipeline is unchanged. These two protocols are the only seam a
swappable backend needs to implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass
class TaskSpec:
    """What the engine needs to run one task. Source-agnostic."""

    task_id: str
    task_name: str
    upload_files: list[Path]
    solution_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttemptResult:
    """What the engine produces. The sink decides how to persist it."""

    task_id: str
    task_name: str
    agent_model_name: str
    prompt_version: int | str | None
    status: str  # "success" | "failed" | "timeout"
    solution_file: Path | None
    # Every log the attempt produced: the completion JSON(s), the chat
    # transcript, and the runtime .log. Uploaded alongside the workbook so an
    # attempt can be diagnosed from stored files alone.
    log_files: list[Path]
    started_at: str  # ISO-8601
    finished_at: str
    duration_seconds: float
    prompt_files: list[Path] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# Credential-failure messages, shared by the postgres_s3 source and sink so
# both name the same file. These used to offer to scaffold null entries into
# infra/configs/configs.yaml; that file no longer holds secrets, and the
# prompt would have recreated it. Fail with directions instead.
_MISSING_DB_URL_MSG = (
    "postgres_s3 {what}: no database url. Set database.v1_url / database.v2_url "
    "in <repo>/config/config.yaml (the run's `benchmark` picks which one), or "
    "export the env var named by database.url_env."
)

_MISSING_AWS_MSG = (
    "postgres_s3 {what}: no AWS credentials. Set aws.access_key_id / "
    "aws.secret_access_key in <repo>/config/config.yaml, or export the env "
    "vars named by aws.access_key_id_env / aws.secret_access_key_env. The "
    "boto3 default credential chain (~/.aws/credentials, IAM role, etc.) is "
    "intentionally NOT consulted — a benchmark run must not silently pick up "
    "whichever identity happens to be ambient."
)


@runtime_checkable
class TaskSource(Protocol):
    def iter_tasks(self) -> Iterator[TaskSpec]: ...
    def close(self) -> None: ...


@runtime_checkable
class AttemptSink(Protocol):
    # True when publish() copies every file it is given somewhere durable, so
    # the caller may delete the originals. False for sinks that only record
    # paths — deleting would destroy the only copy.
    retains_files: bool

    def publish(self, result: AttemptResult) -> None: ...
    def close(self) -> None: ...
