"""The offline task source: the bundle under `data/` instead of the database.

`AutoBatchRunner` normally resolves its tasks by querying the `tasks` table
and downloading `task_starting_files` from the object store. This module is
the other half of that pair: it reads the same rows out of
`data/tasks/task_id=<N>/task.json` and points at the file copies that sit
beside them, so a reviewer with neither a database nor object-store
credentials runs the identical experiment (data/README.md is the contract
for the layout).

WHAT THIS DOES NOT CHANGE. The selection rules, the order, the task object
handed to the runner, and therefore the prompt the agent sees, are the
database path's. `LocalTask` carries every column the ORM `Task` model maps,
under the same names, so `_resolve_tasks_impl` can treat both alike; the two
file columns hold absolute local paths in place of `s3://` URIs, and the
original keys stay under `s3_keys` for provenance.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .repo_config import resolve_bundled_path

# The per-task folder name inside the bundle; also the glob that enumerates
# them. Hive-style, matching the object store's own task_id=<N> partitions.
TASK_DIR_GLOB = "task_id=*"
TASK_JSON = "task.json"


@dataclass
class LocalTask:
    """One bundled `tasks` row, shaped like excel_cli_agent.db.models.Task.

    Only the columns the runner reads are promoted to attributes; everything
    else in the row stays in `row` so nothing is lost on the way through
    (the bundle carries human_difficulty_measure, case_classification and
    the AI time estimates too).
    """

    id: int
    task_name: str
    task_source: str
    task_starting_files: List[str] = field(default_factory=list)
    task_solution_files: List[str] = field(default_factory=list)
    deprecated: bool = False
    deprecated_reason: Optional[str] = None
    row: Dict[str, Any] = field(default_factory=dict)

    @property
    def s3_keys(self) -> Dict[str, Any]:
        """The object-store keys the bundled copies were made from."""
        return dict(self.row.get("s3_keys") or {})


class BundleNotAvailable(RuntimeError):
    """The offline source was asked for inputs that are not in the repo."""


class LocalTaskSource:
    """Read-only view of `data/tasks/`, standing in for the `tasks` table."""

    def __init__(self, data_root: Path, benchmark: str = "v2", bundled: bool = True):
        self.data_root = Path(data_root)
        self.benchmark = benchmark
        self._bundled = bundled
        self._tasks: Optional[List[LocalTask]] = None

    # -- loading ---------------------------------------------------------

    @property
    def tasks_dir(self) -> Path:
        return self.data_root / "tasks"

    def _require_bundle(self) -> None:
        if not self._bundled:
            raise BundleNotAvailable(
                f"benchmark={self.benchmark} inputs are not bundled: only the "
                "v2 task set ships with the repository (data/README.md). Run "
                f"the {self.benchmark} wave against its database and object "
                "store, or drop its bundle under the configured data_root and "
                "mark the benchmark bundled."
            )
        if not self.tasks_dir.is_dir():
            raise BundleNotAvailable(
                f"No task bundle at {self.tasks_dir}. Point local.data_root "
                "in config/config.yaml (or SPREADSHEETSMITH_DATA_ROOT) at the "
                "unpacked bundle, or fetch it with "
                "scripts/export_benchmark_data.py."
            )

    def load_all(self) -> List[LocalTask]:
        """Every bundled task, ordered by id.

        Ordered explicitly rather than by directory-listing order: a batch
        that re-runs the same range on another machine must claim the tasks
        in the same sequence, and `sorted()` on a filesystem glob is
        lexicographic (task_id=10 before task_id=2).
        """
        if self._tasks is not None:
            return self._tasks
        self._require_bundle()
        tasks: List[LocalTask] = []
        for task_dir in self.tasks_dir.glob(TASK_DIR_GLOB):
            manifest = task_dir / TASK_JSON
            if not manifest.is_file():
                continue
            tasks.append(self._load_task(manifest))
        if not tasks:
            raise BundleNotAvailable(
                f"{self.tasks_dir} holds no {TASK_DIR_GLOB}/{TASK_JSON}; the "
                "bundle is empty or only partly unpacked. Verify it with "
                "scripts/verify_offline_bundle.py."
            )
        self._tasks = sorted(tasks, key=lambda t: t.id)
        return self._tasks

    def _load_task(self, manifest: Path) -> LocalTask:
        row = json.loads(manifest.read_text(encoding="utf-8"))
        return LocalTask(
            id=int(row["id"]),
            task_name=row.get("task_name") or "",
            task_source=row.get("task_source") or "",
            task_starting_files=self._resolve_files(row, "task_starting_files", manifest),
            task_solution_files=self._resolve_files(row, "task_solution_files", manifest),
            deprecated=bool(row.get("deprecated") or False),
            deprecated_reason=row.get("deprecated_reason"),
            row=row,
        )

    def _resolve_files(self, row: Dict[str, Any], column: str, manifest: Path) -> List[str]:
        """Absolute local paths for one file column of a bundled row.

        Every path in the bundle is repo-relative, so it survives the
        checkout moving; resolve_bundled_path also honours a relocated
        data_root. A registered file that is not on disk is fatal here
        rather than at workspace setup: an attempt run without its starting
        workbook is the empty-context defect, and the bundle is the only
        place it can come from offline.
        """
        out: List[str] = []
        for rel in (row.get(column) or []):
            path = resolve_bundled_path(rel)
            if not path.is_file() or path.stat().st_size == 0:
                raise BundleNotAvailable(
                    f"{manifest.parent.name}: {column} entry {rel} is missing "
                    f"or empty at {path}. The attempt-files archive is a "
                    "separate download for results only — task inputs are "
                    "tracked; re-check the bundle with "
                    "scripts/verify_offline_bundle.py."
                )
            out.append(str(path))
        return out

    # -- the queries the runner makes ------------------------------------

    def get(self, task_id: int) -> Optional[LocalTask]:
        """The row with this id, or None — `db.query(Task).filter(id==...)`."""
        for task in self.load_all():
            if task.id == int(task_id):
                return task
        return None

    def find_by_name(self, name: str) -> Optional[LocalTask]:
        """Name lookup with the database path's fallbacks, in its order:
        the normalized name among live tasks, the exact name among live
        tasks, then either spelling including deprecated rows."""
        normalized = name.replace(" ", "_")
        live = [t for t in self.load_all() if not t.deprecated]
        if normalized != name:
            for task in live:
                if task.task_name == normalized:
                    return task
        for task in live:
            if task.task_name == name:
                return task
        for task in self.load_all():
            if task.task_name == name:
                return task
        if normalized != name:
            for task in self.load_all():
                if task.task_name == normalized:
                    return task
        return None

    def filter(self, task_source: Optional[str] = None) -> List[LocalTask]:
        """Live tasks, optionally one source — the auto-discovery query."""
        out = [t for t in self.load_all() if not t.deprecated]
        if task_source:
            out = [t for t in out if t.task_source == task_source]
        return out
