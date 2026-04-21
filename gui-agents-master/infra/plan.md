# AWS + DB-backed runtime for gui-agents-master

**Status:** Phase 0a + 0b + 0c + 1 shipped; Phase 2 in progress.
**Last updated:** 2026-04-21

## Goals

1. Run the existing web-GUI automation (Claude.ai / ChatGPT) on AWS, unattended.
2. Replace hand-written YAML task lists with a **pluggable task source** so the batch runner can be fed from any backend (local files, Postgres + S3, SQLite, a queue, ...).
3. Provide a **Bizbench-specific** source/sink implementation that reads `tasks` rows and writes `task_attempts` rows against the existing Neon Postgres DB + `biz-bench` S3 bucket, matching the conventions already used in `cli-agents-master/excel_cli_agent/auto_batch_runner.py` and `judge/operation_scripts/get_tasks.py`.
4. Touch the GUI engine as little as possible. The engine already accepts a task-config dict with `upload_files`, `solution_name`, etc. — keep that contract; only swap what produces those dicts and what consumes the resulting solution.

Non-goals: rewriting the engine, changing the prompt templates, or moving off Chrome+CDP.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  EC2 instance (Ubuntu, Xvfb + Chrome + systemd)                  │
│                                                                  │
│   ┌─────────────────────────────────────────────────┐            │
│   │  claude_web_batch_runner.py --source <kind>     │            │
│   │                                                 │            │
│   │   ┌──────────────┐    ┌──────────────────────┐  │            │
│   │   │ TaskSource   │───▶│ claude_web_engine.py │  │            │
│   │   │ .next_task() │    │ (unchanged)          │  │            │
│   │   └──────────────┘    └──────────────────────┘  │            │
│   │          ▲                       │              │            │
│   │          │                       ▼              │            │
│   │          │              ┌──────────────┐       │            │
│   │          │              │ AttemptSink  │       │            │
│   │          │              │ .publish()   │       │            │
│   │          └──────────────└──────────────┘       │            │
│   └─────────────────────────────────────────────────┘            │
└───────────────────────┬──────────────────────────────────────────┘
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
   ┌────────────────────┐   ┌────────────────────┐
   │ Neon Postgres      │   │ S3 (biz-bench)     │
   │  - tasks           │   │  - BizbenchV1/...  │
   │  - task_attempts   │   │    starting_files  │
   │                    │   │    attempts        │
   └────────────────────┘   └────────────────────┘
```

Everything inside the dashed box is **provider-agnostic**. The two interfaces (`TaskSource`, `AttemptSink`) are the only seam between the GUI pipeline and whatever backend you want to plug in.

---

## Part 1 — Generic interface (ship first)

### Directory layout

```
gui-agents-master/
├── claude_web_agent/                   # existing engine, unchanged
├── data/                               # local sample inputs for runs
│   └── sample/                         #   referenced by task YAMLs via relative paths
│       ├── MO15 Round 1 - Sec 2 - Bread and Butter - Workbook.xlsx
│       └── MO15 Round 1 - Sec 2 - Bread and Butter.pdf
├── task_io/                            # provider-agnostic seam
│   ├── __init__.py
│   ├── base.py                         # TaskSource, AttemptSink, TaskSpec, AttemptResult
│   ├── registry.py                     # cfg → build_source / build_sink
│   ├── sources/
│   │   ├── yaml_source.py              # reads whatever source.yaml_path points at (typically a file under run_configs/)
│   │   └── postgres_s3.py              # Bizbench impl (see Part 2)
│   └── sinks/
│       ├── local_sink.py
│       └── postgres_s3.py              # Bizbench impl (see Part 2)
└── infra/                              # orchestration layer
    ├── plan.md                         # this file
    ├── run.py                          # task-io-driven CLI (reads ONLY infra/configs/)
    └── configs/                        # single source of truth at runtime
        ├── __init__.py
        ├── loader.py
        ├── configs.default.yaml        # every project-wide knob, every default
        ├── configs.yaml                # sparse user overrides (gitignored)
        └── run_configs/                # "what to run this time" profiles — selected via --run-config
            ├── local_run_examples/
            │   ├── sample_task.yaml        # task-shaped: handed to YamlTaskSource
            │   └── sample_task_chatgpt.yaml # task-shaped, per-run switch to ChatGPT
            └── bizbench_run_examples/
                └── sample_bizbench.yaml     # overlay-shaped: postgres_s3 + filters

# Legacy, decoupled from infra/run.py but still used by claude_web_batch_runner.py:
tasks_configs/                          # templates + example task lists — DO NOT edit for new runs
```

**What each top-level directory is for:**

- [data/](../data/) — local input files referenced by per-task YAMLs. Paths in `upload_files` resolve against CWD (or `local_files_base` if set), so keeping sample inputs here lets task YAMLs use short relative paths like `data/sample/foo.xlsx`.
- [task_io/](../task_io/) — the provider-agnostic seam. Defines `TaskSource` / `AttemptSink` protocols and ships reference implementations. The engine pipeline never imports from `infra/`; it only sees the dicts the runner hands it.
- [infra/](./) — orchestration: config loader, CLI runner, per-task merge logic. This is the only layer that knows about `cfg` or `configs/`. Replacing it wouldn't require touching `task_io/` or `claude_web_agent/`.

### Configuration

**One source of truth.** All runtime config lives under [infra/configs/](configs/). `infra/run.py` reads nothing outside that directory. The legacy [tasks_configs/](../tasks_configs/) templates and example task lists are decoupled — they're used only by the legacy `claude_web_batch_runner.py` and should not be edited for new runs.

**Three files + one folder:**

| Path | Role | Checked in? |
|---|---|---|
| `configs.default.yaml` | Every project-wide knob and its default. Canonical schema. | yes |
| `configs.yaml` | Sparse long-lived overrides (env / machine — DB url, AWS, provider project ids). | no (gitignored) |
| `run_configs/*.yaml` | "What to run this time" profiles. Selected via `--run-config PATH`. Two flavors: *task-shaped* (handed to YamlTaskSource) or *overlay-shaped* (deep-merged as a 3rd project-wide layer). | yes (samples) |

**Merge order — three layers.** Every task in a run gets the same config:

1. `configs.default.yaml` — defaults
2. `configs.yaml` — long-lived user overrides
3. `--run-config <file>` — deep-merged as a project-wide layer on top of `configs.yaml` (applies to every task in this run)

Deep-merge at each step; later wins. There is **no per-task override layer**: all 50 tasks in a batch get the same `provider`, `prompts`, `claude_web:`, etc. If you want per-task variation, run separate invocations.

**`--run-config` routing.** When the file has any of `task_name`, `upload_files`, `files_to_upload`, `solution_name`, `skip`, `task_source`, `tasks` at its top level, it is treated as a *task-shaped* YAML: the runner strips those reserved keys, overlays the remaining keys as layer 3, and forces `source.kind='yaml'` with `source.yaml_path` pointing at the file so `YamlTaskSource` reads the reserved keys as the task definition. Otherwise it is a pure *overlay-shaped* YAML and is deep-merged at layer 3 as-is.

**Task YAML schema.** Reserved keys define the task; everything else at top level is a project-wide override. When a task-shaped `--run-config` is loaded, the two sets are split — reserved keys go to `YamlTaskSource`, the rest overlay at layer 3 (run-scoped).

| Reserved (task fields) | `task_name`, `upload_files`, `files_to_upload` (alias), `solution_name`, `skip`, `task_source` |
|---|---|
| Anything else | Overlaid at layer 3 — applies to every task in this run (e.g. `prompts:`, `claude_web:`, `chatgpt_web:`) |

Example — a task-shaped run-config that runs one task on Opus with a longer timeout:

```yaml
# infra/configs/run_configs/local_run_examples/opus_long_task.yaml
task_name: "Analyze_Annual_Report"
upload_files: ["data/annual_report.pdf"]
solution_name: "AR_Analysis"

# ── below: non-reserved keys overlay at layer 3 for this run ──
prompts:
  - "Extract every financial metric into a table."
  - "Build a DCF on a new sheet using formulas only."
claude_web:
  model: "opus_4_6"
  max_sec_per_task: 10800
```

Example — an overlay-shaped run-config that pulls Bizbench tasks from Postgres:

```yaml
# infra/configs/run_configs/bizbench_run_examples/modeloff_chatgpt.yaml
source:
  kind: postgres_s3
  schema: bizbench          # picks BizbenchPostgresS3TaskSource
  filters:
    task_sources: ["modeloff"]
    skip_already_attempted: true
provider:
  kind: "chatgpt"
agent:
  model_name: "chatgpt_web"
  agent_folder: "chatgpt_web"
```

**Loader semantics** ([loader.py](configs/loader.py)) — all three layers merged here; no task-level merge exists:

1. Read `configs.default.yaml`, optional `configs.yaml`, and the `--run-config` overlay data (either read from `run_config_path` or passed pre-parsed as `run_config_data` so callers can strip reserved task keys first).
2. Shape validation — unknown keys / malformed leaves raise `ConfigError` with paths. Each source is validated independently so errors name the offending file.
3. Deep-merge `configs.yaml` overrides then the run-config overrides into a single overrides dict (later wins), then apply to the schema.
4. `required: true` leaves must be truthy after merge.
5. `free_form: true` leaves skip shape validation (used for anything intentionally schema-less).
6. Collapse `{value, required}` to plain values; return nested `SimpleNamespace`.

**Where values live:**

| Scope | Lives in | Examples |
|---|---|---|
| Project-wide, long-lived | `configs.default.yaml` + `configs.yaml` | DB url, AWS, provider project ids, scratch paths |
| Invocation-scoped | `run_configs/*.yaml` (via `--run-config`) | `source.kind` + filters, provider switches, prompts, model overrides |
| Task identity (task-shaped run-config only) | Reserved keys in the same file | `task_name`, `upload_files`, `solution_name` |
| Secrets | `configs.yaml` OR env var named by `database.url_env` | DB connection string |

**What was cut from the previous iteration:**

- `provider.template_path` — deleted; no template file is loaded.
- `template:` group (`prompts`, `prompt_version`, `local_files_base`) — promoted to top-level of `configs.default.yaml`.
- `template_overrides:` escape hatch — deleted; per-task overrides now cover the same use case without a second merge path.
- First-class flat mirrors (`claude_web.output_folder_prefix`, etc.) — replaced by the full nested `claude_web:` block carrying every field the engine reads.
- `browser.{claude,chatgpt}_cdp_port` — moved inside each provider's own `browser:` section.

**Preflight in [run.py](run.py)** runs per-task, after the task-level merge, before touching Chrome:

- Scan for leftover placeholder strings (`your-project-id-here`, etc.) — means an override didn't reach it.
- Claude contract: `claude_web.model` non-null (null crashes the agent at `.lower()`).
- ChatGPT contract: `chatgpt_web.project_id` and `project_slug` set.
- Each `upload_files` entry must exist on disk, resolved the same way the engine resolves them (relative → against `local_files_base` if set, else CWD). Error hints at whether to set `local_files_base` or use an absolute path.

Any failure → `logger.error` each issue, `return 2`.

### The data contract

```python
# task_io/base.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Iterator, Any

@dataclass
class TaskSpec:
    """What the engine needs to run one task. Source-agnostic."""
    task_id: str                      # opaque ID; source chooses the format
    task_name: str
    upload_files: list[Path]          # local paths — source is responsible
                                      # for downloading remote blobs to disk
    solution_name: str | None = None  # base name for the output file
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata["overrides"]: dict of project-wide keys from the task YAML to
    # deep-merge onto the global cfg *for this task only* (permissive).
    # metadata also carries provenance (task_source, db ids, etc.).

@dataclass
class AttemptResult:
    """What the engine produces. Sink decides how/where to persist it."""
    task_id: str
    solution_file: Path | None        # None if the run failed
    logs: dict[str, Any]              # status, timings, errors, cost, etc.
    started_at: str                   # ISO-8601
    finished_at: str
    agent_model_name: str             # "claude_web", "chatgpt_web_agent", ...
    prompt_version: int | str | None

class TaskSource(Protocol):
    def iter_tasks(self) -> Iterator[TaskSpec]: ...
    def close(self) -> None: ...

class AttemptSink(Protocol):
    def publish(self, result: AttemptResult) -> None: ...
    def close(self) -> None: ...
```

### How the runner uses it

```python
# infra/run.py
# --run-config routing: task-shaped files get their reserved keys stripped
# and the rest overlaid; overlay-shaped files are layered as-is. Either
# way, everything collapses into a single cfg at load time.
if run_config_is_task_yaml:
    overlay = {k: v for k, v in run_config_data.items()
               if k not in _RUN_CONFIG_TASK_KEYS}
    cfg = load_configs(run_config_data=overlay)
    cfg.source.kind = "yaml"
    cfg.source.yaml_path = str(run_config_path)
else:
    cfg = load_configs(run_config_path=run_config_path)  # layers 1 + 2 + 3

source = build_source(cfg)
sink   = build_sink(cfg)

for spec in source.iter_tasks():
    engine_config = build_engine_config(cfg, spec)   # pure projection
    preflight_check(engine_config, cfg.provider.kind) or die
    result = run_engine(engine_config)               # existing engine, unchanged
    sink.publish(result)
```

`build_engine_config(cfg, spec)` is a pure projection — no merging. It selects the active provider block (`claude_web` / `chatgpt_web`) and assembles `{agent_type, prompts, task_name, task_id, upload_files, solution_name, <provider>_web: {...}}` for the engine.

No template file is read. `infra/configs/` is the entire input surface.

### Directory/file layout customization

Users customize **what to pull** and **into what layout** by implementing their own `TaskSource`. The interface gives them two levers:

1. **Which files to upload** — `TaskSpec.upload_files` is a plain list of local paths. The source builds this list however it wants (full set, filtered by extension, one file per task, grouped folders, etc.).
2. **Where files land on disk** — the source downloads remote blobs to any directory layout it chooses (typically under a scratch dir). The engine only sees the final paths.

Two reference sources ship out of the box:

- `YamlTaskSource` — reads per-task YAMLs from whatever `source.yaml_path` points at (defaults to `infra/configs/task_configs`, but `--run-config` with a task-shaped file redirects this per invocation). Accepts either a directory of one-task-per-file YAMLs or a single file with a `tasks:` list.
- `PostgresS3TaskSource` — Part 2 below.

---

## Part 2 — Bizbench implementation

### Facts to build against (extracted from existing code)

| Thing | Value | Source |
|---|---|---|
| DB | Neon Postgres | `judge/project_configs.yaml` |
| DB URL env var | `BIZBENCHJUDGE_KEYS_DATABASE_URL` | `judge/operation_scripts/get_tasks.py` |
| Scratch dir env var | `BIZBENCHJUDGE_PATHS_SCRATCH_PATH` | same |
| S3 bucket | `biz-bench` | `cli-agents-master/excel_cli_agent/auto_batch_runner.py` |
| S3 attempts prefix | `BizbenchV1/attempts/{agent_folder}/task_source={src}/task_id={id}` | same |
| S3 URI format | `s3://bucket/key` | `judge/operation_scripts/get_tasks.py:parse_s3_uri` |
| `tasks` columns | `id, task_name, task_starting_files, task_solution_files, task_source, deprecated, ...` | `get_tasks.py` |
| `task_attempts` columns | `id, task_id, agent_model_name, agent_model_type, attempt_files, prompt_files, start_time, end_time, time_taken_min, cost, prompt_version, agent_failed, deprecated` | `get_tasks_and_attempts.py` |
| Existing GUI model names | `claude_web`, `GPT-5.4 (Extended Pro)`, `GPT-5.4 (Agent)` | `get_tasks_and_attempts.py:DEFAULT_MODELS` |

### `PostgresS3TaskSource`

Responsibilities:

1. **Select** tasks from `tasks` with filters: `deprecated = false`, optional `task_source IN (...)`, optional explicit `id IN (...)`, optional "not yet attempted by this agent" join against `task_attempts`.
2. **Download** each row's `task_starting_files` (list of `s3://...` URIs) to `$SCRATCH/gui/task_id={id}/starting_files/{basename}` using the existing `parse_s3_uri` / `boto3` helpers.
3. **Yield** a `TaskSpec` with `upload_files` set to the downloaded paths and `metadata` carrying `task_source`, `old_id`, etc., for the sink to reference later.

Construction-time contract (both source and sink):

- **Strict AWS credentials.** `aws.access_key_id` + `aws.secret_access_key` must resolve from `configs.yaml` or from the env vars named by `aws.*_env`. The boto3 default credential chain (`~/.aws/credentials`, IAM role, etc.) is **not** consulted. Missing values → scaffolding prompt (mirrors the `database.url` pattern via `ensure_overrides_present`) and a `ValueError` swallowed cleanly by `run.py`.
- **Preflight on boto3 client build.** The source calls `sts.get_caller_identity()` and logs `account=… arn=…` so operators see which AWS identity is live before any task download. Bad/expired credentials fail here with an actionable `ValueError`, not mid-run.

Config (YAML):
```yaml
source:
  kind: postgres_s3         # backend category
  schema: bizbench          # schema wiring (required when kind=postgres_s3)
  filters:
    task_ids: [1, 2, 3]               # or omit
    task_sources: [modeloff]          # or omit
    skip_already_attempted: true
    skip_deprecated: true
# DB url, scratch dir, agent identity, and AWS creds are read from the
# shared database.* / paths.* / agent.* / aws.* blocks.
```

The `(kind, schema)` pair is dispatched in [task_io/registry.py](../task_io/registry.py): `kind` picks the backend category (`yaml`, `postgres_s3`, …) and `schema` picks the concrete subclass wiring within that backend. `schema` is required when a kind supports multiple wirings (today only `postgres_s3` → `bizbench`) and rejected when a kind has a single implementation (`yaml`, `local`). Unknown values on either axis raise `ValueError` from `_validate_kind_schema` with the set of accepted alternatives.

Trial / idempotency behavior mirrors `AutoBatchRunner._task_has_recent_attempts` in cli-agents: skip a task if there's already a non-failed, non-deprecated attempt from the same `agent_model_name` at the current `prompt_version` within a configurable window.

### `PostgresS3AttemptSink`

Responsibilities:

1. **Upload** `result.solution_file` to `s3://biz-bench/BizbenchV1/attempts/{agent_folder}/task_source={src}/task_id={id}/{timestamp}_{basename}`.
2. **Upload** the per-task JSON log (same one [completion_logger.py](../claude_web_agent/completion_logger.py) already writes) alongside.
3. **Insert** a row into `task_attempts`:

   ```sql
   INSERT INTO task_attempts (
     task_id, agent_model_name, agent_model_type,
     attempt_files, prompt_files,
     start_time, end_time, time_taken_min, cost,
     prompt_version, agent_failed, agent_failed_reason, deprecated
   ) VALUES (...)
   ```

   with `attempt_files = ARRAY['s3://biz-bench/.../solution.xlsx', 's3://biz-bench/.../completion_log.json']`.

In addition to the strict-credentials contract and STS identity log described above for the source, the sink also calls `s3.head_bucket(aws.s3_bucket)` at construction — a single HEAD request that validates both credential validity and bucket access in one shot, before the engine spends up to 45 min on a task that can't be persisted. Missing bucket / `AccessDenied` / bad region → `ValueError` with an actionable message.

Resolved conventions (from prior open questions):

- `agent_model_type` is hardcoded to `"gui"`.
- `cost` is always `NULL` (web GUI runs are subscription-based; distinct from the CLI runner's `0.0`).
- Failed / timeout runs still insert a row with `agent_failed=true` and `agent_failed_reason` populated from `result.status` or `result.extra`.
- Per-task metadata from the source (`task_source`, `db_task_id`, …) flows to the sink via `result.extra["task_metadata"]`, threaded in `infra/run.py` when the runner constructs the `AttemptResult`.

Config lives in the shared `agent:` / `aws:` / `database:` blocks — the `sink:` YAML only needs `kind` + `schema`. Everything else is read from the existing default/overlay blocks, so a task-source and attempt-sink that both target Postgres + S3 share the same DB url, AWS credentials, bucket, and `agent.*` identity:

```yaml
sink:
  kind: postgres_s3
  schema: bizbench
# bucket / prefix / agent identity / prompt_version are all pulled from
# aws.* + agent.* in configs.default.yaml + configs.yaml.
```

### Open questions still outstanding

- **Concurrency** — if we later parallelize, need `SELECT ... FOR UPDATE SKIP LOCKED` on the task-claiming query. Defer until Phase 5.
- **EC2 credentials story** — Part 3 below still sketches an IAM instance profile, but the current strict-credentials contract explicitly blocks boto3's default chain. Before Phase 3 we need to decide: relax the contract for EC2 (flag / env opt-in), or have the systemd unit materialize STS temp credentials into the `aws.*_env`-named env vars before launching the runner.

---

## Part 3 — AWS runtime

### Recommended: single EC2 instance, first

**Why not Fargate first:** each task is 5–45 min, Claude/ChatGPT web sessions expire if re-authenticated from a fresh fingerprint, and the value of horizontal scaling is low until the queue is deep. One persistent VM with a stable login is the path of least resistance.

**Instance shape:**
- `t3.large` (2 vCPU, 8 GiB) minimum; `t3.xlarge` if running both Claude + ChatGPT Chromes concurrently.
- Ubuntu 22.04 LTS, 30 GiB gp3.
- Security group: SSH (22) from your IP only. No inbound for Chrome CDP — it stays localhost.

**Inside the VM:**
```
systemd
  └─ xvfb.service              : Xvfb :99 -screen 0 1920x1080x24
  └─ chrome-claude.service     : google-chrome --remote-debugging-port=9222
                                 --user-data-dir=/var/lib/gui-agents/chrome-claude
                                 (DISPLAY=:99)
  └─ chrome-chatgpt.service    : same, port 9333, different user-data-dir
  └─ gui-agents.service        : the batch runner in --poll mode
```

**First-time login flow:**
1. SSH with `-L 5901:localhost:5901` tunnel.
2. Run x11vnc (or NICE DCV) against the existing Xvfb.
3. Connect a VNC viewer, log in to claude.ai / chatgpt.com manually.
4. Cookies persist in the `--user-data-dir`.
5. Tear down VNC; the services stay running.

Repeat every few weeks when sessions expire. (Or later: script cookie injection from a secret store.)

### How the runner is driven

Instead of `--tasks file.yaml` the runner runs in a **poll loop**:

```bash
gui-agents poll \
  --source postgres_s3 --source-config /etc/gui-agents/source.yaml \
  --sink   postgres_s3 --sink-config   /etc/gui-agents/sink.yaml \
  --provider claude \
  --poll-interval 60 \
  --max-idle 3600
```

Each iteration: ask the source for the next eligible task → run it → publish → loop. If `iter_tasks()` is empty for `max-idle` seconds, exit (systemd restarts — cheap way to recycle Chrome state).

### Config & secrets

- `BIZBENCHJUDGE_KEYS_DATABASE_URL` stored in SSM Parameter Store (SecureString) or AWS Secrets Manager. Pulled into the systemd unit's `Environment=` at start.
- AWS credentials for the runner must be surfaced as env vars named by `aws.access_key_id_env` / `aws.secret_access_key_env` (e.g. `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) **before** the service starts, because the code deliberately does not use boto3's default credential chain. Two viable paths:
  - Store long-lived IAM user keys in SSM/Secrets Manager, inject via systemd `Environment=` at service start.
  - Keep an IAM instance profile on the box, run a systemd pre-start hook that calls `aws sts assume-role` (or reads the instance-metadata creds) and exports them into the unit's environment before `ExecStart`. Revisit before Phase 3 — see open questions above.
- Required IAM permissions on whichever principal: `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on `biz-bench`. `sts:GetCallerIdentity` is always allowed.
- No AWS keys in the repo.

### Observability

- systemd journal → CloudWatch Logs agent.
- Per-task JSON log is already written by `completion_logger.py`; sink uploads it to S3 next to the solution.
- CloudWatch alarm on: "no successful attempts in 2h" (detects session expiry).

---

## Phased rollout

| Phase | Deliverable | Exit criteria | Status |
|---|---|---|---|
| **0a** | `task_io/` scaffold + `TaskSpec`/`AttemptResult` + `YamlTaskSource` + `LocalAttemptSink` + `infra/run.py` | Existing YAML tasks run through the new seam | ✅ shipped |
| **0b** | Config consolidation: everything under `infra/configs/`, legacy `tasks_configs/` decoupled from `infra/run.py`, `task_configs/` folder for per-task YAMLs, permissive task-level merge | Running `python -m infra.run` reads zero files outside `infra/configs/`; a task YAML's top-level keys deep-merge onto global cfg for that task | ✅ shipped (superseded by 0c) |
| **0c** | `--run-config PATH` CLI flag + `infra/configs/run_configs/` layout. Replaces `--yaml-path`. **Dropped the per-task override layer (layer 4)** — merge model collapses to 3 layers. Task-shaped run-configs split reserved keys (→ YamlTaskSource) from non-reserved keys (→ layer 3 overlay); overlay-shaped files are layer 3 as-is | `--run-config local_run_examples/sample_task.yaml` runs the local sample end-to-end; `--run-config bizbench_run_examples/sample_bizbench.yaml` drives `postgres_s3` | ✅ shipped |
| **1** | `PostgresS3TaskSource` (read-only, no DB writes) + `LocalAttemptSink` already shipped | Can run a real Bizbench task end-to-end locally, pulling from Neon+S3, writing solution to local disk | ✅ shipped |
| **2** | `PostgresS3AttemptSink`. `agent_model_type` is always `"gui"`; `cost` is always `NULL` (GUI runs are subscription-based); failed/timeout runs still insert a row with `agent_failed=true` + `agent_failed_reason`. Per-task metadata from the source flows to the sink via `result.extra["task_metadata"]`. Strict AWS-credentials contract (no boto3 default chain) + construction-time preflight: `sts.get_caller_identity()` on both source/sink + `s3.head_bucket` on sink. `build_source`/`build_sink` ValueErrors are caught cleanly in `infra/run.py`. | Solutions land in S3 and new `task_attempts` rows appear, on laptop first | in progress |
| **3** | AWS EC2 + systemd + first manual login | One successful unattended task run on EC2 | planning |
| **4** | Poll loop + idempotency (skip-already-attempted) | Can leave it running overnight against the real backlog | planning |
| **5** (optional) | Second EC2 for ChatGPT, or parallelism via row-level locking | Two providers running concurrently | planning |

Each phase is shippable on its own; stop whenever the value runs out.

---

## Risks

- **Session expiry / bot detection.** Claude.ai and chatgpt.com both get cranky about automated browsers from cloud IPs. Budget time; may need residential-style egress or NAT through a known-clean IP.
- **ToS.** Automating the web UI at volume is a gray area for both providers. For large research runs, the official APIs are more defensible — worth a sanity check before investing in Phases 3+.
- **Long tasks.** 45 min max per task means any queue lease / visibility timeout must be generous, and the runner must be idempotent (a crash mid-task must not duplicate the attempt row).
- **Schema drift.** If `tasks` / `task_attempts` columns change, `PostgresS3TaskSource/Sink` break. Keep the SQL in one file and pin to the columns we read/write; loud-fail on missing columns.
