"""The task_attempts row, built by ONE function for every sink.

`attempt_row` maps an AttemptResult to the columns the postgres_s3 sink
INSERTs. The local (offline) sink calls the same function and adds only the
columns the database fills in on insert (id, created_at, ...), so an
offline row and a database row have the same shape by construction rather
than by convention. Pure: no boto3, no psycopg2, nothing to configure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..base import AttemptResult

# The INSERT column list (identical in BizbenchV1 and SpreadsheetSmith).
TASK_ATTEMPTS_INSERT_COLUMNS: tuple[str, ...] = (
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
)

# Every column of the table, in table order: the INSERT columns plus what
# the database supplies on insert. This is the key set of an offline row
# (outputs/<agent_model_name>/task_attempts.jsonl, see <repo>/data/README.md)
# and of the bundled rows under data/results/.
TASK_ATTEMPTS_COLUMNS: tuple[str, ...] = (
    "id",
    *TASK_ATTEMPTS_INSERT_COLUMNS,
    "created_at",
    "context_reduced",
    "deprecated_reason",
    "updated_at",
    "extra_configs",
)

# Values the database assigns when the INSERT names none of these columns
# (the GUI sinks never do). Recorded verbatim in an offline row so the two
# read the same; `id` and `created_at` are set at write time.
TASK_ATTEMPTS_INSERT_DEFAULTS: dict[str, Any] = {
    "context_reduced": None,
    "deprecated_reason": None,
    "updated_at": None,
    "extra_configs": None,
}


def task_db_id(result: AttemptResult) -> int:
    """The tasks.id this attempt belongs to.

    The source records it in spec.metadata['db_task_id'], which the runner
    threads through as result.extra['task_metadata']; a numeric task_id is
    the fallback so a source that yields DB ids directly still works.
    """
    extra = result.extra or {}
    meta = extra.get("task_metadata")
    meta = meta if isinstance(meta, dict) else {}
    db_task_id = meta.get("db_task_id")
    if db_task_id is not None:
        return int(db_task_id)
    try:
        return int(result.task_id)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"task_attempts sink: task_id must resolve to an int, got "
            f"{result.task_id!r}. Ensure the source populates "
            f"spec.metadata['db_task_id'] or yields numeric task_ids (this "
            f"sink writes to task_attempts.task_id which is INT NOT NULL)."
        ) from e


def attempt_row(
    result: AttemptResult,
    *,
    agent_model_name: str,
    agent_model_type: str,
    prompt_version: int | str | None,
    attempt_files: list[str],
    prompt_files: list[str],
) -> dict[str, Any]:
    """The INSERT columns for one attempt, keyed by column name.

    `attempt_files` / `prompt_files` are whatever the sink stored — s3://
    URIs for the postgres_s3 sink, repo-relative paths for the local sink;
    the order is the sink's upload order (solution workbook first).
    start_time / end_time are datetimes, as psycopg2 wants them; the local
    sink serialises them to ISO-8601 with an offset. `cost` is always NULL
    (GUI runs are subscription-based) and `deprecated` always FALSE.
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
        "task_id": task_db_id(result),
        "agent_model_name": agent_model_name,
        "agent_model_type": agent_model_type,
        "attempt_files": list(attempt_files),
        "prompt_files": list(prompt_files),
        "start_time": start_dt,
        "end_time": end_dt,
        "time_taken_min": time_taken_min,
        "cost": None,
        "prompt_version": prompt_version,
        "agent_failed": agent_failed,
        "agent_failed_reason": agent_failed_reason,
        "deprecated": False,
    }
    assert tuple(row) == TASK_ATTEMPTS_INSERT_COLUMNS
    return row
