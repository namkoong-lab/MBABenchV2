#!/usr/bin/env python3
"""
Auto Batch Runner for Excel CLI Agent

Automated pipeline that handles the full lifecycle:
  DB lookup -> trial count check -> S3 download -> task execution -> S3 upload -> DB entry

Extends BatchRunner with auto-discovery, S3 workspace setup, and result upload.

OFFLINE MODE. Each end of that pipeline has a local counterpart — the task
bundle in data/ (local_source.py) and the attempt tree in outputs/
(local_sink.py) — so the same experiment runs with no database and no object
store. A batch config selects them explicitly with `source: local` /
`sink: local`; saying nothing keeps today's behaviour whenever a database
URL resolves, and falls back to local only when none does. Nothing else
changes: the task objects, the selection rules, the prompts, the attachments
and the recorded columns are the database path's.
"""

import fcntl
import json
import os
import shutil
import time
import traceback
import yaml
import boto3
from boto3.s3.transfer import TransferConfig
from contextlib import contextmanager
from sqlalchemy import inspect as sa_inspect, text as sa_text
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date

from .batch_runner import BatchRunner, WorkspaceConfig, WorkspaceResult, BatchResult
from .models_config import DEFAULT_MAX_ITERATIONS

from .agent_identity import resolve_agent_identity
from .db import database as db_config
from .db.database import SessionLocal
from .db.models import Task, TaskAttempt
from .local_sink import LocalAttemptSink, iso
from .local_source import BundleNotAvailable, LocalTaskSource
from .repo_config import (
    boto3_credentials, data_root, describe_database_target,
    describe_local_target, monorepo_root, output_root, repo_value,
    resolve_attachments, resolve_db_url, to_repo_relative,
)
from .prompt_versions import (
    PROMPTS_DIR, PROMPT_VERSIONS, DEFAULT_PROMPT_VERSION, DEFAULT_V2_PROMPT_VERSION,
    attachment_names_for, attachments_for, parse_prompt_version, rubric_for_prompt_version,
    system_prompt_file,
)
from .models_config import uses_gemini_tool_calls

# Resolved prompt paths (set by load_config based on prompt_version)
SYSTEM_PROMPT_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["system"]
TASK_TEMPLATE_FMWC_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["fmwc"]
TASK_TEMPLATE_WSP_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["wsp"]

# Local per-attempt record, kept even after the workspace is cleaned up:
# run_logs/attempt-{model}-{timestamp}/ at the cli-agents-master root.
RUN_LOGS_DIR: Path = Path(__file__).resolve().parents[1] / "run_logs"

# Result uploads share the uplink with every lane's model calls - and, on the maintainer's
# MacBook, with the coding pipeline's Codex lanes. There, on 2026-09-20, all 36
# API streams that dropped fell inside S3 upload windows (big uploads reach only
# 0.3-0.8 MB/s on that Mac; a 209 MB one caused a 6-min storm). So an attempt's
# uploads wait for the coding pipeline's own cross-lane lock (the same file, so
# the two pipelines take turns) and go out as one 250 KB/s stream - exactly the
# rule in coding-agents-master/coding_agent/recorder.py. One stream, not boto3's
# default ten: ten threads sharing 250 KB/s left connections idle past S3's 20 s
# limit there. The run needs far less on average (CLI attempts: median 9 MB).
S3_UPLOAD_MAX_BYTES_PER_SEC = 250_000
S3_UPLOAD_LOCK_WAIT_SECONDS = 45 * 60  # then upload anyway: a wedged lane must not block the rest


def _s3_upload_lock_path() -> Path:
    return monorepo_root() / "coding-agents-master" / "workspaces" / ".s3_upload.lock"


@contextmanager
def _upload_slot(lock_path: Path):
    """Hold the cross-pipeline upload lock (flock: released when the holder
    exits, however it exits). A lock that cannot be opened never blocks an
    upload - the files matter more than the turn-taking."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")
    except OSError as e:
        print(f"  ⚠️ Upload lock unavailable ({e}); uploading without it")
        yield
        return
    try:
        deadline = time.monotonic() + S3_UPLOAD_LOCK_WAIT_SECONDS
        waited = False
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(f"  ⚠️ Upload lock still held after {S3_UPLOAD_LOCK_WAIT_SECONDS // 60} min; uploading anyway")
                    break
                if not waited:
                    print("  ⏳ Another lane is uploading; waiting for the upload lock")
                    waited = True
                time.sleep(2)
        yield
    finally:
        lock_file.close()


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
    # load_config) so a v1 config can never write to the SpreadsheetSmith DB or
    # vice versa. The bucket comes from the same config (aws.s3_bucket).
    # Prompts pair with the benchmark by default: `prompt_version` still
    # picks the prompt set, but load_config defaults it from `benchmark` and
    # refuses a prompt set embedding the other benchmark's rubric
    # (EXCEL_AGENT_SKIP_RUBRIC_GUARD=1 overrides).
    # data_root / output_root are the offline counterparts of root/db_name:
    # where a run with no database and no object store reads its tasks and
    # writes its attempts (data/README.md), overridable per machine through
    # local.data_root / local.output_root in config/config.yaml and per run
    # through SPREADSHEETSMITH_DATA_ROOT / SPREADSHEETSMITH_OUTPUT_ROOT. `bundled` says
    # whether inputs for that benchmark exist under data_root at all: only
    # the v2 task set ships with the repository, so a v1 config asking for
    # the local source is refused rather than run against an empty folder.
    BENCHMARKS = {
        "v1": {"root": "BizbenchV1", "db_name": "BizbenchV1",
               "data_root": "data", "output_root": "outputs", "bundled": False},
        "v2": {"root": "SpreadsheetSmith", "db_name": "SpreadsheetSmith",
               "data_root": "data", "output_root": "outputs", "bundled": True},
    }

    # Offline wiring, decided by load_config (see _select_storage). Declared
    # on the class, not just in __init__, because the guard tests exercise
    # setup_workspace on a bare object.__new__(AutoBatchRunner) — the cloud
    # path must stay the default for a runner that never loaded a config.
    _source_local = False
    _sink_local = False
    _local_source: Optional[LocalTaskSource] = None
    _local_sink: Optional[LocalAttemptSink] = None
    _prompt_local_files: Optional[List[str]] = None

    def __init__(self, config_path: str, server_path: str, api_key: str,
                 custom_reasoning: bool = False, enable_langfuse: bool = False):
        super().__init__(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
        self._s3_client = None
        self._prompt_s3_uris: Optional[List[str]] = None
        # Placeholders until load_config resolves them from the benchmark
        # key and the monorepo config. The bucket has no default: a run that
        # needs one and cannot name it must say so, not guess (see
        # load_config); a fully local run never resolves one at all.
        self._s3_bucket = None
        self._s3_root = "BizbenchV1"
        self._identity = None
        self._extra_configs_supported = False
        # Offline wiring, decided by load_config (see _select_storage).
        self._source_local = False
        self._sink_local = False
        self._local_source: Optional[LocalTaskSource] = None
        self._local_sink: Optional[LocalAttemptSink] = None
        self._prompt_local_files: Optional[List[str]] = None

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
        config.setdefault('max_iterations', DEFAULT_MAX_ITERATIONS)
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
                "BizbenchV1 wave, 'v2' = SpreadsheetSmith task set). It selects "
                "the database, S3 root, and default prompt set together."
            )
        benchmark = str(config['benchmark']).lower()
        if benchmark not in self.BENCHMARKS:
            raise ValueError(
                f"Unknown benchmark '{benchmark}'. Available: "
                f"{list(self.BENCHMARKS.keys())} (v1 = BizbenchV1 wave, "
                f"v2 = SpreadsheetSmith task set)"
            )
        bench = self.BENCHMARKS[benchmark]
        self._s3_root = bench["root"]
        db_url, db_source = self._select_storage(config, benchmark, bench)

        # The bucket is resolved only for a run that uses one, and only from
        # the config — a hardcoded default would send a wave to whichever
        # bucket happened to be named in the source, which is exactly the
        # kind of silent mis-targeting the benchmark guard above prevents.
        if not (self._source_local and self._sink_local):
            self._s3_bucket = repo_value("aws", "s3_bucket")
            if not self._s3_bucket:
                raise ValueError(
                    "No object-store bucket: set aws.s3_bucket in "
                    "<SpreadsheetSmith>/config/config.yaml. It has no default. To "
                    "run without an object store at all, set source: local "
                    "and sink: local (the bundle in data/, attempts into "
                    "outputs/)."
                )

        if not self._source_local or not self._sink_local:
            db_config.configure(benchmark)
            # The monorepo layer is selected by benchmark and can't mismatch;
            # this guards the blind $DATABASE_URL fallback.
            if f"/{bench['db_name']}" not in db_url:
                raise ValueError(
                    f"benchmark={benchmark} expects a {bench['db_name']} "
                    f"database, but the URL from {db_source} points elsewhere. "
                    f"Export the {bench['db_name']} connection string (or fix "
                    f"the config's benchmark key) before running."
                )
        if not self._sink_local:
            # task_attempts.extra_configs (JSONB) exists in SpreadsheetSmith only. It
            # is deliberately not on the ORM model: a mapped column missing from
            # one database breaks every SELECT there (see db/models.py), so the
            # runner probes for it and writes it with plain SQL after the insert.
            self._extra_configs_supported = self._probe_extra_configs_column()
            if not self._extra_configs_supported:
                print(f"⚠️  task_attempts.extra_configs not present in the {bench['db_name']} "
                      f"database — run settings will NOT be recorded on the row "
                      f"(only in the local attempt package and this log).")
        else:
            # A local row carries the whole column set unconditionally; there
            # is no older schema to probe for.
            self._extra_configs_supported = True

        # Resolve prompt version from config; the default follows the
        # benchmark (v1 -> DEFAULT_PROMPT_VERSION, v2 -> the v2-rubric set),
        # and an explicit choice must embed this benchmark's rubric — running
        # v2 tasks against prompts that promise the v1 grading rubric (or
        # vice versa) invalidates the wave. Set EXCEL_AGENT_SKIP_RUBRIC_GUARD=1
        # for a deliberate cross-benchmark experiment (logged loudly).
        global SYSTEM_PROMPT_PATH, TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH
        default_ver = DEFAULT_V2_PROMPT_VERSION if benchmark == "v2" else DEFAULT_PROMPT_VERSION
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
                suggestion = DEFAULT_V2_PROMPT_VERSION if benchmark == "v2" else "v11"
                raise ValueError(
                    f"prompt_version {prompt_ver} embeds the {prompt_rubric} "
                    f"grading rubric, but benchmark={benchmark} tasks are graded "
                    f"with the {benchmark} rubric. Use prompt_version "
                    f"{suggestion} (or omit the key to default it from the "
                    f"benchmark); set EXCEL_AGENT_SKIP_RUBRIC_GUARD=1 to force "
                    f"a cross-benchmark run."
                )
        ver_files = PROMPT_VERSIONS[prompt_ver]
        # The set's system prompt - or, for Gemini 3.8 Flash alone, its
        # function-call variant (prompt_versions.MODEL_SYSTEM_PROMPT_VARIANTS);
        # parse_prompt_version still records the set's version for it.
        SYSTEM_PROMPT_PATH = PROMPTS_DIR / system_prompt_file(
            prompt_ver, self._identity.model, require_variant=uses_gemini_tool_calls(self._identity.model))
        TASK_TEMPLATE_FMWC_PATH = PROMPTS_DIR / ver_files["fmwc"]
        TASK_TEMPLATE_WSP_PATH = PROMPTS_DIR / ver_files["wsp"]

        # Set versioned system prompt path for TaskExecutor
        config['system_prompt_path'] = str(SYSTEM_PROMPT_PATH)

        # The prompt version — never the config — names the files shipped
        # with every workspace (house standards). Resolved now so a missing
        # file fails the batch before any task is claimed.
        self._attachments = resolve_attachments(attachments_for(prompt_ver))
        self._attachment_names = attachment_names_for(prompt_ver)

        self.config = config

        print(f"✅ Configuration loaded: {config['batch_name']}")
        print(f"   Model: {config['model']}")
        print(f"   Benchmark: {benchmark}")
        if self._source_local and self._sink_local:
            print(f"   Storage: LOCAL (no database, no object store)")
            print(f"   Local: {describe_local_target(bench)}")
        else:
            if self._source_local:
                print(f"   Tasks: LOCAL bundle {self._local_source.tasks_dir}")
            if self._sink_local:
                print(f"   Attempts: LOCAL {self._local_sink.cohort_dir}")
            print(f"   Database: {describe_database_target(benchmark)}")
            print(f"   S3: s3://{self._s3_bucket}/{self._s3_root}/")
        print(f"   Agent model name: {config['agent_model_name']} (agent_identities.yaml)")
        print(f"   Pinned by identity: {identity.settings()}")
        print(f"   extra_configs column: {'yes' if self._extra_configs_supported else 'NO'}")
        print(f"   Prompt version: {prompt_ver}")
        print(f"   Attachments: {[self._delivered_name(p) for p in self._attachments] or 'none'}")
        print(f"   Max iterations: {config['max_iterations']}")
        print(f"   Max trials: {config['max_trials']}")
        print(f"   Trials since: {config['trials_since']}")
        print(f"   Workspace base: {config.get('workspace_base_dir') or 'batch_logs/batch_<ts>/workspaces (default)'}")

        return config

    # Batch-config values for `source:` / `sink:`. The cloud spellings are
    # accepted so a config can pin today's behaviour explicitly instead of
    # relying on a URL being configured.
    _SOURCE_MODES = {"local", "postgres", "db", "database"}
    _SINK_MODES = {"local", "postgres", "db", "database", "s3"}

    def _select_storage(self, config: Dict[str, Any], benchmark: str,
                        bench: Dict[str, Any]) -> Tuple[str, str]:
        """Decide where tasks are read from and attempts are written to.

        THE RULE, in the order it is applied:

        * `source: local` / `sink: local` in the batch config is obeyed, full
          stop — that is how a reviewer runs offline on a machine that does
          have credentials configured.
        * Otherwise, if a database URL resolves for this benchmark, behaviour
          is exactly what it has always been: the database and the object
          store. A configured URL is never silently bypassed — going offline
          behind the operator's back would write a wave into outputs/ that
          nobody would think to look for.
        * Only when nothing is said AND no URL resolves does the run go local
          on its own, saying so on the way in. That is the standalone
          checkout: no config/config.yaml, no DATABASE_URL, just the bundle.

        Returns the (url, provenance) pair load_config goes on to validate,
        which is ("", "unresolved") on a fully local run.
        """
        for key, allowed in (("source", self._SOURCE_MODES), ("sink", self._SINK_MODES)):
            value = config.get(key)
            if value is not None and str(value).lower() not in allowed:
                raise ValueError(
                    f"Unknown {key} '{value}'. Available: {sorted(allowed)} "
                    f"('local' = the bundle in data/ and the tree in outputs/; "
                    f"anything else = the database and the object store)."
                )
        source = str(config.get("source") or "").lower() or None
        sink = str(config.get("sink") or "").lower() or None

        db_url, db_source = resolve_db_url(benchmark)
        offline_default = not db_url

        self._source_local = source == "local" or (source is None and offline_default)
        self._sink_local = sink == "local" or (sink is None and offline_default)

        if (not self._source_local or not self._sink_local) and not db_url:
            asked = "source" if not self._source_local else "sink"
            raise ValueError(
                f"No database URL for benchmark={benchmark}, but the config's "
                f"`{asked}` asks for the database. Set database.{benchmark}_url "
                f"in <SpreadsheetSmith>/config/config.yaml (or export DATABASE_URL), "
                f"or set source: local / sink: local to run from the bundle in "
                f"data/ and write to outputs/."
            )

        if self._source_local:
            self._local_source = LocalTaskSource(
                data_root(bench.get("data_root", "data")),
                benchmark=benchmark,
                bundled=bool(bench.get("bundled")),
            )
            # Fail here, before any task is claimed, rather than at the first
            # workspace: an unbundled benchmark or a missing bundle is a
            # setup mistake, not a per-task one.
            self._local_source.load_all()
        if self._sink_local:
            self._local_sink = LocalAttemptSink(
                output_root(bench.get("output_root", "outputs")),
                config["agent_model_name"],
            )

        if offline_default and source is None and sink is None:
            print("🔌 No database URL resolved for this benchmark — running "
                  "offline: tasks from the bundle in data/, attempts into "
                  "outputs/ (set source:/sink: in the config to pin either).")

        return db_url, db_source

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
        cfg.update(self._run_limit_extra_configs())
        cfg.update(self._recalc_extra_configs())
        cfg.update(self._attachment_extra_configs())
        cfg.update(self._response_contract_extra_configs())
        db.execute(
            sa_text("UPDATE task_attempts SET extra_configs = CAST(:cfg AS jsonb) WHERE id = :id"),
            {"cfg": json.dumps(cfg), "id": attempt_id},
        )
        db.commit()

    def resolve_tasks(self) -> List[TaskInfo]:
        """The tasks to process, from the database or the local bundle."""
        if self._source_local:
            return self._apply_skip_list(self._resolve_tasks_local())
        db = SessionLocal()
        try:
            return self._apply_skip_list(self._resolve_tasks_impl(db))
        finally:
            db.close()

    def _apply_skip_list(self, tasks_result: List[TaskInfo]) -> List[TaskInfo]:
        """Drop `skip_task_ids` and announce the total — shared by both
        sources so a lane's selection cannot depend on where it read."""
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

    def _resolve_tasks_local(self) -> List[TaskInfo]:
        """The bundle's answer to the three selection modes.

        Deliberately mirrors _resolve_tasks_impl clause for clause — same
        modes, same precedence, same skips, same log lines — so the set a
        lane claims cannot depend on which source it read. The only
        difference is that task_starting_files hold local paths.
        """
        source = self._local_source
        tasks_result: List[TaskInfo] = []

        def info(task) -> TaskInfo:
            return TaskInfo(
                task_id=task.id,
                task_name=task.task_name,
                task_source=task.task_source,
                task_starting_files=list(task.task_starting_files or []),
            )

        if 'task_ids' in self.config:
            task_ids = self.config['task_ids']
            print(f"\n🔍 Resolving {len(task_ids)} explicit task id(s) from the local bundle...")
            for tid in task_ids:
                task = source.get(tid)
                if task is None:
                    print(f"  ❌ id {tid} -> NOT FOUND in the bundle")
                    continue
                if task.deprecated:
                    print(f"  ⏭️  id {tid} ({task.task_name}) -> deprecated, skipping")
                    continue
                tasks_result.append(info(task))
                print(f"  ✅ id {tid} -> {task.task_name} ({task.task_source})")

        elif 'tasks' in self.config:
            task_names = self.config['tasks']
            print(f"\n🔍 Resolving {len(task_names)} explicit task(s) from the local bundle...")
            for name in task_names:
                task = source.find_by_name(name)
                if task:
                    tasks_result.append(info(task))
                    print(f"  ✅ {name} -> ID {task.id} ({task.task_source})")
                else:
                    print(f"  ❌ {name} -> NOT FOUND in the bundle")

        elif 'task_filter' in self.config:
            tf = self.config['task_filter']
            print(f"\n🔍 Auto-discovering tasks from the local bundle...")
            task_source = tf.get('task_source')
            if task_source:
                print(f"  Filter: task_source = {task_source}")
            all_tasks = source.filter(task_source=task_source)
            print(f"  Found {len(all_tasks)} eligible tasks in the bundle")

            if tf.get('missing_for_model'):
                trials_since = self.config['trials_since']
                max_trials = self.config['max_trials']
                for task in all_tasks:
                    # get_trial_count, not the local counter directly: a
                    # config may read tasks locally while still recording to
                    # the database, and the count must come from wherever
                    # this lane's rows land.
                    if self.get_trial_count(info(task)) < max_trials:
                        tasks_result.append(info(task))
                print(f"  After trial filter (< {max_trials} trials since {trials_since}): "
                      f"{len(tasks_result)} tasks")
            else:
                tasks_result.extend(info(t) for t in all_tasks)

        return tasks_result

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

    def _local_trial_count(self, task_id: int) -> int:
        """Trial count from outputs/<label>/task_attempts.jsonl."""
        return self._local_sink.trial_count(task_id, self.config['trials_since'])

    def get_trial_count(self, task_info: TaskInfo) -> int:
        """Public interface: get trial count for a task."""
        if self._sink_local:
            return self._local_trial_count(task_info.task_id)
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
        task+agent since trials_since.

        Offline this reads the JSONL the sink appends to, which is what
        makes a relaunched local lane resume instead of re-running.
        """
        if self._sink_local:
            return self._local_sink.has_success(task_info.task_id,
                                                self.config['trials_since'])
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

        for source_ref in task_info.task_starting_files:
            filename = source_ref.split("/")[-1]
            local_path = workspace / filename

            if local_path.exists():
                print(f"  ✅ Already exists: {filename}")
                continue

            if self._source_local:
                # The bundled copy is byte-for-byte the object the cloud path
                # downloads (data/MANIFEST.json records both hashes), so the
                # workspace the agent sees is the same either way.
                src_path = Path(source_ref)
                try:
                    print(f"  📦 Staging from the bundle: {filename}")
                    shutil.copy2(src_path, local_path)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to copy {src_path} for '{task_info.task_name}': {e}. "
                        "Aborting this task — running without starting files would "
                        "produce an invalid (empty-context) attempt."
                    ) from e
            else:
                # Parse s3://bucket/key
                if not source_ref.startswith("s3://"):
                    raise RuntimeError(f"Invalid S3 URI for '{task_info.task_name}': {source_ref}")

                parts = source_ref[5:].split("/", 1)
                if len(parts) != 2:
                    raise RuntimeError(f"Invalid S3 URI format for '{task_info.task_name}': {source_ref}")

                bucket = parts[0]
                key = parts[1]

                try:
                    print(f"  ⬇️  Downloading: {filename}")
                    self.s3_client.download_file(bucket, key, str(local_path))
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download {source_ref} for '{task_info.task_name}': {e}. "
                        "Aborting this task — running without starting files would "
                        "produce an invalid (empty-context) attempt."
                    ) from e

            verb = "Copied" if self._source_local else "Downloaded"
            size = local_path.stat().st_size
            if size == 0:
                raise RuntimeError(
                    f"{verb} zero-byte file {filename} from {source_ref} for "
                    f"'{task_info.task_name}'; refusing to run on an empty starting file"
                )
            print(f"  ✅ {verb}: {filename} ({size:,} bytes)")

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

        # The prompt version's attachments (house standards) ride alongside
        # the starting files, under the bare name the prompt directive uses.
        self._copy_attachments(workspace)

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
                "<SpreadsheetSmith>/config/config.yaml, or the boto3 default chain: "
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
        if self._sink_local:
            return self._store_prompts_locally()

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

        # Upload task templates (both variants; v12+'s slots share one file,
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

        # Upload the attachments too: they are prompt text the agent saw, so
        # prompt_files must reproduce them alongside the system prompt.
        for attachment in self._attachments:
            s3_key = f"{prompts_prefix}/{timestamp}_{self._delivered_name(attachment)}"
            try:
                self.s3_client.upload_file(str(attachment), self._s3_bucket, s3_key)
                uri = f"s3://{self._s3_bucket}/{s3_key}"
                uris.append(uri)
                print(f"  📤 Uploaded attachment: {uri}")
            except Exception as e:
                print(f"  ⚠️  Failed to upload attachment {attachment.name}: {e}")

        self._prompt_s3_uris = uris
        return uris

    def _store_prompts_locally(self) -> List[str]:
        """Snapshot this batch's prompts under outputs/<label>/prompts/.

        The mirror of upload_prompts' S3 tree, same contents in the same
        order — system prompt, the task template(s), then the prompt
        version's attachments — so prompt_files says the same thing whichever
        sink recorded the row. Written once per batch and reused by every
        attempt in it.
        """
        if self._prompt_local_files is not None:
            return self._prompt_local_files

        prompts_dir = self._local_sink.cohort_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored: List[str] = []

        sources: List[Tuple[Path, str]] = []
        if SYSTEM_PROMPT_PATH.exists():
            sources.append((SYSTEM_PROMPT_PATH, SYSTEM_PROMPT_PATH.name))
        # v12+ point both slots at one file; dedupe so prompt_files has no repeats.
        for template_path in dict.fromkeys([TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH]):
            if template_path.exists():
                sources.append((template_path, template_path.name))
        for attachment in self._attachments:
            sources.append((attachment, self._delivered_name(attachment)))

        for src_path, name in sources:
            dest = prompts_dir / f"{timestamp}_{name}"
            shutil.copy2(src_path, dest)
            rel = to_repo_relative(dest)
            stored.append(rel)
            print(f"  📝 Prompt snapshot: {rel}")

        self._prompt_local_files = stored
        return stored

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
            if p.name.startswith('.'):
                # a save killed mid-write can leave .solution.xlsx.tmp-<pid> behind
                # (see _save_workbook_sync); dotfiles are never part of an attempt.
                return False
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

    def _upload_files(self, files_to_upload: List[Tuple[Path, str]]) -> List[str]:
        """Upload one attempt's files under the cross-pipeline upload lock, as
        one throttled stream (see S3_UPLOAD_MAX_BYTES_PER_SEC). Returns the
        s3:// URIs that made it."""
        throttle = TransferConfig(max_bandwidth=S3_UPLOAD_MAX_BYTES_PER_SEC, max_concurrency=1)
        uploaded = []
        with _upload_slot(_s3_upload_lock_path()):
            for local_path, s3_key in files_to_upload:
                try:
                    self.s3_client.upload_file(str(local_path), self._s3_bucket, s3_key, Config=throttle)
                    s3_uri = f"s3://{self._s3_bucket}/{s3_key}"
                    uploaded.append(s3_uri)
                    print(f"  📤 {local_path.name} -> {s3_uri}")
                except Exception as e:
                    print(f"  ❌ Failed to upload {local_path.name}: {e}")
        return uploaded

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

        if self._sink_local:
            # Same file set, same order as the v2 object-store layout — the
            # sink only changes where it lands.
            attempt_files = self._local_sink.store_files(
                self._v2_upload_files(package_dir), task_info.task_id, timestamp)
            print(f"  💾 {len(attempt_files)} file(s) -> "
                  f"{self._local_sink.attempt_dir(task_info.task_id, timestamp)}")
        elif str(self.config.get('benchmark', '')).lower() == 'v2':
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

        if files_to_upload:
            attempt_files.extend(self._upload_files(files_to_upload))

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

        # Record the attempt
        prompt_files = (self._prompt_local_files if self._sink_local
                        else self._prompt_s3_uris) or []

        if self._sink_local:
            self._record_local_attempt(
                task_info=task_info,
                attempt_files=attempt_files,
                prompt_files=prompt_files,
                start_time_dt=start_time_dt,
                end_time_dt=end_time_dt,
                time_taken_min=time_taken_min,
                total_cost=total_cost,
                agent_failed=agent_failed,
                agent_failed_reason=agent_failed_reason,
                prompt_version=prompt_version,
                context_reduced=workspace_result.context_reduced,
            )
            return

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

    def _record_local_attempt(self, task_info: TaskInfo, attempt_files: List[str],
                              prompt_files: List[str], start_time_dt: datetime,
                              end_time_dt: datetime, time_taken_min: float,
                              total_cost: float, agent_failed: bool,
                              agent_failed_reason: Optional[str],
                              prompt_version: int,
                              context_reduced: Optional[bool]) -> None:
        """Append one task_attempts-shaped row to the cohort's JSONL.

        Every value is the one the database row would have carried, computed
        by the same code above this call; only `id` and the timestamps are
        produced here (see local_sink for both conventions). extra_configs is
        the identity's settings merged with the run limits, the recalc
        provenance and the attachment provenance — the same dict
        _write_extra_configs sends to the database.
        """
        extra_configs = dict(self._identity.extra_configs())
        extra_configs.update(self._run_limit_extra_configs())
        extra_configs.update(self._recalc_extra_configs())
        extra_configs.update(self._attachment_extra_configs())

        now = datetime.now().astimezone()
        row = self._local_sink.append_row({
            "id": self._local_sink.new_attempt_id(),
            "task_id": task_info.task_id,
            "agent_model_name": self.config['agent_model_name'],
            "agent_model_type": "api",
            "attempt_files": attempt_files,
            "prompt_files": prompt_files,
            "start_time": iso(start_time_dt),
            "end_time": iso(end_time_dt),
            "time_taken_min": time_taken_min,
            "cost": total_cost,
            "prompt_version": prompt_version,
            "agent_failed": agent_failed,
            "agent_failed_reason": agent_failed_reason,
            "deprecated": False,
            "created_at": iso(now),
            "context_reduced": context_reduced,
            "deprecated_reason": None,
            "updated_at": iso(now),
            "extra_configs": extra_configs,
        })
        print(f"  ✅ Recorded attempt (ID: {row['id']}, cost: ${total_cost:.4f}) "
              f"-> {to_repo_relative(self._local_sink.jsonl_path)}")
        print(f"  📌 extra_configs recorded: {self._identity.extra_configs()}")

    def run_batch(self) -> BatchResult:
        """Execute automated batch: resolve tasks -> setup -> execute -> upload."""
        batch_start_time = time.time()

        # Load configuration
        config = self.load_config()

        # Fail fast if S3 is unreachable (e.g. no AWS credentials in the
        # environment). A batch that cannot download starting files must not
        # start: silent download failures previously produced empty-context
        # attempts that looked normal in the DB. A fully local run never
        # opens a client, so there is nothing to verify — the bundle was
        # checked in load_config for the same reason, before any task is
        # claimed.
        if not (self._source_local and self._sink_local):
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

        # Snapshot the prompts once
        print(f"\n📤 Recording prompt snapshots...")
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

                # Store the attempt files + record the attempt row
                print(f"\n  📤 Recording results...")
                self.upload_result(task_info, workspace_path, result)

                # Cleanup workspace (everything is recorded by now)
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
