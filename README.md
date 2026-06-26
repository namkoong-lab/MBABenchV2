# BizbenchV2

Scripts for the BizbenchV2 task set: downloading task files from S3 and Neon
into a local folder, and generating expert-human time estimates for each task
with Gemini. Both scripts are read-only with respect to the `tasks` table.

The tasks live in a Neon Postgres database (BizbenchV2) in a `tasks` table, with
the starting and solution files stored in S3 under
`s3://biz-bench/BizbenchV2/tasks/<task_name>/`.

## Layout

```text
pyproject.toml             Editable-install metadata; exposes `config` as a module.
scripts/                   The runnable scripts.
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
- An AWS profile that can read/write the `biz-bench` S3 bucket
- The Neon connection string for the BizbenchV2 database
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
export DATABASE_URL="postgresql://USER:PASSWORD@HOST/BizbenchV2?sslmode=require"
export GEMINI_API_KEY="your-gemini-api-key"

# 4. Confirm AWS access (credentials come from ~/.aws/credentials or AWS_* vars)
aws sts get-caller-identity
aws s3 ls s3://biz-bench/
```

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
