"""Task sources: where a task's definition and starting files come from.

InternalSource — the MBABench V1 benchmark: task row from Neon Postgres
(`tasks` table, read-only) and starting files from S3.

ExternalSource — a local task folder supplied by a third party:
    my_task/
      task.yaml            (task_name; task_source: fmwc|modeloff|wsp)
      starting_files/      (the input files)
No database, S3, or MBABench credentials involved.
"""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class TaskSpec:
    task_id: int | None  # None in external mode
    task_name: str
    task_source: str  # 'fmwc' | 'modeloff' | 'wsp'
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
        if source not in ("fmwc", "modeloff", "wsp"):
            raise ValueError(f"task_source must be fmwc|modeloff|wsp, got {source!r}")
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


class InternalSource:
    """Reads the `tasks` row (READ-ONLY) and downloads starting files from S3."""

    def __init__(self, task_id: int):
        self.task_id = task_id

    def fetch(self, staging_dir: Path) -> TaskSpec:
        import boto3
        import psycopg2

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
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

        s3 = boto3.client("s3")
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
