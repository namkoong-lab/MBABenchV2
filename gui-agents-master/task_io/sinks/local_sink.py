"""LocalAttemptSink — the offline attempt record (see <repo>/data/README.md).

Writes exactly what the postgres_s3 sink uploads, where the offline layout
contract puts it:

    <output_root>/<agent_model_name>/task_id=<N>/<YYYYmmdd_HHMMSS>/<file>
        the solution workbook FIRST (the judged file), then every log the
        attempt produced, then the prompts JSON
    <output_root>/<agent_model_name>/task_attempts.jsonl
        one task_attempts-shaped row per attempt, built by the same
        `attempt_row` the postgres_s3 sink uses for its INSERT

`id` is the millisecond epoch time of the write; file paths are
repo-relative POSIX paths; timestamps are ISO-8601 with an offset. Every
file is copied, so the runner may delete the staging directory afterwards
(retains_files = True). The pre-contract `attempts.ndjson` line
(asdict(AttemptResult)) is no longer written.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import AttemptResult
from .attempt_row import (
    TASK_ATTEMPTS_COLUMNS,
    TASK_ATTEMPTS_INSERT_DEFAULTS,
    attempt_row,
    task_db_id,
)

logger = logging.getLogger(__name__)

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def _iso(dt: datetime) -> str:
    """ISO-8601 with an offset; a naive datetime is taken as local time."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def _json_default(o: Any):
    if isinstance(o, datetime):
        return _iso(o)
    if isinstance(o, Path):
        return o.as_posix()
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
        agent_model_type: str = "gui",
        prompt_version: int | str | None,
        repo_root: str | Path,
        run_id: str | None = None,
    ):
        if not agent_model_name:
            raise ValueError(
                "local sink: agent_model_name is required. It is derived from "
                "resolve_agent_identity(cfg) — an empty value means the "
                "resolver returned an invalid AgentIdentity."
            )
        self.output_root = Path(output_root)
        self.repo_root = Path(repo_root).resolve()
        self.agent_model_name = agent_model_name
        self.agent_model_type = agent_model_type
        self.prompt_version = prompt_version
        # Same role as the postgres_s3 sink's run_id: disambiguates two lanes
        # publishing the same task in the same second on one machine.
        self.run_id = run_id or uuid.uuid4().hex[:8]

        # A label such as openpyxl_anthropic/claude-x nests one level; Path
        # joining does that for free.
        self.cohort_dir = self.output_root / agent_model_name
        self.log_path = self.cohort_dir / "task_attempts.jsonl"
        try:
            self.cohort_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a"):
                pass
        except OSError as e:
            raise ValueError(
                f"local sink: cannot write under {self.cohort_dir} "
                f"({type(e).__name__}: {e}). Set local.output_root in "
                f"<repo>/config/config.yaml or export SPREADSHEETSMITH_OUTPUT_ROOT."
            ) from e
        logger.info(
            f"Sink: run_id={self.run_id} -> {self._display(self.cohort_dir)}/ "
            f"(rows appended to {self._display(self.log_path)})"
        )

    # --- paths -------------------------------------------------------------

    def _record_path(self, p: Path) -> str:
        """Repo-relative POSIX path (the contract); absolute only when the
        output root lives outside the repository."""
        p = p.resolve()
        try:
            return p.relative_to(self.repo_root).as_posix()
        except ValueError:
            return p.as_posix()

    def _display(self, p: Path) -> str:
        return self._record_path(p)

    def attempt_dir(self, task_id: int, timestamp: str) -> Path:
        return self.cohort_dir / f"task_id={task_id}" / timestamp

    def describe_destination(self, task_id: int) -> str:
        """One line for --dry-run: where this task's attempt would land."""
        folder = self.cohort_dir / f"task_id={task_id}" / "<YYYYmmdd_HHMMSS>"
        return (
            f"{self._display(folder)}/ (artifact set) + one row appended to "
            f"{self._display(self.log_path)}"
        )

    # --- file selection (same rule and order as the postgres_s3 sink) -------

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

    def _attempt_files(self, result: AttemptResult) -> list[Path]:
        return self._existing((result.solution_file, *result.log_files), "file")

    def _prompt_files(self, result: AttemptResult) -> list[Path]:
        return self._existing(result.prompt_files or [], "prompt file")

    def _copy_all(self, files: list[Path], dest_dir: Path) -> list[str]:
        recorded: list[str] = []
        for local in files:
            dest = dest_dir / local.name
            shutil.copy2(local, dest)
            recorded.append(self._record_path(dest))
        return recorded

    # --- public API --------------------------------------------------------

    def publish(self, result: AttemptResult) -> None:
        task_id = task_db_id(result)
        timestamp = datetime.now().strftime(_TIMESTAMP_FMT)
        dest_dir = self.attempt_dir(task_id, timestamp)
        if dest_dir.exists():
            # Another lane published this task in the same second.
            dest_dir = dest_dir.with_name(f"{timestamp}_{self.run_id}")
        dest_dir.mkdir(parents=True, exist_ok=False)

        attempt_paths = self._copy_all(self._attempt_files(result), dest_dir)
        prompt_paths = self._copy_all(self._prompt_files(result), dest_dir)

        row = attempt_row(
            result,
            agent_model_name=self.agent_model_name,
            agent_model_type=self.agent_model_type,
            prompt_version=self.prompt_version,
            attempt_files=attempt_paths,
            prompt_files=prompt_paths,
        )
        full = {
            "id": int(time.time() * 1000),
            **row,
            "created_at": datetime.now().astimezone(),
            **TASK_ATTEMPTS_INSERT_DEFAULTS,
        }
        ordered = {c: full[c] for c in TASK_ATTEMPTS_COLUMNS}
        assert set(ordered) == set(full), set(full) ^ set(ordered)
        line = json.dumps(ordered, default=_json_default)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")
            f.flush()
        logger.info(
            f"Sink: recorded attempt id={ordered['id']} for task_id={task_id} "
            f"status={result.status} attempt_files={len(attempt_paths)} "
            f"prompt_files={len(prompt_paths)} -> {self._display(dest_dir)}/"
        )

    def close(self) -> None:
        return None
