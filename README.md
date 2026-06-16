# BizbenchV2

Scripts for the BizbenchV2 task set: uploading task files to S3 and Neon, and
generating expert-human time estimates for each task with Gemini.

The tasks live in a Neon Postgres database (BizbenchV2) in a `tasks` table, with
the starting and solution files stored in S3 under
`s3://biz-bench/BizbenchV2/tasks/<task_name>/`.

## Contents

- `estimate_task_times.py` — for each task, downloads the starting file from S3,
  converts it to CSV, asks Gemini how long an expert modeler would take to
  complete it, and writes the estimate back to the `tasks` table.
- `ingest_tasks.py` — uploads local task folders to S3 and inserts a row per
  task into the `tasks` table. (Used for the initial load; included for
  reference.)

## Prerequisites

- Python 3.10+
- An AWS profile that can read/write the `biz-bench` S3 bucket
- The Neon connection string for the BizbenchV2 database
- A Gemini API key (https://ai.google.dev/)

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
#    ...edit .env and fill in DATABASE_URL and GEMINI_API_KEY...
source .env

# 4. Confirm AWS access (credentials come from ~/.aws/credentials or AWS_* vars)
aws sts get-caller-identity
aws s3 ls s3://biz-bench/
```

## Running the time estimation

Always dry-run first — it downloads and converts the starting files but does not
call Gemini or write to the database:

```bash
# Preview one task end to end
python estimate_task_times.py --dry-run --limit 1

# Estimate one task for real
python estimate_task_times.py --limit 1

# Estimate all tasks (skips any that already have an estimate)
python estimate_task_times.py
```

Useful flags:

- `--task-source NAME` — which task source to process (default `jp`).
- `--model NAME` — Gemini model to use (default is set at the top of the script).
- `--limit N` — process at most N tasks.
- `--force` — re-estimate tasks that already have an estimate.
- `--skip TASK_NAME [...]` — skip specific tasks entirely (see Known limitations).
- `--dry-run` — download and convert only; no Gemini calls, no writes.

The script is idempotent: it skips tasks that already have an estimate, so it is
safe to stop (Ctrl+C) and re-run — it picks up where it left off.

## Running the ingestion

```bash
python ingest_tasks.py --tasks-dir /path/to/Tasks --dry-run
python ingest_tasks.py --tasks-dir /path/to/Tasks
```

Each task folder must contain a `starting_files/` and a `solution_files/`
subfolder. The script is idempotent on `(task_name, task_source)`.

## Schema

The estimates are stored in two columns on the `tasks` table:

```sql
ALTER TABLE tasks ADD COLUMN ai_time_estimate_min DOUBLE PRECISION;
ALTER TABLE tasks ADD COLUMN ai_time_estimate_reasoning TEXT;
```

`ai_time_estimate_min` holds the point estimate in minutes; the model's reasoning
is stored alongside it in `ai_time_estimate_reasoning`.

## Known limitations

Two tasks (`FundFun` and `MarketBalanced`) contain very large stock-price tables
(1M+ cells), which exceed the Gemini free-tier per-minute token limit and so do
not yet have an estimate. They are currently excluded with `--skip FundFun
MarketBalanced`. Options to handle them later: send only a sample of the large
sheet, or use a paid Gemini tier.
