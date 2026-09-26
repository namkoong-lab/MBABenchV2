"""LocalAttemptSink — the offline record of an attempt, no database, no S3.

Writes the layout <repo>/data/README.md fixes for `outputs/`:

    outputs/<agent_model_name>/task_attempts.jsonl
        one task_attempts-shaped row per attempt (every column of the DB
        row, built by the same `attempt_row` the postgres_s3 sink uses)
    outputs/<agent_model_name>/task_id=<N>/<YYYYmmdd_HHMMSS>/
        the artifact set the cloud sink uploads, in upload order: the
        solution workbook first, then the completion JSON(s) and .log, then
        the prompts JSON

The row's `attempt_files` / `prompt_files` are repo-relative POSIX paths
with the canonical `outputs/` prefix (task_io/local_layout.py maps it to
`local.output_root`), `id` is the millisecond epoch time of the write, and
timestamps are ISO-8601 with an offset. Skip-if-attempted in the bundle
source reads the same file this appends to.

Files are COPIED here, so the sink retains them and the runner may delete
its staging directory afterwards — exactly as with the cloud sink.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import AttemptResult
from ..local_layout import (
    OUTPUT_PREFIX,
    TASK_DIR_PREFIX,
    attempts_file,
    canonical_path,
)
from .attempt_row import TASK_ATTEMPTS_COLUMNS, attempt_row, db_task_id

logger = logging.getLogger(__name__)


def _with_offset(dt: datetime) -> datetime:
    """A naive datetime (the runner records local wall-clock time) gets the
    local offset attached; an aware one stands."""
    return dt.astimezone() if dt.tzinfo is None else dt


def _json_default(o: Any):
    if isinstance(o, datetime):
        return _with_offset(o).isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


class LocalAttemptSink:
    # Every file handed to publish() is copied under output_root before the
    # row is written, so the caller's staging copies are disposable.
    retains_files = True

    def __init__(
        self,
        *,
        output_root: str | Path,
        agent_model_name: str,
        agent_model_type: str = "excel",
        prompt_version: int | str | None,
        extra_configs: dict[str, Any] | None = None,
    ):
        if not agent_model_name:
            raise ValueError(
                "local sink: agent_model_name is required. It is derived from "
                "resolve_agent_identity(cfg) — an empty value means the "
                "resolver returned an invalid AgentIdentity."
            )
        self.output_root = Path(output_root)
        self.agent_model_name = agent_model_name
        self.agent_model_type = agent_model_type
        self.prompt_version = prompt_version
        self.extra_configs = dict(extra_configs or {})
        self.attempts_path = attempts_file(self.output_root, agent_model_name)
        self._last_id = 0
        logger.info(
            f"Sink: local -> {self.attempts_path.parent} "
            f"(rows in {self.attempts_path.name})"
        )

    # --- layout ------------------------------------------------------------

    def attempt_dir(self, task_id: int | str, timestamp: str) -> Path:
        return self.attempts_path.parent / f"{TASK_DIR_PREFIX}{task_id}" / timestamp

    def describe_destination(self, task_id: int | str, task_name: str = "") -> str:
        """One line for the dry run: where this task's files and row go."""
        folder = self.attempt_dir(task_id, "<YYYYmmdd_HHMMSS>")
        return (
            f"{canonical_path(self.output_root, OUTPUT_PREFIX, folder)}/ "
            f"(solution workbook, logs, prompts JSON) + one row appended to "
            f"{canonical_path(self.output_root, OUTPUT_PREFIX, self.attempts_path)}"
        )

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _existing(paths, what: str) -> list[Path]:
        out: list[Path] = []
        for p in paths:
            if p is None:
                continue
            p = Path(p)
            if not p.exists():
                logger.warning(f"Sink: skipping missing {what} {p}")
                continue
            out.append(p)
        return out

    def _attempt_files_to_copy(self, result: AttemptResult) -> list[Path]:
        # Solution workbook FIRST (the judged file), then every log.
        return self._existing((result.solution_file, *result.log_files), "file")

    def _prompt_files_to_copy(self, result: AttemptResult) -> list[Path]:
        return self._existing(result.prompt_files or [], "prompt file")

    def _copy_files(self, files: list[Path], dest_dir: Path) -> list[str]:
        recorded: list[str] = []
        for local in files:
            dest = dest_dir / local.name
            shutil.copy2(local, dest)
            recorded.append(canonical_path(self.output_root, OUTPUT_PREFIX, dest))
        return recorded

    def _next_id(self) -> int:
        row_id = int(time.time() * 1000)
        if row_id <= self._last_id:  # two publishes inside one millisecond
            row_id = self._last_id + 1
        self._last_id = row_id
        return row_id

    # --- public API --------------------------------------------------------

    def publish(self, result: AttemptResult) -> None:
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        dest_dir = self.attempt_dir(db_task_id(result), timestamp)
        suffix = 1
        while dest_dir.exists():  # same task, same second: never merge folders
            suffix += 1
            dest_dir = self.attempt_dir(db_task_id(result), f"{timestamp}_{suffix}")
        dest_dir.mkdir(parents=True, exist_ok=False)

        attempt_files = self._copy_files(self._attempt_files_to_copy(result), dest_dir)
        prompt_files = self._copy_files(self._prompt_files_to_copy(result), dest_dir)

        row = attempt_row(
            result, self, attempt_files=attempt_files, prompt_files=prompt_files
        )
        row["id"] = self._next_id()
        row["created_at"] = _with_offset(now)
        assert tuple(row) == TASK_ATTEMPTS_COLUMNS

        self.attempts_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.attempts_path, "a") as f:
            f.write(json.dumps(row, default=_json_default) + "\n")
        logger.info(
            f"Sink: recorded attempt id={row['id']} for task_id={row['task_id']} "
            f"status={result.status} attempt_files={len(attempt_files)} "
            f"prompt_files={len(prompt_files)} -> {dest_dir}"
        )

    def close(self) -> None:
        return None
