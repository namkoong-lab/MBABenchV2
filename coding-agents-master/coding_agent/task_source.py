"""Task sources: where a task's definition and starting files come from.

InternalSource — a benchmark task (BizbenchV1 or MBABenchV2, per the run
config's `benchmark`): task row from Neon Postgres (`tasks` table,
read-only) and starting files from S3.

ExternalSource — a local task folder supplied by a third party:
    my_task/
      task.yaml            (task_name; task_source: fmwc|modeloff|wsp|jp)
      starting_files/      (the input files)
No database, S3, or MBABench credentials involved.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

from .repo_config import s3_client

TASK_SOURCES = ("fmwc", "modeloff", "wsp", "jp")  # jp = every v2 task


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
