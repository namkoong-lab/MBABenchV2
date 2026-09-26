"""The offline attempt sink: `outputs/` instead of the database and the
object store.

`AutoBatchRunner.upload_result` normally uploads the attempt package to the
object store and inserts one `task_attempts` row. This module is the other
half of that pair: it copies the same file set under

    outputs/<agent_model_name>/task_id=<N>/<YYYYmmdd_HHMMSS>/

and appends one JSON object per attempt to

    outputs/<agent_model_name>/task_attempts.jsonl

carrying exactly the columns the database row has, in the table's own column
order, with the two file columns holding repo-relative paths into the folder
above. data/README.md is the contract; the judge and the paper scripts read
these rows the same way they read the bundled `data/results/*.jsonl`.

IDS. A local row's `id` is the millisecond epoch at which it was written:
unique across lanes running concurrently on one machine, monotonic, and far
above the small database ids in `data/results/`, so a local attempt can
never be confused with a recorded one.

RESUME. The same JSONL is what skip-if-attempted reads, so a relaunched lane
picks up where it stopped exactly as the database path does.
"""

import fcntl
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .repo_config import to_repo_relative

# The `task_attempts` columns, in the table's ordinal order. A local row has
# all of them and nothing else: a reader that accepts both sources must not
# have to care which one it got.
ATTEMPT_COLUMNS: Tuple[str, ...] = (
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

ATTEMPTS_JSONL = "task_attempts.jsonl"


def iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with an offset, the shape every timestamp in the bundle has.

    The runner builds its times with datetime.fromtimestamp(), which is
    naive local time; the database column is timestamptz, so the offset the
    row ends up carrying is this machine's. astimezone() on a naive value
    attaches exactly that, making the local row say out loud what the
    database row stores implicitly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


class LocalAttemptSink:
    """Writes one cohort's attempts under `outputs/<agent_model_name>/`."""

    def __init__(self, output_root: Path, agent_model_name: str):
        self.output_root = Path(output_root)
        self.agent_model_name = agent_model_name

    # -- locations -------------------------------------------------------

    @property
    def cohort_dir(self) -> Path:
        """`outputs/<label>` — a label with a slash nests one level, by design."""
        return self.output_root.joinpath(*self.agent_model_name.split("/"))

    @property
    def jsonl_path(self) -> Path:
        return self.cohort_dir / ATTEMPTS_JSONL

    def attempt_dir(self, task_id: int, timestamp: str) -> Path:
        return self.cohort_dir / f"task_id={task_id}" / timestamp

    # -- writing ---------------------------------------------------------

    @staticmethod
    def new_attempt_id() -> int:
        """Millisecond epoch — see the module docstring."""
        return int(time.time() * 1000)

    def store_files(self, files: Iterable[Tuple[Path, str]], task_id: int,
                    timestamp: str) -> List[str]:
        """Copy one attempt's artifacts in, return their repo-relative paths.

        `files` is the (source, relative destination) list the object-store
        path uploads, so both sinks record the same artifact set in the same
        order — solution.xlsx first, which is what makes attempt_files[0]
        the judged workbook.
        """
        dest_root = self.attempt_dir(task_id, timestamp)
        stored: List[str] = []
        for src, rel in files:
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            stored.append(to_repo_relative(dest))
        return stored

    def append_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Append one attempt row, filling the columns it did not set.

        The write is a single line under an exclusive lock on the file, so
        concurrent lanes appending to one cohort interleave whole rows
        rather than half-lines.
        """
        missing = set(row) - set(ATTEMPT_COLUMNS)
        if missing:
            raise ValueError(
                f"{sorted(missing)} are not task_attempts columns; a local "
                "row must carry the database's column set exactly"
            )
        complete = {col: row.get(col) for col in ATTEMPT_COLUMNS}
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(complete, ensure_ascii=False, default=str) + "\n"
        with open(self.jsonl_path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return complete

    # -- reading (skip-if-attempted / resume) -----------------------------

    def read_rows(self) -> List[Dict[str, Any]]:
        """Every row this cohort has recorded locally, oldest first.

        A truncated final line is skipped rather than fatal: a lane killed
        mid-write must not stop the relaunch that is meant to recover it.
        """
        path = self.jsonl_path
        if not path.is_file():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def trial_count(self, task_id: int, since: Optional[str] = None) -> int:
        """Non-deprecated attempts for this task since `since`, the local
        counterpart of the runner's trial-count query."""
        return sum(1 for r in self._live_rows(task_id, since))

    def has_success(self, task_id: int, since: Optional[str] = None) -> bool:
        """True if a non-failed, non-deprecated attempt exists — the query
        `skip_if_succeeded` makes."""
        return any(not r.get("agent_failed") for r in self._live_rows(task_id, since))

    def _live_rows(self, task_id: int, since: Optional[str]) -> List[Dict[str, Any]]:
        out = []
        for row in self.read_rows():
            if int(row.get("task_id") or -1) != int(task_id):
                continue
            if row.get("deprecated"):
                continue
            if since and str(row.get("created_at") or "") < str(since):
                continue
            out.append(row)
        return out
