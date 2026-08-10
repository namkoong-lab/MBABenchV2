# MBABenchV2

The single repo for the MBABench experiments. It hosts every agent pipeline
plus the judge, and each run declares which **benchmark** it belongs to:

| | **v1** (BizbenchV1 wave) | **v2** (MBABenchV2 task set) |
|---|---|---|
| DB (Neon) | `BizbenchV1` | `BizbenchV2` |
| S3 | `s3://mbabench/BizbenchV1/…` | `s3://biz-bench/MBABenchV2/…` |
| Agent prompts | single-prompt pv9 (`gui-agents-master/tasks_configs/prompts_pv9/`) | 3-step rubric prompts (`gui-agents-master/tasks_configs/prompts_v2/`) |
| Grading rubric | 3 categories / 17 checks (`judge/prompts/rubrics/rubric_8.json`) | 12 categories / 132 checks (`judge/prompts/rubrics/rubric_9.json`, agentic judge only) |

How each pipeline selects the benchmark at launch:

- **`gui-agents-master/`** — Playwright drives claude.ai / chatgpt.com.
  Set `benchmark: v1|v2` in the run config; it gates identity labels, the
  source/sink schema (`bizbench` vs `mbabenchv2`), S3 defaults, and provider
  preflight. Prompts come from `prompts_file` (defaults to the v2 3-step
  set; v1 configs point at the pv9 payload). Examples:
  `infra/configs/run_configs/{bizbenchv1,mbabenchv2}_run_examples/`.
- **`cli-agents-master/`** — our own harness on raw model APIs.
  Set `benchmark: v1|v2` in the batch config (S3 + a DATABASE_URL sanity
  check); prompts are chosen independently via `prompt_version` (v11 = the
  frozen pv1105 wave prompts).
- **`coding-agents-master/`** — vendor coding agents (Claude Code, Codex),
  one sandboxed container per attempt. Set `benchmark: v1|v2` in the run
  config; v2 flips S3/DB and defaults `template_version` to v8 (the
  v2-rubric mirror; v7 is the v1 pv9 mirror).
- **`judge/`** — grades attempts from either benchmark. Select the rubric
  pair + `check_order` in `judge/project_configs.yaml` (copy from
  `project_configs.example.yaml`, which documents both presets). The
  12-category v2 rubric must be graded through the agentic judge.

Cross-benchmark misconfiguration fails at startup in every pipeline (schema
guards + DATABASE_URL checks) rather than writing to the wrong store.

The rest of this README covers the V2 task-management scripts. Tasks live
in the Neon `MBABenchV2` database (`tasks` table), with starting and
solution files in S3 under `s3://biz-bench/BizbenchV2/tasks/<task_name>/`.

## Layout

```text
pyproject.toml             Editable-install metadata; exposes `config` as a module.
scripts/                   The runnable scripts.
  add_task.py              Upload a new task's files to S3 and register it in
                           the tasks table (the one WRITE script).
  ingest_tasks.py          Download task folders from S3 + the table into a
                           local directory (read-only on the DB).
  estimate_task_times.py   For each local task folder: convert the starting file
                           to CSV, ask Gemini for an expert time estimate, write
                           it to ai_judgement.json (read-only on the DB).
config/                    The two-tiered config system (ThomsonYen/config).
  config_default.yaml      Committed defaults (bucket, model, persona, ...).
  config.yaml              Local overrides (gitignored, auto-created).
  python/                  Upstream package; config.py is installed as `config`.
setup/                     Environment setup.
  requirements.txt         Flat dependency list; freeze target for
                           `uv pip freeze > setup/requirements.txt`.
  setup.sh                 Installs requirements.txt, then `pip install -e .`
                           (uv if present, else pip).
```

[ThomsonYen/config](https://github.com/ThomsonYen/config) is the config system.

## Configuration

Non-secret settings live in `config/config_default.yaml` (committed): the S3
bucket and prefix, the Gemini model, persona, rate-limit settings, and the
default task source. On first run a local `config/config.yaml` is created from
the defaults — edit it for machine-specific overrides; it is gitignored and
takes precedence over the defaults.

Secrets are **not** stored in the YAML. The two secret values reference the
environment via `${env:VAR}`:

- `DATABASE_URL` → `database.url`
- `GEMINI_API_KEY` → `gemini.api_key`

Set them in your shell (see the `${env:VAR}` references in
`config/config_default.yaml`). AWS credentials are still read from
the standard locations (`~/.aws/credentials` or `AWS_*` env vars).

## Prerequisites

- Python 3.10+
- An AWS profile that can read/write the `mbabench` S3 bucket
- The Neon connection string for the MBABenchV2 database
- A Gemini API key (https://ai.google.dev/)

## Setup

`setup/requirements.txt` is the runtime dependency list. `setup.sh` installs it
and then runs `pip install -e .`, the editable install that exposes the `config`
module.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install the project (editable) and its dependencies.
#    This also makes the `config` module importable.
bash setup/setup.sh          # or: pip install -r setup/requirements.txt

# 3. Configure environment variables. These are referenced from
#    config/config_default.yaml via ${env:VAR}; set them in your shell.
export DATABASE_URL="postgresql://USER:PASSWORD@HOST/MBABenchV2?sslmode=require"
export GEMINI_API_KEY="your-gemini-api-key"

# 4. Confirm AWS access (credentials come from ~/.aws/credentials or AWS_* vars)
aws sts get-caller-identity
aws s3 ls s3://mbabench/
```

## Adding a new task

`add_task.py` uploads a task's files to S3 and registers the task in the DB.
It is the only script that writes to the `tasks` table. Always dry-run first:

```bash
python scripts/add_task.py --dry-run \
    --task-name MyTask \
    --starting-files /path/to/MyTask.xlsx \
    --solution-files "/path/to/MyTask - Solution.xlsx"

python scripts/add_task.py \
    --task-name MyTask \
    --task-source jp \
    --starting-files /path/to/MyTask.xlsx \
    --solution-files "/path/to/MyTask - Solution.xlsx"
```

The script:
1. Validates all local files exist before touching S3 or the DB.
2. Uploads each file to `s3://mbabench/MBABenchV2/tasks/<task_name>/{starting,solution}_files/<filename>`.
3. Inserts a row into `tasks` with the resulting S3 URIs.

Useful flags:
- `--task-source NAME` — `task_source` value in the DB (default: `tasks.default_source` from config).
- `--force` — if the task already exists, re-upload and UPDATE instead of erroring.
- `--dry-run` — preview everything without executing.

## Downloading the tasks

`ingest_tasks.py` reads the `tasks` table (read-only — it never alters it) and
downloads each task's starting and solution files from S3 into a local folder:

```bash
python scripts/ingest_tasks.py --dry-run
python scripts/ingest_tasks.py
```

Files download to `<repo root>/scratch/tasks` by default; override with
`--out-dir`. This recreates each task as `<out-dir>/<task_name>/starting_files/`
and `solution_files/`. Useful flags:

- `--task-source NAME` — which task source to download (default from
  `config/config_default.yaml`, `tasks.default_source`).
- `--limit N` — download at most N tasks.
- `--dry-run` — print every download without writing any files.

## Running the time estimation

`estimate_task_times.py` works on the local task folders produced above. It is
read-only on the database: it writes the judgement to `ai_judgement.json` inside
each task folder, not back to the table. Always dry-run first — it converts the
starting files but does not call Gemini or write anything:

It reads from `<repo root>/scratch/tasks` by default (override with
`--tasks-dir`):

```bash
# Preview one task end to end
python scripts/estimate_task_times.py --dry-run --limit 1

# Estimate one task for real
python scripts/estimate_task_times.py --limit 1

# Estimate all tasks (skips any that already have ai_judgement.json)
python scripts/estimate_task_times.py
```

Useful flags:

- `--model NAME` — Gemini model to use (default from
  `config/config_default.yaml`, `gemini.model`).
- `--limit N` — process at most N tasks.
- `--force` — re-estimate tasks that already have an `ai_judgement.json`.
- `--skip TASK_NAME [...]` — skip specific tasks entirely (see Known limitations).
- `--dry-run` — convert only; no Gemini calls, no writes.

The script is idempotent: it skips tasks that already have an `ai_judgement.json`,
so it is safe to stop (Ctrl+C) and re-run — it picks up where it left off.

## ai_judgement.json

For each task, the estimate is written to `<task_name>/ai_judgement.json`:

```json
{
  "task_name": "<task_name>",
  "ai_time_estimate_min": 42.0,
  "ai_time_estimate_reasoning": "..."
}
```

`ai_time_estimate_min` holds the point estimate in minutes; the model's reasoning
is stored alongside it in `ai_time_estimate_reasoning`.

## Known limitations

Two tasks (`FundFun` and `MarketBalanced`) contain very large stock-price tables
(1M+ cells), which exceed the Gemini free-tier per-minute token limit and so do
not yet have an estimate. They are currently excluded with `--skip FundFun
MarketBalanced`. Options to handle them later: send only a sample of the large
sheet, or use a paid Gemini tier.
