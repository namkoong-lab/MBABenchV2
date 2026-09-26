"""BundleTaskSource — the offline `tasks` table (see <repo>/data/README.md).

Reads the rows the export wrote under <data_root>/tasks/task_id=<N>/task.json
and yields, per task, the TaskSpec `SpreadsheetSmithPostgresS3TaskSource` would
yield for the same row: same task_id / task_name, the starting files in
the same order under the same names (read in place — the bundle keeps the
object-store basenames, so nothing is copied), the same metadata keys, and
the same four filters applied in the same order as that source's WHERE
clause:

    task_ids               -> id = ANY(task_ids)
    skip_deprecated        -> deprecated IS NULL OR deprecated = FALSE
    task_sources           -> task_source = ANY(task_sources)
    skip_already_attempted -> NOT EXISTS a row in the local attempts log
                              (outputs/<agent_model_name>/task_attempts.jsonl)
                              for this task, agent_model_name and
                              prompt_version with agent_failed = FALSE and
                              deprecated = FALSE

ordered by id. Only `metadata["source_kind"]` differs ("bundle" rather than
"postgres_s3"); nothing downstream reads it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator

from ..base import TaskSpec

logger = logging.getLogger(__name__)

_BUNDLE_PREFIX = "data"


def _load_attempted_task_ids(
    attempts_log: Path | None, agent_model_name: str, prompt_version
) -> set[int]:
    """Task ids with a non-failed, non-deprecated row for this
    (agent_model_name, prompt_version) in the local attempts log — the
    anti-join the database source runs against task_attempts."""
    if attempts_log is None or not attempts_log.exists():
        return set()
    done: set[int] = set()
    with open(attempts_log) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"{attempts_log}:{lineno}: not JSON; ignored")
                continue
            if row.get("agent_model_name") != agent_model_name:
                continue
            if str(row.get("prompt_version")) != str(prompt_version):
                continue
            if row.get("agent_failed") or row.get("deprecated"):
                continue
            try:
                done.add(int(row["task_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    return done


class BundleTaskSource:
    def __init__(
        self,
        *,
        data_root: str | Path,
        repo_root: str | Path,
        agent_model_name: str,
        prompt_version: int | str | None,
        task_ids: list[int] | None = None,
        task_sources: list[str] | None = None,
        skip_deprecated: bool = True,
        skip_already_attempted: bool = True,
        attempts_log: str | Path | None = None,
    ):
        self.data_root = Path(data_root)
        self.repo_root = Path(repo_root)
        self.tasks_dir = self.data_root / "tasks"
        if not self.tasks_dir.is_dir():
            raise FileNotFoundError(
                f"bundle task source: {self.tasks_dir} does not exist. The "
                f"offline bundle (data/tasks/task_id=<N>/task.json + "
                f"starting_files/) must be present; set local.data_root in "
                f"<repo>/config/config.yaml or export SPREADSHEETSMITH_DATA_ROOT if it "
                f"lives elsewhere."
            )
        self.agent_model_name = agent_model_name
        self.prompt_version = prompt_version
        self.task_ids = [int(t) for t in (task_ids or [])]
        self.task_sources = list(task_sources or [])
        self.skip_deprecated = skip_deprecated
        self.skip_already_attempted = skip_already_attempted
        self.attempts_log = Path(attempts_log) if attempts_log else None

    # --- rows --------------------------------------------------------------

    def _load_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task_json in self.tasks_dir.glob("task_id=*/task.json"):
            with open(task_json) as f:
                row = json.load(f)
            if not isinstance(row, dict) or "id" not in row:
                logger.warning(f"{task_json}: not a tasks row; ignored")
                continue
            rows.append(row)
        rows.sort(key=lambda r: int(r["id"]))
        return rows

    def _select_rows(self) -> list[dict[str, Any]]:
        rows = self._load_rows()
        if self.task_ids:
            wanted = set(self.task_ids)
            rows = [r for r in rows if int(r["id"]) in wanted]
        if self.skip_deprecated:
            rows = [r for r in rows if not r.get("deprecated")]
        if self.task_sources:
            rows = [r for r in rows if r.get("task_source") in self.task_sources]
        if self.skip_already_attempted:
            done = _load_attempted_task_ids(
                self.attempts_log, self.agent_model_name, self.prompt_version
            )
            rows = [r for r in rows if int(r["id"]) not in done]
        return rows

    # --- files -------------------------------------------------------------

    def _resolve_path(self, rel: str) -> Path:
        """Bundle paths are repo-relative with a leading `data/`; when
        data_root is not that default, the leading segment is replaced."""
        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[0] == _BUNDLE_PREFIX:
            path = self.data_root / parts[1]
        else:
            path = self.repo_root / rel
        if not path.is_file():
            raise FileNotFoundError(
                f"bundle task source: starting file {rel!r} is missing at "
                f"{path}. The bundle under {self.data_root} is incomplete."
            )
        return path

    def _metadata_for(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_kind": "bundle",
            "db_task_id": int(row["id"]),
            "overrides": {},
            "task_source": row.get("task_source"),
        }

    # --- public API --------------------------------------------------------

    def iter_tasks(self) -> Iterator[TaskSpec]:
        rows = self._select_rows()
        logger.info(
            f"{type(self).__name__} matched {len(rows)} task(s) "
            f"(ids={self.task_ids or 'any'}) under {self.tasks_dir}"
        )
        for row in rows:
            tid = int(row["id"])
            files = row.get("task_starting_files") or []
            if not files:
                logger.warning(
                    f"Task id={tid} name={row.get('task_name')!r} has no "
                    f"task_starting_files; skipping."
                )
                continue
            yield TaskSpec(
                task_id=str(tid),
                task_name=row["task_name"],
                upload_files=[self._resolve_path(rel) for rel in files],
                solution_name=None,
                metadata=self._metadata_for(row),
            )

    def close(self) -> None:
        return None
