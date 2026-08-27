#!/usr/bin/env python3
"""
Auto Batch Runner for Excel CLI Agent

Automated pipeline that handles the full lifecycle:
  DB lookup -> trial count check -> S3 download -> task execution -> S3 upload -> DB entry

Extends BatchRunner with auto-discovery, S3 workspace setup, and result upload.
"""

import json
import os
import shutil
import time
import traceback
import yaml
import boto3
from sqlalchemy import inspect as sa_inspect, text as sa_text
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date

from .batch_runner import BatchRunner, WorkspaceConfig, WorkspaceResult, BatchResult

from .agent_identity import resolve_agent_identity
from .db import database as db_config
from .db.database import SessionLocal
from .db.models import Task, TaskAttempt
from .repo_config import (
    boto3_credentials, describe_database_target, repo_value, resolve_db_url,
)
from .prompt_versions import (
    PROMPTS_DIR, PROMPT_VERSIONS, DEFAULT_PROMPT_VERSION, parse_prompt_version,
    rubric_for_prompt_version,
)

# Resolved prompt paths (set by load_config based on prompt_version)
SYSTEM_PROMPT_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["system"]
TASK_TEMPLATE_FMWC_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["fmwc"]
TASK_TEMPLATE_WSP_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["wsp"]

# Local per-attempt record, kept even after the workspace is cleaned up:
# run_logs/attempt-{model}-{timestamp}/ at the cli-agents-master root.
RUN_LOGS_DIR: Path = Path(__file__).resolve().parents[1] / "run_logs"


@dataclass
class TaskInfo:
    """Information about a task from the database"""
    task_id: int
    task_name: str
    task_source: str
    task_starting_files: List[str] = field(default_factory=list)


def _normalize_task_name(name: str) -> str:
    """Normalize task name: replace spaces with underscores for DB lookup."""
    return name.replace(' ', '_')



class AutoBatchRunner(BatchRunner):
    """Automated batch runner with DB-driven task discovery, S3 download/upload, and trial management."""

    # Per-benchmark storage wiring. The benchmark also selects the database
    # URL (database.v1_url / v2_url in <repo>/config/config.yaml, see
    # load_config) so a v1 config can never write to the MBABenchV2 DB or
    # vice versa. The bucket comes from the same config (aws.s3_bucket).
    # Prompts pair with the benchmark by default: `prompt_version` still
    # picks the prompt set, but load_config defaults it from `benchmark` and
    # refuses a prompt set embedding the other benchmark's rubric
    # (EXCEL_AGENT_SKIP_RUBRIC_GUARD=1 overrides).
    BENCHMARKS = {
        "v1": {"root": "BizbenchV1", "db_name": "BizbenchV1"},
        "v2": {"root": "MBABenchV2", "db_name": "MBABenchV2"},
    }

    def __init__(self, config_path: str, server_path: str, api_key: str,
                 custom_reasoning: bool = False, enable_langfuse: bool = False):
        super().__init__(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
        self._s3_client = None
        self._prompt_s3_uris: Optional[List[str]] = None
        # Placeholders until load_config resolves them from the benchmark
        # key and the monorepo config.
        self._s3_bucket = "mbabench"
        self._s3_root = "BizbenchV1"
        self._identity = None
        self._extra_configs_supported = False

    @property
    def s3_client(self):
        if self._s3_client is None:
            # Credentials from <repo>/config/config.yaml aws.* when set;
            # {} falls back to boto3's default chain (env, AWS_PROFILE).
            self._s3_client = boto3.client("s3", **boto3_credentials())
        return self._s3_client

    def _s3_agent_prefix(self) -> str:
        """Build the S3 prefix: {root}/attempts/{model}_openpyxl"""
        model = self.config['model']
        return f"{self._s3_root}/attempts/{model}_openpyxl"

    def _s3_task_prefix(self, task_info: TaskInfo) -> str:
        """Build per-task S3 prefix: {agent_prefix}/task_source={src}/task_id={id}"""
        return f"{self._s3_agent_prefix()}/task_source={task_info.task_source}/task_id={task_info.task_id}"

    def load_config(self) -> Dict[str, Any]:
        """Load and validate YAML configuration for auto mode.

        Relaxes required fields - no task_template or workspaces needed.
        """
        print(f"📋 Loading auto batch configuration from {self.config_path}")

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Validate required fields for auto mode
        required_fields = ['batch_name', 'agent_model_name']
        for fld in required_fields:
            if fld not in config:
                raise ValueError(f"Missing required field in config: {fld}")

        if not config.get('auto_mode'):
            raise ValueError("auto_mode must be true for AutoBatchRunner")

        # workspace_base_dir is optional: unset, workspaces are created under
        # the batch's own logs directory (batch_logs/batch_{ts}/workspaces/),
        # keeping each batch's working files and logs in one place.

        # Must have either tasks, task_ids, or task_filter
        if 'tasks' not in config and 'task_ids' not in config and 'task_filter' not in config:
            raise ValueError(
                "Either 'tasks' (list of task names), 'task_ids' (list of task ids), "
                "or 'task_filter' is required"
            )

        # Set defaults
        config.setdefault('verbose', False)
        config.setdefault('max_iterations', 30)
        config.setdefault('batch_size', 1)
        config.setdefault('snapshot_iterations', False)
        config.setdefault('max_trials', 7)
        config.setdefault('trials_since', date.today().isoformat())
        # When true, a task with a non-failed attempt (for this agent, since
        # trials_since) is skipped even if trials remain — so a resumed batch
        # never re-runs tasks that already succeeded. Default false preserves
        # the historical trial-count-only behavior.
        config.setdefault('skip_if_succeeded', False)

        # The config names its cohort (agent_model_name) and NOTHING else
        # about the model: the agent_identities.yaml entry for that label
        # supplies model, reasoning_effort, thinking_budget_tokens,
        # max_completion_tokens, base_url, fresh_context_mode,
        # enhanced_excel_context and recent_history_count. A config that sets
        # any of them refuses to run (resolve_agent_identity), so rows under
        # one label cannot have run with different settings. agent_folder is
        # the retired name for a free-text label.
        if 'agent_folder' in config:
            raise ValueError(
                "agent_folder is no longer supported. Name the cohort with "
                "agent_model_name (an entry in excel_cli_agent/"
                "agent_identities.yaml); the registry supplies the model "
                "settings."
            )
        identity = resolve_agent_identity(config)
        # From here the executor reads the registry's values from config
        # like any other key.
        config.update(identity.settings())
        self._identity = identity

        # Benchmark selection. Required so a config can never silently
        # target the wrong experiment; it selects the database URL
        # (database.v{1,2}_url in <repo>/config/config.yaml, falling back to
        # DATABASE_URL for standalone runs) together with the S3 root,
        # so a v1 run gets v1's store or nothing.
        if 'benchmark' not in config:
            raise ValueError(
                "Missing required field in config: benchmark ('v1' = "
                "BizbenchV1 wave, 'v2' = MBABenchV2 task set). It selects "
                "the database, S3 root, and default prompt set together."
            )
        benchmark = str(config['benchmark']).lower()
        if benchmark not in self.BENCHMARKS:
            raise ValueError(
                f"Unknown benchmark '{benchmark}'. Available: "
                f"{list(self.BENCHMARKS.keys())} (v1 = BizbenchV1 wave, "
                f"v2 = MBABenchV2 task set)"
            )
        bench = self.BENCHMARKS[benchmark]
        self._s3_bucket = repo_value("aws", "s3_bucket") or "mbabench"
        self._s3_root = bench["root"]
        db_config.configure(benchmark)
        db_url, db_source = resolve_db_url(benchmark)
        if not db_url:
            raise ValueError(
                f"No database URL for benchmark={benchmark}. Set "
                f"database.{benchmark}_url in <MBABenchV2>/config/config.yaml "
                f"or export DATABASE_URL."
            )
        # The monorepo layer is selected by benchmark and can't mismatch;
        # this guards the blind $DATABASE_URL fallback.
        if f"/{bench['db_name']}" not in db_url:
            raise ValueError(
                f"benchmark={benchmark} expects a {bench['db_name']} "
                f"database, but the URL from {db_source} points elsewhere. "
                f"Export the {bench['db_name']} connection string (or fix "
                f"the config's benchmark key) before running."
            )
        # task_attempts.extra_configs (JSONB) exists in MBABenchV2 only. It
        # is deliberately not on the ORM model: a mapped column missing from
        # one database breaks every SELECT there (see db/models.py), so the
        # runner probes for it and writes it with plain SQL after the insert.
        self._extra_configs_supported = self._probe_extra_configs_column()
        if not self._extra_configs_supported:
            print(f"⚠️  task_attempts.extra_configs not present in the {bench['db_name']} "
                  f"database — run settings will NOT be recorded on the row "
                  f"(only in the local attempt package and this log).")

        # Resolve prompt version from config; the default follows the
        # benchmark (v1 -> DEFAULT_PROMPT_VERSION, v2 -> the v2-rubric set),
        # and an explicit choice must embed this benchmark's rubric — running
        # v2 tasks against prompts that promise the v1 grading rubric (or
        # vice versa) invalidates the wave. Set EXCEL_AGENT_SKIP_RUBRIC_GUARD=1
        # for a deliberate cross-benchmark experiment (logged loudly).
        global SYSTEM_PROMPT_PATH, TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH
        default_ver = "v13" if benchmark == "v2" else DEFAULT_PROMPT_VERSION
        prompt_ver = config.get('prompt_version', default_ver)
        if prompt_ver not in PROMPT_VERSIONS:
            raise ValueError(f"Unknown prompt_version '{prompt_ver}'. Available: {list(PROMPT_VERSIONS.keys())}")
        prompt_rubric = rubric_for_prompt_version(prompt_ver)
        if prompt_rubric != benchmark:
            if os.environ.get("EXCEL_AGENT_SKIP_RUBRIC_GUARD"):
                print(f"⚠️  EXCEL_AGENT_SKIP_RUBRIC_GUARD set — running benchmark "
                      f"{benchmark} with the {prompt_rubric}-rubric prompt set "
                      f"({prompt_ver}) as a deliberate cross-benchmark experiment.")
            else:
                suggestion = "v13" if benchmark == "v2" else "v11"
                raise ValueError(
                    f"prompt_version {prompt_ver} embeds the {prompt_rubric} "
                    f"grading rubric, but benchmark={benchmark} tasks are graded "
                    f"with the {benchmark} rubric. Use prompt_version "
                    f"{suggestion} (or omit the key to default it from the "
                    f"benchmark); set EXCEL_AGENT_SKIP_RUBRIC_GUARD=1 to force "
                    f"a cross-benchmark run."
                )
        ver_files = PROMPT_VERSIONS[prompt_ver]
        SYSTEM_PROMPT_PATH = PROMPTS_DIR / ver_files["system"]
        TASK_TEMPLATE_FMWC_PATH = PROMPTS_DIR / ver_files["fmwc"]
        TASK_TEMPLATE_WSP_PATH = PROMPTS_DIR / ver_files["wsp"]

        # Set versioned system prompt path for TaskExecutor
        config['system_prompt_path'] = str(SYSTEM_PROMPT_PATH)

        self.config = config

        print(f"✅ Configuration loaded: {config['batch_name']}")
        print(f"   Model: {config['model']}")
        print(f"   Benchmark: {benchmark}")
        print(f"   Database: {describe_database_target(benchmark)}")
        print(f"   S3: s3://{self._s3_bucket}/{self._s3_root}/")
        print(f"   Agent model name: {config['agent_model_name']} (agent_identities.yaml)")
        print(f"   Pinned by identity: {identity.settings()}")
        print(f"   extra_configs column: {'yes' if self._extra_configs_supported else 'NO'}")
        print(f"   Prompt version: {prompt_ver}")
        print(f"   Max iterations: {config['max_iterations']}")
        print(f"   Max trials: {config['max_trials']}")
        print(f"   Trials since: {config['trials_since']}")
        print(f"   Workspace base: {config.get('workspace_base_dir') or 'batch_logs/batch_<ts>/workspaces (default)'}")

        return config

    @staticmethod
    def _probe_extra_configs_column() -> bool:
        """True if task_attempts has an extra_configs column in the connected DB."""
        cols = sa_inspect(db_config._get_engine()).get_columns("task_attempts")
        return any(c["name"] == "extra_configs" for c in cols)

    def _write_extra_configs(self, db, attempt_id: int) -> None:
        """Record the identity's full settings on the row (v2 DB only),
        plus the recalc-engine provenance from the batch preflight."""
        if not self._extra_configs_supported:
            return
        cfg = dict(self._identity.extra_configs())
        cfg.update(self._recalc_extra_configs())
        db.execute(
            sa_text("UPDATE task_attempts SET extra_configs = CAST(:cfg AS jsonb) WHERE id = :id"),
            {"cfg": json.dumps(cfg), "id": attempt_id},
        )
        db.commit()

    def resolve_tasks(self) -> List[TaskInfo]:
        """Query DB for tasks to process based on config."""
        db = SessionLocal()
        try:
            return self._resolve_tasks_impl(db)
        finally:
            db.close()

    def _resolve_tasks_impl(self, db) -> List[TaskInfo]:
        tasks_result = []

        if 'task_ids' in self.config:
            # Explicit task ids mode — preserves the given order (e.g. a
            # lightest-to-heaviest ranking).
            task_ids = self.config['task_ids']
            print(f"\n🔍 Resolving {len(task_ids)} explicit task id(s) from database...")
            for tid in task_ids:
                task = db.query(Task).filter(Task.id == tid).first()
                if task is None:
                    print(f"  ❌ id {tid} -> NOT FOUND in DB")
                    continue
                if task.deprecated:
                    print(f"  ⏭️  id {tid} ({task.task_name}) -> deprecated, skipping")
                    continue
                tasks_result.append(TaskInfo(
                    task_id=task.id,
                    task_name=task.task_name,
                    task_source=task.task_source,
                    task_starting_files=task.task_starting_files or [],
                ))
                print(f"  ✅ id {tid} -> {task.task_name} ({task.task_source})")

        elif 'tasks' in self.config:
            # Explicit task names mode
            task_names = self.config['tasks']
            print(f"\n🔍 Resolving {len(task_names)} explicit task(s) from database...")

            for name in task_names:
                task = self._find_task_by_name(db, name)
                if task:
                    tasks_result.append(TaskInfo(
                        task_id=task.id,
                        task_name=task.task_name,
                        task_source=task.task_source,
                        task_starting_files=task.task_starting_files or [],
                    ))
                    print(f"  ✅ {name} -> ID {task.id} ({task.task_source})")
                else:
                    print(f"  ❌ {name} -> NOT FOUND in DB")

        elif 'task_filter' in self.config:
            # Auto-discovery mode
            tf = self.config['task_filter']
            print(f"\n🔍 Auto-discovering tasks from database...")

            query = db.query(Task).filter(Task.deprecated == False)  # noqa: E712

            # Filter by task_source
            task_source = tf.get('task_source')
            if task_source:
                query = query.filter(Task.task_source == task_source)
                print(f"  Filter: task_source = {task_source}")

            all_tasks = query.all()
            print(f"  Found {len(all_tasks)} eligible tasks in DB")

            # Filter for missing_for_model: only tasks where trial count < max_trials
            if tf.get('missing_for_model'):
                agent_model_name = self.config['agent_model_name']
                trials_since = self.config['trials_since']
                max_trials = self.config['max_trials']

                for task in all_tasks:
                    trial_count = self._get_trial_count(db, task.id, agent_model_name, trials_since)
                    if trial_count < max_trials:
                        tasks_result.append(TaskInfo(
                            task_id=task.id,
                            task_name=task.task_name,
                            task_source=task.task_source,
                            task_starting_files=task.task_starting_files or [],
                        ))

                print(f"  After trial filter (< {max_trials} trials since {trials_since}): {len(tasks_result)} tasks")
            else:
                for task in all_tasks:
                    tasks_result.append(TaskInfo(
                        task_id=task.id,
                        task_name=task.task_name,
                        task_source=task.task_source,
                        task_starting_files=task.task_starting_files or [],
                    ))

        # Explicit skip list — tasks intentionally deferred (e.g. starting
        # files too large for the model's input context). See SKIPPED_TASKS.md.
        skip_ids = set(self.config.get('skip_task_ids') or [])
        if skip_ids:
            kept = [t for t in tasks_result if t.task_id not in skip_ids]
            dropped = sorted({t.task_id for t in tasks_result} & skip_ids)
            if dropped:
                print(f"⏭️  Skipping {len(dropped)} task id(s) via skip_task_ids: {dropped}")
            tasks_result = kept

        print(f"📋 Total tasks to process: {len(tasks_result)}")
        return tasks_result

    def _find_task_by_name(self, db, name: str) -> Optional[Task]:
        """Find a task by name, trying normalized variants."""
        normalized = _normalize_task_name(name)

        # Try normalized first (canonical)
        if normalized != name:
            task = db.query(Task).filter(
                Task.task_name == normalized,
                Task.deprecated == False  # noqa: E712
            ).first()
            if task:
                return task

        # Try exact
        task = db.query(Task).filter(
            Task.task_name == name,
            Task.deprecated == False  # noqa: E712
        ).first()
        if task:
            return task

        # Fallback: any match including deprecated
        task = db.query(Task).filter(Task.task_name == name).first()
        if task:
            return task
        if normalized != name:
            task = db.query(Task).filter(Task.task_name == normalized).first()
        return task

    def _get_trial_count(self, db, task_id: int, agent_model_name: str, trials_since: str) -> int:
        """Count non-deprecated attempts for a task+model since a given date."""
        from sqlalchemy import func as sa_func
        count = db.query(sa_func.count(TaskAttempt.id)).filter(
            TaskAttempt.task_id == task_id,
            TaskAttempt.agent_model_name == agent_model_name,
            TaskAttempt.deprecated == False,  # noqa: E712
            TaskAttempt.created_at >= trials_since,
        ).scalar()
        return count or 0

    def get_trial_count(self, task_info: TaskInfo) -> int:
        """Public interface: get trial count for a task."""
        db = SessionLocal()
        try:
            return self._get_trial_count(
                db, task_info.task_id,
                self.config['agent_model_name'],
                self.config['trials_since'],
            )
        finally:
            db.close()

    def _has_success(self, task_info: TaskInfo) -> bool:
        """True if a non-failed, non-deprecated attempt exists for this
        task+agent since trials_since."""
        db = SessionLocal()
        try:
            row = db.query(TaskAttempt.id).filter(
                TaskAttempt.task_id == task_info.task_id,
                TaskAttempt.agent_model_name == self.config['agent_model_name'],
                TaskAttempt.deprecated == False,  # noqa: E712
                TaskAttempt.agent_failed.isnot(True),
                TaskAttempt.created_at >= self.config['trials_since'],
            ).first()
            return row is not None
        finally:
            db.close()

    def should_skip(self, task_info: TaskInfo) -> bool:
        """Check if task should be skipped (already succeeded, or max trials)."""
        if self.config.get('skip_if_succeeded') and self._has_success(task_info):
            print(f"  ⏭️  {task_info.task_name}: already has a successful attempt, skipping")
            return True

        trial_count = self.get_trial_count(task_info)
        max_trials = self.config['max_trials']

        if trial_count >= max_trials:
            print(f"  ⏭️  {task_info.task_name}: {trial_count}/{max_trials} trials exhausted, skipping")
            return True

        print(f"  📊 {task_info.task_name}: trial {trial_count + 1}/{max_trials}")
        return False

    def setup_workspace(self, task_info: TaskInfo) -> str:
        """Create workspace directory and download S3 starting files."""
        base_override = self.config.get('workspace_base_dir')
        if base_override:
            base_dir = Path(base_override).expanduser()
        elif self.batch_logs_dir is not None:
            base_dir = Path(self.batch_logs_dir) / "workspaces"
        else:
            raise RuntimeError(
                "No workspace location: set workspace_base_dir in the config, "
                "or run via run_batch so the batch_logs directory exists."
            )
        # Use task_name with spaces replaced by underscores for folder names
        # Append timestamp+PID to avoid collisions when multiple processes run concurrently
        folder_name = task_info.task_name.replace(' ', '_')
        run_id = f"{int(time.time())}_{os.getpid()}"
        workspace = base_dir / f"{folder_name}_{run_id}"
        workspace.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Workspace: {workspace}")

        # A task with no registered starting files would run the agent with no
        # task content at all (the July empty-context defect) — refuse it.
        if not task_info.task_starting_files:
            raise RuntimeError(
                f"Task '{task_info.task_name}' has no task_starting_files; "
                "refusing to run the agent with an empty workspace"
            )

        for s3_uri in task_info.task_starting_files:
            # Parse s3://bucket/key
            if not s3_uri.startswith("s3://"):
                raise RuntimeError(f"Invalid S3 URI for '{task_info.task_name}': {s3_uri}")

            parts = s3_uri[5:].split("/", 1)
            if len(parts) != 2:
                raise RuntimeError(f"Invalid S3 URI format for '{task_info.task_name}': {s3_uri}")

            bucket = parts[0]
            key = parts[1]
            filename = key.split("/")[-1]
            local_path = workspace / filename

            if local_path.exists():
                print(f"  ✅ Already exists: {filename}")
                continue

            try:
                print(f"  ⬇️  Downloading: {filename}")
                self.s3_client.download_file(bucket, key, str(local_path))
            except Exception as e:
                raise RuntimeError(
                    f"Failed to download {s3_uri} for '{task_info.task_name}': {e}. "
                    "Aborting this task — running without starting files would "
                    "produce an invalid (empty-context) attempt."
                ) from e
            size = local_path.stat().st_size
            if size == 0:
                raise RuntimeError(
                    f"Downloaded zero-byte file {filename} from {s3_uri} for "
                    f"'{task_info.task_name}'; refusing to run on an empty starting file"
                )
            print(f"  ✅ Downloaded: {filename} ({size:,} bytes)")

        # Belt and braces: every registered starting file must now be present
        # and non-empty in the workspace before the agent is allowed to run.
        missing = [
            uri.split("/")[-1]
            for uri in task_info.task_starting_files
            if not (workspace / uri.split("/")[-1]).exists()
            or (workspace / uri.split("/")[-1]).stat().st_size == 0
        ]
        if missing:
            raise RuntimeError(
                f"Workspace for '{task_info.task_name}' is missing starting files "
                f"after download: {missing}"
            )

        return str(workspace)

    def _verify_s3_access(self):
        """Abort the batch immediately if S3 credentials/bucket access are broken."""
        try:
            self.s3_client.head_bucket(Bucket=self._s3_bucket)
            print(f"  ✅ S3 access verified: s3://{self._s3_bucket}")
        except Exception as e:
            raise RuntimeError(
                f"Cannot access s3://{self._s3_bucket} ({e}). Check AWS "
                "credentials (aws.access_key_id / aws.secret_access_key in "
                "<MBABenchV2>/config/config.yaml, or the boto3 default chain: "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE). "
                "Refusing to start: without S3 the batch would run agents on "
                "empty workspaces and record invalid attempts."
            ) from e

    def cleanup_workspace(self, workspace_path: str):
        """Delete entire workspace folder after results are uploaded to S3."""
        if self.config.get('cleanup_workspace', True) is False:
            print(f"  📂 Workspace preserved: {workspace_path}")
            return
        try:
            shutil.rmtree(workspace_path)
            print(f"  🧹 Cleaned up workspace: {workspace_path}")
        except Exception as e:
            print(f"  ⚠️  Cleanup failed for {workspace_path}: {e}")

    def select_task_template(self, task_source: str) -> str:
        """Load task_template from versioned txt file based on task_source."""
        template_path = TASK_TEMPLATE_WSP_PATH if task_source == "wsp" else TASK_TEMPLATE_FMWC_PATH
        if not template_path.exists():
            raise FileNotFoundError(f"Task template not found: {template_path}")
        return template_path.read_text(encoding='utf-8')

    def upload_prompts(self) -> List[str]:
        """Upload versioned prompt snapshots to S3. Called once at batch start.

        Path: {root}/prompts/{model}_openpyxl/{timestamp}_{filename}
        """
        if self._prompt_s3_uris is not None:
            return self._prompt_s3_uris

        agent_prefix = self._s3_agent_prefix()  # {root}/attempts/{model}_openpyxl
        # Use a parallel prompts/ tree under the same agent identifier
        prompts_prefix = agent_prefix.replace(
            f"{self._s3_root}/attempts/", f"{self._s3_root}/prompts/"
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uris = []

        # Upload system prompt
        if SYSTEM_PROMPT_PATH.exists():
            s3_key = f"{prompts_prefix}/{timestamp}_{SYSTEM_PROMPT_PATH.name}"
            try:
                self.s3_client.upload_file(str(SYSTEM_PROMPT_PATH), self._s3_bucket, s3_key)
                uri = f"s3://{self._s3_bucket}/{s3_key}"
                uris.append(uri)
                print(f"  📤 Uploaded system prompt: {uri}")
            except Exception as e:
                print(f"  ⚠️  Failed to upload system prompt: {e}")

        # Upload task templates (both variants; v12's slots share one file,
        # so dedupe to keep prompt_files free of repeats)
        for template_path in dict.fromkeys([TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH]):
            if template_path.exists():
                s3_key = f"{prompts_prefix}/{timestamp}_{template_path.name}"
                try:
                    self.s3_client.upload_file(str(template_path), self._s3_bucket, s3_key)
                    uri = f"s3://{self._s3_bucket}/{s3_key}"
                    uris.append(uri)
                    print(f"  📤 Uploaded template: {uri}")
                except Exception as e:
                    print(f"  ⚠️  Failed to upload template {template_path.name}: {e}")

        self._prompt_s3_uris = uris
        return uris

    def _model_slug(self) -> str:
        """Model name safe as one path segment ('openai/gpt-5.2' nests otherwise)."""
        return self.config['model'].replace('/', '_')

    def _v2_upload_files(self, package_dir: Path) -> List[Tuple[Path, str]]:
        """(local_path, relative_key) pairs for the v2 per-attempt S3 folder.

        solution.xlsx is the only workbook uploaded: starting files come from
        the task's DB row (tasks.task_starting_files), and intermediate
        workbook copies live in the local run_logs record — so the S3 attempt
        record stays unambiguous about which workbook is the result, and a run
        that produced no solution.xlsx uploads no workbook at all.
        solution.xlsx sorts first so attempt_files leads with it.
        """
        def _include(p: Path) -> bool:
            if p.name == "solution.xlsx":
                return True
            return not p.name.lower().endswith(('.xlsx', '.xlsm', '.xlsb', '.xls'))

        return [
            (p, p.relative_to(package_dir).as_posix())
            for p in sorted(
                (q for q in package_dir.rglob('*') if q.is_file() and _include(q)),
                key=lambda q: (q.name != "solution.xlsx", str(q)),
            )
        ]

    def _build_attempt_package(self, workspace: Path, timestamp: str) -> Path:
        """Copy everything that reproduces this attempt into run_logs/.

        Layout of run_logs/attempt-{model}-{timestamp}/:
          <workspace root files>   starting files + solution.xlsx
          agent_logs/              requests csv, task.json, transcript.md, ...
          prompts/                 the exact system prompt + task template(s)
          config/                  the batch config this run was launched with

        Built before any upload, so the local record survives an S3 or DB
        failure — and survives cleanup_workspace deleting the workspace.
        """
        package_dir = RUN_LOGS_DIR / f"attempt-{self._model_slug()}-{timestamp}"
        package_dir.mkdir(parents=True, exist_ok=True)

        for f in workspace.iterdir():
            if f.is_file():
                shutil.copy2(f, package_dir / f.name)
        agent_logs = workspace / "agent_logs"
        if agent_logs.exists():
            shutil.copytree(agent_logs, package_dir / "agent_logs", dirs_exist_ok=True)

        prompts_dir = package_dir / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        for p in dict.fromkeys([SYSTEM_PROMPT_PATH, TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH]):
            if p.exists():
                shutil.copy2(p, prompts_dir / p.name)

        config_dir = package_dir / "config"
        config_dir.mkdir(exist_ok=True)
        shutil.copy2(self.config_path, config_dir / Path(self.config_path).name)

        return package_dir

    def upload_result(self, task_info: TaskInfo, workspace_path: str, workspace_result: WorkspaceResult):
        """Package the attempt, keep a local copy, upload to S3, insert the DB row.

        Local (both benchmarks): run_logs/attempt-{model}-{ts}/ — see
        _build_attempt_package.

        S3, benchmark v2: the whole package mirrors to one folder per attempt,
          {root}/attempts/cli_agents/{model}/task_id={id}/{ts}/...
        S3, benchmark v1: the historical flat Hive-style keys are kept so the
          existing v1 cohort's layout stays uniform,
          {root}/attempts/{model}_openpyxl/task_source={src}/task_id={id}/{ts}_file
        """
        agent_model_name = self.config['agent_model_name']
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        workspace = Path(workspace_path)

        package_dir = self._build_attempt_package(workspace, timestamp)
        print(f"  📂 Local attempt record: {package_dir}")

        attempt_files = []
        files_to_upload = []

        if str(self.config.get('benchmark', '')).lower() == 'v2':
            s3_base = (f"{self._s3_root}/attempts/cli_agents/{self._model_slug()}"
                       f"/task_id={task_info.task_id}/{timestamp}")
            for local_path, rel in self._v2_upload_files(package_dir):
                files_to_upload.append((local_path, f"{s3_base}/{rel}"))
        else:
            s3_base = f"{self._s3_task_prefix(task_info)}/{timestamp}"

            # solution.xlsx
            solution_path = workspace / "solution.xlsx"
            if solution_path.exists():
                files_to_upload.append((solution_path, f"{s3_base}_solution.xlsx"))

            # Find agent_logs directory and its contents
            agent_logs = workspace / "agent_logs"
            if agent_logs.exists():
                # openai_requests.csv (always at agent_logs level)
                csv_path = agent_logs / "openai_requests.csv"
                if csv_path.exists():
                    files_to_upload.append((csv_path, f"{s3_base}_openai_requests.csv"))

                # task.json and transcript.md (inside task_id subdirectory)
                for task_dir in sorted(agent_logs.iterdir()):
                    if task_dir.is_dir() and task_dir.name.startswith("task_"):
                        task_json = task_dir / "task.json"
                        if task_json.exists():
                            files_to_upload.append((task_json, f"{s3_base}_task.json"))

                        transcript = task_dir / "transcript.md"
                        if transcript.exists():
                            files_to_upload.append((transcript, f"{s3_base}_transcript.md"))

        # Upload files to S3
        for local_path, s3_key in files_to_upload:
            try:
                self.s3_client.upload_file(str(local_path), self._s3_bucket, s3_key)
                s3_uri = f"s3://{self._s3_bucket}/{s3_key}"
                attempt_files.append(s3_uri)
                print(f"  📤 {local_path.name} -> {s3_uri}")
            except Exception as e:
                print(f"  ❌ Failed to upload {local_path.name}: {e}")

        # Times - direct from execution
        start_time_dt = datetime.fromtimestamp(workspace_result.start_time) if workspace_result.start_time else datetime.now()
        end_time_dt = datetime.fromtimestamp(workspace_result.end_time) if workspace_result.end_time else datetime.now()
        time_taken_min = workspace_result.duration_seconds / 60.0 if workspace_result.duration_seconds > 0 else 0.0

        # Cost - direct from execution (use 0 if not available, never None since column is nullable)
        total_cost = workspace_result.cost_usd if workspace_result.cost_usd > 0 else 0.0

        # Determine failure status. Hitting the iteration cap is NOT a
        # failure: the workbook was still built and uploaded, and it gets
        # judged as-is. This matches the prior benchmark wave's rows
        # (agent_failed=false with reason "Max iterations (N) reached" —
        # 215 such rows at prompt_version 1105).
        agent_failed = workspace_result.status != "success"
        agent_failed_reason = workspace_result.error_message if agent_failed else None
        if agent_failed and (agent_failed_reason or "").startswith("Max iterations"):
            agent_failed = False

        # Compute prompt_version from versioned file paths
        template_path = TASK_TEMPLATE_WSP_PATH if task_info.task_source == "wsp" else TASK_TEMPLATE_FMWC_PATH
        prompt_version = parse_prompt_version(SYSTEM_PROMPT_PATH, template_path)

        # Create TaskAttempt in DB
        prompt_files = self._prompt_s3_uris or []

        attempt = TaskAttempt(
            task_id=task_info.task_id,
            prompt_files=prompt_files,
            start_time=start_time_dt,
            end_time=end_time_dt,
            agent_model_name=agent_model_name,
            agent_model_type="api",
            attempt_files=attempt_files,
            time_taken_min=time_taken_min,
            cost=total_cost,
            agent_failed=agent_failed,
            agent_failed_reason=agent_failed_reason,
            deprecated=False,
            prompt_version=prompt_version,
            context_reduced=workspace_result.context_reduced,
        )

        db = SessionLocal()
        try:
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            print(f"  ✅ Created TaskAttempt (ID: {attempt.id}, cost: ${total_cost:.4f})")
            self._write_extra_configs(db, attempt.id)
            if self._extra_configs_supported:
                print(f"  📌 extra_configs recorded: {self._identity.extra_configs()}")
        except Exception as e:
            db.rollback()
            print(f"  ❌ Failed to create TaskAttempt: {e}")
            traceback.print_exc()
        finally:
            db.close()

    def run_batch(self) -> BatchResult:
        """Execute automated batch: resolve tasks -> setup -> execute -> upload."""
        batch_start_time = time.time()

        # Load configuration
        config = self.load_config()

        # Fail fast if S3 is unreachable (e.g. no AWS credentials in the
        # environment). A batch that cannot download starting files must not
        # start: silent download failures previously produced empty-context
        # attempts that looked normal in the DB.
        self._verify_s3_access()

        # Fail fast if the LibreOffice recalc engine can't start (unless the
        # config sets allow_recalc_fallback), and record which engine this
        # batch runs so every attempt row carries the provenance.
        self._verify_recalc_engine()

        # Setup batch logging directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_logs_dir = Path("batch_logs") / f"batch_{timestamp}"
        self.batch_logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n📂 Batch logs directory: {self.batch_logs_dir}")

        # Upload prompt snapshots once
        print(f"\n📤 Uploading prompt snapshots...")
        self.upload_prompts()

        # Resolve tasks from DB
        task_infos = self.resolve_tasks()
        if not task_infos:
            print("⚠️  No tasks to process. Exiting.")
            return BatchResult(
                batch_name=config['batch_name'],
                total_workspaces=0,
                successful=0,
                failed=0,
                workspace_results=[],
                total_duration_seconds=time.time() - batch_start_time,
                aggregated_tokens=0,
                aggregated_iterations=0,
                aggregated_cost_usd=0.0,
            )

        # Process each task
        workspace_results = []
        skipped_count = 0

        for idx, task_info in enumerate(task_infos):
            print(f"\n{'='*80}")
            print(f"📦 Task {idx + 1}/{len(task_infos)}: {task_info.task_name}")
            print(f"{'='*80}")

            # Check trial count
            if self.should_skip(task_info):
                skipped_count += 1
                continue

            workspace_path = None
            try:
                # Setup workspace (download from S3)
                workspace_path = self.setup_workspace(task_info)

                # Select appropriate task template
                task_template = self.select_task_template(task_info.task_source)
                self.config['task_template'] = task_template

                # Detect workspace files
                ws_config = self.detect_workspace_files(workspace_path)

                # Process workspace (inherited from BatchRunner)
                result = self.process_workspace(ws_config)
                workspace_results.append(result)

                # Upload result to S3 + create DB entry
                print(f"\n  📤 Uploading results...")
                self.upload_result(task_info, workspace_path, result)

                # Cleanup workspace (everything uploaded to S3)
                self.cleanup_workspace(workspace_path)

            except Exception as e:
                print(f"  💥 Error processing task '{task_info.task_name}': {e}")
                traceback.print_exc()
                workspace_results.append(WorkspaceResult(
                    workspace_path=workspace_path or "unknown",
                    status="error",
                    pdf_files=[],
                    excel_files=[],
                    task_id=None,
                    iterations=0,
                    total_tokens=0,
                    cost_usd=0.0,
                    error_message=str(e),
                    duration_seconds=0,
                    final_result=None,
                ))

        # Calculate aggregated metrics
        total_duration = time.time() - batch_start_time
        successful = sum(1 for r in workspace_results if r.status == "success")
        failed = len(workspace_results) - successful
        total_tokens = sum(r.total_tokens for r in workspace_results)
        total_iterations = sum(r.iterations for r in workspace_results)
        total_cost = sum(r.cost_usd for r in workspace_results)

        batch_result = BatchResult(
            batch_name=config['batch_name'],
            total_workspaces=len(workspace_results),
            successful=successful,
            failed=failed,
            workspace_results=workspace_results,
            total_duration_seconds=total_duration,
            aggregated_tokens=total_tokens,
            aggregated_iterations=total_iterations,
            aggregated_cost_usd=total_cost,
        )

        # Generate reports (inherited)
        self.generate_reports(batch_result)

        # Print skip info
        if skipped_count > 0:
            print(f"⏭️  Skipped (trials exhausted): {skipped_count}")

        return batch_result


def run_auto_batch_from_config(config_path: str, server_path: str, api_key: str,
                                custom_reasoning: bool = False, enable_langfuse: bool = False) -> BatchResult:
    """Entry point for auto batch execution."""
    runner = AutoBatchRunner(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
    return runner.run_batch()
