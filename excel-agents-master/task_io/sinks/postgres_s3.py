"""Postgres + S3 attempt sink.

Symmetric with `task_io/sources/postgres_s3.py`:

* `PostgresS3AttemptSink` — generic. Uploads an AttemptResult's files to
  S3 and inserts one row into an attempts table. Knows only about one
  schema (AttemptSchema) and three hooks subclasses override:
      _s3_base_key(result, timestamp)  -> str
      _file_key(base_key, local)       -> str
      _attempt_values(result, uris)    -> dict[col -> value]

* `_TaskAttemptsPostgresS3Sink` — shared wiring for both benchmark DBs:
  the `task_attempts` schema (identical in BizbenchV1 and MBABenchV2),
  credential validation, and row assembly. `cost` is always NULL (add-in
  runs are subscription-based); failed/timeout runs are still inserted
  with `agent_failed=true`. When the connected DB has the JSONB
  `extra_configs` column (MBABenchV2 only — never mapped in an ORM model,
  see the repo convention), the resolved identity settings plus runtime
  stamps are written to it with raw SQL after the insert.

* `BizbenchPostgresS3AttemptSink` — benchmark v1 (BizbenchV1 DB). Keeps
  the Hive-style S3 layout used by cli-agents' `auto_batch_runner.py`:
  `{prefix}/{agent_folder}/task_source={src}/task_id={id}/{ts}_{name}`.

* `MBABenchV2PostgresS3AttemptSink` — benchmark v2 (MBABenchV2 DB).
  Task-name-based S3 layout, one folder per attempt:
  `{prefix}/{agent_folder}/{task_name}/{ts}_{run_id}/{name}`.

An attempt's files are its solution workbook, every log it produced
(`AttemptResult.log_files`), and the prompts JSON. All of them are
uploaded, and each is also copied to a local mirror directory
(`mirror_dir`, wired from `paths.output_dir`) under the SAME relative
path as its S3 key, so the folder on disk can be diffed against the
bucket by eye. The mirror is best-effort: S3 stays the record of truth.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from psycopg2 import sql

from ..base import _MISSING_AWS_MSG, _MISSING_DB_URL_MSG, AttemptResult

logger = logging.getLogger(__name__)


def _sanitize_s3_segment(name: str) -> str:
    """Make a task name safe as a single S3 key segment.

    Keeps alphanumerics, dash, underscore, and dot; replaces everything else
    (spaces, slashes, etc.) with "_" so a task name can't fork the key path
    into unintended sub-prefixes. Mirrors the local-filename sanitizer in
    infra/run.py.
    """
    if not name:
        return ""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)


@dataclass(frozen=True)
class AttemptSchema:
    """Describes the attempts table this sink writes to.

    `columns` is the INSERT column list; `json_columns` names the subset
    that must be wrapped in psycopg2.extras.Json so list/dict values are
    serialized to the JSON/JSONB column type rather than pg arrays.
    """

    table: str
    columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()


class PostgresS3AttemptSink:
    # Every file handed to publish() is uploaded to S3 before the DB row is
    # written, so the caller's copies are disposable.
    retains_files = True

    def __init__(
        self,
        *,
        db_url: str,
        s3_bucket: str,
        s3_prefix: str,
        attempt_schema: AttemptSchema,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        mirror_dir: str | Path | None = None,
        run_id: str | None = None,
    ):
        if not db_url:
            raise ValueError(
                "PostgresS3AttemptSink: db_url is empty. "
                + _MISSING_DB_URL_MSG.format(what="task_attempts sink")
            )
        if not s3_bucket:
            raise ValueError("PostgresS3AttemptSink: s3_bucket is empty.")
        self.db_url = db_url
        self.s3_bucket = s3_bucket
        self.s3_prefix = (s3_prefix or "").rstrip("/")
        self.attempt_schema = attempt_schema
        self.mirror_dir = Path(mirror_dir) if mirror_dir else None
        # One id per sink instance = one per `python -m infra.run` invocation,
        # shared by every task in that run. It disambiguates two runs of the
        # same (agent, task) that land in the same second, and makes "every
        # artifact from that run" a single grep over the S3 keys. 8 hex chars:
        # a full uuid4 would dominate the key without adding usable entropy.
        self.run_id = run_id or uuid.uuid4().hex[:8]

        self._conn: psycopg2.extensions.connection | None = None
        client_kwargs: dict[str, Any] = {}
        if aws_region:
            client_kwargs["region_name"] = aws_region
        if aws_access_key_id:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            client_kwargs["aws_session_token"] = aws_session_token
        self._s3 = boto3.client("s3", **client_kwargs)
        self._sts = boto3.client("sts", **client_kwargs)

        self._preflight_aws()

    # --- preflight ---------------------------------------------------------

    def _preflight_aws(self) -> None:
        """Verify AWS creds work and the target bucket is reachable BEFORE
        any engine work happens. Logs the caller identity so operators can
        confirm which AWS account they're running against.

        Raises ValueError with an actionable message on failure — the
        runner catches ValueError at build_sink() and exits cleanly."""
        try:
            ident = self._sts.get_caller_identity()
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise ValueError(
                f"AWS preflight failed: sts.get_caller_identity() errored "
                f"({type(e).__name__}: {e}). Check aws.access_key_id / "
                f"aws.secret_access_key in <repo>/config/config.yaml, or the "
                f"env vars named by aws.access_key_id_env / "
                f"aws.secret_access_key_env."
            ) from e
        logger.info(
            f"AWS identity: account={ident.get('Account')} " f"arn={ident.get('Arn')}"
        )
        try:
            self._s3.head_bucket(Bucket=self.s3_bucket)
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise ValueError(
                f"AWS preflight failed: head_bucket({self.s3_bucket!r}) "
                f"errored ({type(e).__name__}: {e}). Confirm the IAM user "
                f"has s3:ListBucket on {self.s3_bucket!r}, the bucket "
                f"exists, and aws.region matches the bucket's region."
            ) from e

    # --- extension points --------------------------------------------------

    def _s3_base_key(self, result: AttemptResult, timestamp: str) -> str:
        """Key prefix (under `s3_bucket`) for this attempt's uploaded files.
        Default layout: `{s3_prefix}/task_id={id}/{timestamp}`."""
        return f"{self.s3_prefix}/task_id={result.task_id}/{timestamp}"

    def _file_key(self, base_key: str, local: Path) -> str:
        """Full S3 key for one uploaded file.

        Default joins with "_" because the base key ends in a timestamp
        rather than a folder segment — subclasses whose base key IS a folder
        override this to join with "/".
        """
        return f"{base_key}_{local.name}"

    def _attempt_values(
        self,
        result: AttemptResult,
        attempt_file_uris: list[str],
        prompt_file_uris: list[str],
    ) -> dict[str, Any]:
        """Map an AttemptResult to column-value pairs. Keys must cover every
        column in `self.attempt_schema.columns`. Subclasses must override."""
        raise NotImplementedError

    # --- internals ---------------------------------------------------------

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_url)
        return self._conn

    def _attempt_files_to_upload(self, result: AttemptResult) -> list[Path]:
        out: list[Path] = []
        for p in (result.solution_file, *result.log_files):
            if p is None:
                continue
            p = Path(p)
            if not p.exists():
                logger.warning(f"Sink: skipping missing file {p}")
                continue
            out.append(p)
        return out

    def _prompt_files_to_upload(self, result: AttemptResult) -> list[Path]:
        out: list[Path] = []
        for p in result.prompt_files or []:
            p = Path(p)
            if not p.exists():
                logger.warning(f"Sink: skipping missing prompt file {p}")
                continue
            out.append(p)
        return out

    def _mirror_file(self, local: Path, key: str) -> None:
        """Copy an uploaded file into the local mirror at the same relative
        path as its S3 key, so `mirror_dir` reads like a local checkout of
        the bucket.

        Runs AFTER the upload succeeds, so the mirror only ever contains
        files that really reached S3. Best-effort: a full disk or a
        permission error must not fail an attempt whose files are already
        safely in the bucket and about to be recorded in the DB.
        """
        if self.mirror_dir is None:
            return
        dest = self.mirror_dir / key
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, dest)
        except OSError as e:
            logger.warning(
                f"Local mirror failed for {local} -> {dest} "
                f"({type(e).__name__}: {e}); the S3 copy stands"
            )

    def _upload_files(self, local_files: list[Path], base_key: str) -> list[str]:
        uris: list[str] = []
        for local in local_files:
            key = self._file_key(base_key, local)
            logger.info(f"S3 upload {local} -> s3://{self.s3_bucket}/{key}")
            self._s3.upload_file(str(local), self.s3_bucket, key)
            self._mirror_file(local, key)
            uris.append(f"s3://{self.s3_bucket}/{key}")
        return uris

    def _insert_row(self, values: dict[str, Any]) -> int | None:
        schema = self.attempt_schema
        missing = [c for c in schema.columns if c not in values]
        if missing:
            raise ValueError(f"_attempt_values missing required columns: {missing}")
        ident = sql.Identifier
        stmt = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES ({ph}) RETURNING id").format(
            tbl=ident(schema.table),
            cols=sql.SQL(", ").join(ident(c) for c in schema.columns),
            ph=sql.SQL(", ").join(sql.Placeholder() for _ in schema.columns),
        )
        params = [
            (
                psycopg2.extras.Json(values[c])
                if c in schema.json_columns and values[c] is not None
                else values[c]
            )
            for c in schema.columns
        ]
        # Neon (serverless PG) drops idle connections server-side during long
        # GUI tasks; the stale socket still reads as open client-side, so the
        # next execute dies with OperationalError ("SSL connection has been
        # closed unexpectedly") and even rollback() then raises. Losing that
        # insert loses the whole attempt (files are already in S3) AND kills
        # the run mid-pass (observed 3x on 2026-07-24). Reconnect and retry
        # once on stale-connection errors; the INSERT is not committed on the
        # dead connection, so the retry cannot double-insert.
        last_exc: Exception | None = None
        for attempt in range(2):
            conn = self._connect()
            try:
                # Neon computes have been observed serving sessions with
                # default_transaction_read_only=on (2026-08-27: engine
                # succeeded, INSERT refused with ReadOnlySqlTransaction,
                # attempt stranded in S3 with no row). The default is
                # advisory — request a read-write transaction explicitly,
                # as the FIRST statement of the INSERT's own transaction so
                # it also holds under PgBouncer transaction pooling (where
                # session-level SETs don't stick to the next transaction).
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ WRITE")
                    cur.execute(stmt, params)
                    row = cur.fetchone()
                conn.commit()
                return row[0] if row else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_exc = e
                logger.warning(
                    f"Sink: DB connection stale ({e.__class__.__name__}: {e}); "
                    f"{'reconnecting and retrying' if attempt == 0 else 'giving up'}"
                )
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        raise last_exc

    # --- public API --------------------------------------------------------

    def _after_insert(self, row_id: int | None, result: AttemptResult) -> None:
        """Hook: runs after the row is committed. Default: nothing."""

    def publish(self, result: AttemptResult) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self._s3_base_key(result, timestamp)
        attempt_uris = self._upload_files(self._attempt_files_to_upload(result), base)
        prompt_uris = self._upload_files(self._prompt_files_to_upload(result), base)
        values = self._attempt_values(result, attempt_uris, prompt_uris)
        row_id = self._insert_row(values)
        self._after_insert(row_id, result)
        logger.info(
            f"Sink: recorded attempt for task_id={result.task_id} "
            f"status={result.status} attempt_files={len(attempt_uris)} "
            f"prompt_files={len(prompt_uris)} "
            f"s3://{self.s3_bucket}/{base}"
        )
        if self.mirror_dir is not None:
            # A prefix, not necessarily a directory — the v1 layout joins the
            # base key to the filename with "_" rather than "/".
            logger.info(f"Sink: local mirror under {self.mirror_dir / base}*")

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


# ----- Benchmark-specific subclasses ----------------------------------------

# The task_attempts table is column-identical in BizbenchV1 and MBABenchV2;
# only the DB pointed at (database.url) and the S3 key layout differ.
TASK_ATTEMPTS_SCHEMA = AttemptSchema(
    table="task_attempts",
    columns=(
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
    ),
    json_columns=frozenset({"attempt_files", "prompt_files"}),
)


class _TaskAttemptsPostgresS3Sink(PostgresS3AttemptSink):
    """Shared task_attempts wiring for both benchmarks (see module docstring).

    Subclasses pick the S3 key layout via `_s3_base_key` / `_upload_files`.
    """

    def __init__(
        self,
        *,
        db_url: str,
        s3_bucket: str,
        s3_prefix: str,
        agent_folder: str,
        agent_model_name: str,
        agent_model_type: str = "excel",
        prompt_version: int | str | None,
        extra_configs: dict[str, Any] | None = None,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        mirror_dir: str | Path | None = None,
        run_id: str | None = None,
    ):
        if not agent_folder:
            raise ValueError(
                "task_attempts sink: agent_folder is required. "
                "This is derived from resolve_agent_identity(cfg) — "
                "an empty value means the resolver returned an invalid "
                "AgentIdentity (check infra/configs/agent_identity.py)."
            )
        if not agent_model_name:
            raise ValueError(
                "task_attempts sink: agent_model_name is required. "
                "This is derived from resolve_agent_identity(cfg) — "
                "an empty value means the resolver returned an invalid "
                "AgentIdentity (check infra/configs/agent_identity.py)."
            )
        if not db_url:
            raise ValueError(_MISSING_DB_URL_MSG.format(what="task_attempts sink"))
        if not (aws_access_key_id and aws_secret_access_key):
            raise ValueError(_MISSING_AWS_MSG.format(what="task_attempts sink"))
        super().__init__(
            db_url=db_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            attempt_schema=TASK_ATTEMPTS_SCHEMA,
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            mirror_dir=mirror_dir,
            run_id=run_id,
        )
        self.agent_folder = agent_folder
        self.agent_model_name = agent_model_name
        self.agent_model_type = agent_model_type
        self.prompt_version = prompt_version
        self.extra_configs = dict(extra_configs or {})
        self._has_extra_configs_column: bool | None = None  # probed lazily
        mirror_note = (
            f"mirrored to {self.mirror_dir}" if self.mirror_dir else "no local mirror"
        )
        logger.info(
            f"Sink: run_id={self.run_id} -> "
            f"s3://{self.s3_bucket}/{self.s3_prefix}/{self.agent_folder}/ "
            f"({mirror_note})"
        )

    def _task_metadata(self, result: AttemptResult) -> dict:
        extra = result.extra or {}
        meta = extra.get("task_metadata")
        return meta if isinstance(meta, dict) else {}

    # --- extra_configs (MBABenchV2 only; probe + raw SQL by convention) ----

    def _probe_extra_configs_column(self) -> bool:
        """Does the connected task_attempts table have extra_configs?

        BizbenchV1 does not; MBABenchV2 does. The column is deliberately
        never mapped in any ORM model (a mapped attribute lands in every
        SELECT and broke v1 lookups once) — probe once, then raw SQL.
        """
        if self._has_extra_configs_column is None:
            try:
                conn = self._connect()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = 'extra_configs'",
                        (self.attempt_schema.table,),
                    )
                    self._has_extra_configs_column = cur.fetchone() is not None
                conn.rollback()  # end the probe's implicit transaction
            except Exception as e:
                logger.warning(
                    f"Sink: extra_configs column probe failed "
                    f"({type(e).__name__}: {e}); skipping extra_configs writes"
                )
                self._has_extra_configs_column = False
            if not self._has_extra_configs_column:
                logger.info(
                    "Sink: task_attempts has no extra_configs column — run "
                    "settings will only be recorded locally"
                )
        return self._has_extra_configs_column

    def _after_insert(self, row_id: int | None, result: AttemptResult) -> None:
        """Write the identity settings + per-attempt runtime stamps to
        extra_configs. Best-effort: the row (and its S3 files) already
        stand; a failure here warns and never fails the attempt."""
        payload = dict(self.extra_configs)
        per_attempt = (result.extra or {}).get("extra_configs")
        if isinstance(per_attempt, dict):
            payload |= per_attempt
        if not payload or row_id is None:
            return
        if not self._probe_extra_configs_column():
            return
        try:
            import json

            conn = self._connect()
            conn.rollback()
            with conn.cursor() as cur:
                # Same read-only-default guard as _insert_row.
                cur.execute("SET TRANSACTION READ WRITE")
                cur.execute(
                    sql.SQL(
                        "UPDATE {tbl} SET extra_configs = %s::jsonb WHERE id = %s"
                    ).format(tbl=sql.Identifier(self.attempt_schema.table)),
                    (json.dumps(payload), row_id),
                )
            conn.commit()
        except Exception as e:
            logger.warning(
                f"Sink: could not write extra_configs for row {row_id} "
                f"({type(e).__name__}: {e}); the attempt row itself stands"
            )
            try:
                self._connect().rollback()
            except Exception:
                pass

    def _attempt_values(
        self,
        result: AttemptResult,
        attempt_file_uris: list[str],
        prompt_file_uris: list[str],
    ) -> dict[str, Any]:
        meta = self._task_metadata(result)
        db_task_id = meta.get("db_task_id")
        if db_task_id is None:
            try:
                db_task_id = int(result.task_id)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"task_attempts sink: task_id must resolve to "
                    f"an int, got {result.task_id!r}. Ensure the source "
                    f"populates spec.metadata['db_task_id'] or yields numeric "
                    f"task_ids (this sink writes to task_attempts.task_id "
                    f"which is INT NOT NULL)."
                ) from e

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

        return {
            "task_id": db_task_id,
            "agent_model_name": self.agent_model_name,
            "agent_model_type": self.agent_model_type,
            "attempt_files": attempt_file_uris,
            "prompt_files": prompt_file_uris,
            "start_time": start_dt,
            "end_time": end_dt,
            "time_taken_min": time_taken_min,
            "cost": None,
            "prompt_version": self.prompt_version,
            "agent_failed": agent_failed,
            "agent_failed_reason": agent_failed_reason,
            "deprecated": False,
        }


class BizbenchPostgresS3AttemptSink(_TaskAttemptsPostgresS3Sink):
    """Benchmark v1 sink (BizbenchV1 DB).

    S3 layout (mirrors cli-agents-master/auto_batch_runner.py):
        {s3_prefix}/{agent_folder}/task_source={src}/task_id={id}/{ts}_{name}

    The source (`BizbenchPostgresS3TaskSource`) populates
    `spec.metadata["task_source"]` and `spec.metadata["db_task_id"]`; the
    runner threads those through as `result.extra["task_metadata"]` so
    they land here. Uses the base `_upload_files` ("_"-joined), so each
    file key ends `{timestamp}_{filename}`.
    """

    def _s3_base_key(self, result: AttemptResult, timestamp: str) -> str:
        task_source = self._task_metadata(result).get("task_source") or "unknown"
        return (
            f"{self.s3_prefix}/{self.agent_folder}"
            f"/task_source={task_source}/task_id={result.task_id}/{timestamp}"
        )


class MBABenchV2PostgresS3AttemptSink(_TaskAttemptsPostgresS3Sink):
    """Benchmark v2 sink (MBABenchV2 DB).

    S3 layout — one folder per attempt, under a per-task folder:
        {s3_prefix}/{agent_folder}/{task_name}/{timestamp}_{run_id}/{name}
    e.g. MBABenchV2/attempts/claude_opus_4_8/ApfelInc/20260623_120000_9f3ac81b/
         ├── 20260623_115804_ApfelInc_Solution_claude_web_Model.xlsx
         ├── completion_claude_web_20260623_115012_ApfelInc.json
         └── prompts_ApfelInc_20260623_115012.json

    Every file from one attempt lands in that single folder, so an attempt
    can be downloaded, diffed, or deleted as a unit — the previous layout
    interleaved every attempt for a task in one flat folder and relied on
    the engine's per-file timestamps to tell them apart, which broke down
    for files stamped in the same second and made "which workbook goes with
    which prompt record?" a filename-parsing exercise.

    `timestamp` is the publish time (per task); `run_id` is constant across
    every task in one `infra.run` invocation, so it also groups a sweep.

    task_name is sanitized (_sanitize_s3_segment) so it stays a single key
    segment. task_source / db_task_id still flow through
    `result.extra["task_metadata"]` (populated by the source) and are
    written to the task_attempts row, just not encoded in the S3 path.
    """

    def _s3_base_key(self, result: AttemptResult, timestamp: str) -> str:
        safe_task = _sanitize_s3_segment(result.task_name) or f"task_id={result.task_id}"
        return (
            f"{self.s3_prefix}/{self.agent_folder}/{safe_task}"
            f"/{timestamp}_{self.run_id}"
        )

    def _file_key(self, base_key: str, local: Path) -> str:
        # The base key is a real folder here, so join with "/".
        return f"{base_key}/{local.name}"
