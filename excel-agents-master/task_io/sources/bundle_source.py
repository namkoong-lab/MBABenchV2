"""BundleTaskSource — the `tasks` table read from the offline bundle.

Reads <data_root>/tasks/task_id=<N>/task.json (one `tasks` row per file,
exported verbatim; see <repo>/data/README.md) and yields, per task, the
same TaskSpec the postgres_s3 source yields for that row: task_id and
task_name from the row, upload_files = the task's starting files in row
order (read in place from the bundle — byte-for-byte the objects the cloud
source downloads), and the same metadata keys (source_kind, db_task_id,
overrides, task_source).

The filters mirror the cloud source's WHERE clause and its ORDER BY id:
  * task_ids                — `id = ANY(...)`
  * skip_deprecated         — `deprecated IS NULL OR deprecated = FALSE`
  * task_sources            — `task_source = ANY(...)`
  * skip_already_attempted  — NOT EXISTS a row in
                              outputs/<agent_model_name>/task_attempts.jsonl
                              with this task_id, this agent_model_name, this
                              prompt_version, agent_failed = false and
                              deprecated = false (the DB join, against the
                              file the local sink appends to).

No database, no S3, no network.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator

from ..base import TaskSpec
from ..local_layout import (
    TASK_DIR_PREFIX,
    TASK_ROW_FILE,
    TASKS_DIR,
    attempts_file,
    data_path,
)

logger = logging.getLogger(__name__)

_TASK_DIR_RE = re.compile(rf"^{re.escape(TASK_DIR_PREFIX)}(\d+)$")

# Same column names as the cloud source's TaskSchema for `tasks`.
ID_COL = "id"
NAME_COL = "task_name"
FILES_COL = "task_starting_files"
SOURCE_COL = "task_source"
DEPRECATED_COL = "deprecated"


def _same_version(a: Any, b: Any) -> bool:
    """prompt_version equality across the int the config carries and
    whatever a JSON row recorded (int, or a numeric string)."""
    if a is None or b is None:
        return a is b
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


class BundleTaskSource:
    def __init__(
        self,
        *,
        data_root: Path | str,
        output_root: Path | str,
        agent_model_name: str,
        prompt_version: int | str | None,
        task_ids: list[int] | None = None,
        task_sources: list[str] | None = None,
        skip_deprecated: bool = True,
        skip_already_attempted: bool = True,
    ):
        self.data_root = Path(data_root)
        self.output_root = Path(output_root)
        self.tasks_dir = self.data_root / TASKS_DIR
        if not self.tasks_dir.is_dir():
            raise ValueError(
                f"bundle task source: {self.tasks_dir} is not a directory. The "
                f"offline bundle (data/tasks/task_id=<N>/task.json) is tracked "
                f"in the repository; set local.data_root in "
                f"<repo>/config/config.yaml or SPREADSHEETSMITH_DATA_ROOT if it lives "
                f"elsewhere."
            )
        self.agent_model_name = agent_model_name
        self.prompt_version = prompt_version
        self.task_ids = [int(t) for t in (task_ids or [])]
        self.task_sources = list(task_sources or [])
        self.skip_deprecated = skip_deprecated
        self.skip_already_attempted = skip_already_attempted

    # --- rows --------------------------------------------------------------

    def _task_dirs(self) -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        for entry in self.tasks_dir.iterdir():
            m = _TASK_DIR_RE.match(entry.name)
            if m and (entry / TASK_ROW_FILE).is_file():
                found.append((int(m.group(1)), entry / TASK_ROW_FILE))
        return sorted(found)  # ORDER BY id

    def _load_row(self, path: Path) -> dict:
        with open(path) as f:
            row = json.load(f)
        if not isinstance(row, dict) or ID_COL not in row:
            raise ValueError(f"bundle task source: {path} is not a tasks row")
        return row

    def _already_attempted(self) -> set[int]:
        """task_ids with a live, non-failed attempt by this agent at this
        prompt_version in the local outputs — the DB NOT EXISTS join."""
        path = attempts_file(self.output_root, self.agent_model_name)
        if not path.is_file():
            return set()
        done: set[int] = set()
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping unparsable line in {path}")
                    continue
                if (
                    row.get("agent_model_name") == self.agent_model_name
                    and _same_version(row.get("prompt_version"), self.prompt_version)
                    and row.get("agent_failed") is False
                    and row.get("deprecated") is False
                    and row.get("task_id") is not None
                ):
                    done.add(int(row["task_id"]))
        return done

    def _select_rows(self) -> list[dict]:
        wanted = set(self.task_ids)
        done = self._already_attempted() if self.skip_already_attempted else set()
        rows: list[dict] = []
        for tid, path in self._task_dirs():
            if wanted and tid not in wanted:
                continue
            row = self._load_row(path)
            if self.skip_deprecated and row.get(DEPRECATED_COL):
                continue
            if self.task_sources and row.get(SOURCE_COL) not in self.task_sources:
                continue
            if tid in done:
                continue
            rows.append(row)
        return rows

    # --- spec assembly (mirrors PostgresS3TaskSource) ----------------------

    def _metadata_for(self, row: dict) -> dict:
        return {
            "source_kind": "bundle",
            "db_task_id": row[ID_COL],
            "overrides": {},
            SOURCE_COL: row.get(SOURCE_COL),
        }

    def _starting_files(self, row: dict) -> list[Path]:
        resolved: list[Path] = []
        for rel in row.get(FILES_COL) or []:
            p = data_path(str(rel), self.data_root)
            if not p.is_file():
                raise FileNotFoundError(
                    f"bundle task source: task id={row[ID_COL]} names starting "
                    f"file {rel!r} but {p} is missing — run "
                    f"scripts/verify_offline_bundle.py"
                )
            resolved.append(p)
        return resolved

    # --- public API --------------------------------------------------------

    def iter_tasks(self) -> Iterator[TaskSpec]:
        rows = self._select_rows()
        logger.info(
            f"{type(self).__name__} matched {len(rows)} task(s) "
            f"(ids={self.task_ids or 'any'}) under {self.tasks_dir}"
        )
        for row in rows:
            tid = row[ID_COL]
            if not (row.get(FILES_COL) or []):
                logger.warning(
                    f"Task id={tid} name={row[NAME_COL]!r} has no "
                    f"{FILES_COL}; skipping."
                )
                continue
            yield TaskSpec(
                task_id=str(tid),
                task_name=row[NAME_COL],
                upload_files=self._starting_files(row),
                solution_name=None,
                metadata=self._metadata_for(row),
            )

    def close(self) -> None:
        return None
