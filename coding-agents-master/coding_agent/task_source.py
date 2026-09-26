"""Task sources: where a task's definition and starting files come from.

InternalSource — a benchmark task (BizbenchV1 or SpreadsheetSmith, per the run
config's `benchmark`): task row from Neon Postgres (`tasks` table,
read-only) and starting files from S3.

LocalSource — the same benchmark task, read from the bundle checked into the
repository instead (data/README.md):
    <data_root>/tasks/task_id=<N>/task.json         the `tasks` row verbatim
    <data_root>/tasks/task_id=<N>/starting_files/   the same files S3 holds
It stages the same names in the same order, so the workspace, the PROMPT.md
listing and the seeded-file manifest are byte-identical to the S3 path's.
No database, object store or credentials involved.

ExternalSource — a local task folder supplied by a third party:
    my_task/
      task.yaml            (task_name; task_source: fmwc|modeloff|wsp|v2)
      starting_files/      (the input files)
No database, S3, or SpreadsheetSmith credentials involved.
"""
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .repo_config import bundled_path, s3_client

TASK_SOURCES = ("fmwc", "modeloff", "wsp", "v2")  # v2 = every v2 task


@dataclass
class TaskSpec:
    task_id: int | None  # None in external mode
    task_name: str
    task_source: str  # one of TASK_SOURCES
    starting_files: list  # list[Path] once staged locally


class ExternalSource:
    def __init__(self, task_dir: str | Path):
        self.task_dir = Path(task_dir).resolve()

    def fetch(self, staging_dir: Path) -> TaskSpec:
        meta_path = self.task_dir / "task.yaml"
        if not meta_path.exists():
            raise FileNotFoundError(f"External task needs {meta_path}")
        meta = yaml.safe_load(meta_path.read_text())
        source = meta.get("task_source", "fmwc")
        if source not in TASK_SOURCES:
            raise ValueError(f"task_source must be one of {'|'.join(TASK_SOURCES)}, got {source!r}")
        files_dir = self.task_dir / "starting_files"
        files = sorted(p for p in files_dir.iterdir() if p.is_file()) if files_dir.exists() else []
        if not files:
            raise FileNotFoundError(f"No files in {files_dir}")
        return TaskSpec(
            task_id=None,
            task_name=str(meta.get("task_name") or self.task_dir.name),
            task_source=source,
            starting_files=files,
        )


def verify_s3_access(bucket: str) -> None:
    """Raise unless the bucket answers with the resolved AWS credentials.

    Credentials may come from config/config.yaml or ~/.aws rather than env
    vars, so presence checks prove nothing — only a real call does. The
    recorder needs S3 again at the end of a multi-hour attempt; a run that
    would fail there must not start.
    """
    s3_client().head_bucket(Bucket=bucket)


class InternalSource:
    """Reads the `tasks` row (READ-ONLY) and downloads starting files from S3."""

    def __init__(self, task_id: int, db_url: str):
        self.task_id = task_id
        self.db_url = db_url

    def fetch(self, staging_dir: Path) -> TaskSpec:
        import psycopg2

        conn = psycopg2.connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_name, task_source, task_starting_files, deprecated "
                    "FROM tasks WHERE id = %s",
                    (self.task_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            raise LookupError(f"No task with id {self.task_id}")
        task_name, task_source, starting_uris, deprecated = row
        if deprecated:
            raise ValueError(f"Task {self.task_id} is deprecated — refusing to run")
        if not starting_uris:
            raise ValueError(f"Task {self.task_id} has no task_starting_files")

        s3 = s3_client()
        staging_dir.mkdir(parents=True, exist_ok=True)
        local_files = []
        for uri in starting_uris:
            if not uri.startswith("s3://"):
                raise ValueError(f"Unexpected starting-file URI: {uri}")
            bucket, _, key = uri[len("s3://"):].partition("/")
            dest = staging_dir / Path(key).name
            s3.download_file(bucket, key, str(dest))
            if dest.stat().st_size == 0:
                raise IOError(f"Downloaded empty file: {uri}")
            local_files.append(dest)
        return TaskSpec(
            task_id=self.task_id,
            task_name=task_name or f"task_{self.task_id}",
            task_source=task_source,
            starting_files=local_files,
        )


# The bundle mirrors the `tasks` table, so a listing applies the same filters
# the database query does: this task source, never a deprecated row,
# ascending id.
def task_dir(data_root: Path, task_id: int) -> Path:
    return Path(data_root) / "tasks" / f"task_id={task_id}"


def load_task_row(data_root: Path, task_id: int) -> dict:
    """The bundled `tasks` row for one task, verbatim."""
    path = task_dir(data_root, task_id) / "task.json"
    if not path.exists():
        raise LookupError(
            f"No bundled task with id {task_id} ({path} does not exist). The "
            f"benchmark inputs live under local.data_root; see data/README.md."
        )
    return json.loads(path.read_text())


def local_task_ids(data_root: Path, task_source: str = "v2",
                   first: int | None = None, last: int | None = None) -> list[int]:
    """Bundled task ids, ascending — the offline form of

        SELECT id FROM tasks
        WHERE task_source = %s AND NOT deprecated
          AND id BETWEEN %s AND %s
        ORDER BY id

    Reads tasks/tasks.json (the whole table, one read) when the bundle ships
    it, else the per-task task.json files."""
    root = Path(data_root)
    index = root / "tasks" / "tasks.json"
    if index.exists():
        rows = json.loads(index.read_text())
    else:
        rows = [json.loads(p.read_text())
                for p in sorted((root / "tasks").glob("task_id=*/task.json"))]
    ids = [
        int(r["id"]) for r in rows
        if r.get("task_source") == task_source
        and not r.get("deprecated")
        and (first is None or int(r["id"]) >= first)
        and (last is None or int(r["id"]) <= last)
    ]
    return sorted(ids)


class LocalSource:
    """Reads the bundled `tasks` row and stages its starting files."""

    def __init__(self, task_id: int, data_root: Path):
        self.task_id = task_id
        self.data_root = Path(data_root)

    def fetch(self, staging_dir: Path) -> TaskSpec:
        row = load_task_row(self.data_root, self.task_id)
        if row.get("deprecated"):
            raise ValueError(f"Task {self.task_id} is deprecated — refusing to run")
        starting = row.get("task_starting_files") or []
        if not starting:
            raise ValueError(f"Task {self.task_id} has no task_starting_files")

        staging_dir.mkdir(parents=True, exist_ok=True)
        local_files = []
        # Same iteration order as the S3 path: the order the column records.
        for rel in starting:
            if str(rel).startswith("s3://"):
                raise ValueError(
                    f"Bundled task {self.task_id} still carries an object-store URI "
                    f"({rel}); the exporter rewrites these to repo-relative paths "
                    f"(data/README.md)"
                )
            src = bundled_path(str(rel))
            if not src.is_file():
                raise FileNotFoundError(
                    f"Task {self.task_id} lists {rel}, which is missing from the "
                    f"bundle at {src}"
                )
            dest = staging_dir / Path(rel).name
            dest.write_bytes(src.read_bytes())
            if dest.stat().st_size == 0:
                raise IOError(f"Bundled file is empty: {src}")
            local_files.append(dest)
        return TaskSpec(
            task_id=self.task_id,
            task_name=row.get("task_name") or f"task_{self.task_id}",
            task_source=row.get("task_source"),
            starting_files=local_files,
        )
