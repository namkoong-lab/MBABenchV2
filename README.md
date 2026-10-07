# MBABenchV2

A benchmark of financial-modelling tasks solved in Excel, the four agent
pipelines that attempt them, and the LLM judge that grades the results.

- **Tasks.** Each task is a business case: a starting workbook (usually with a
  case PDF) that the agent must turn into a working financial model, plus a
  golden solution the judge compares against. Benchmark **v2** (`MBABenchV2`,
  101 tasks, source `jp`) is the current set; **v1** (`BizbenchV1`, 206 public
  competition cases) is closed but still runnable.
- **Pipelines.** The same task reaches a model through four surfaces: the
  vendors' chat products in a browser (`gui-agents-master/`), their add-ins
  inside Excel Online (`excel-agents-master/`), raw model APIs driving our
  own Excel tool server (`cli-agents-master/`), and vendor coding agents in a
  sandbox (`coding-agents-master/`).
- **Judge.** One LLM conversation grades every applicable check of a
  132-check, 12-category rubric (`judge/`), with deterministic Python verdicts
  for the Questions-sheet answers and for 19 formatting and structure checks.
- **House standards.** Every v2 attempt also receives the house
  financial-modelling conventions (`house_standards/`) with its starting files.

## How the pieces fit

```text
            tasks table + task files (Postgres + S3)
                              │
      ┌───────────┬───────────┼───────────┬────────────┐
      ▼           ▼           ▼           ▼            │
   gui-agents  excel-agents  cli-agents  coding-agents │  one attempt = one workbook
      └───────────┴───────────┴───────────┘            │
                              ▼                        │
            task_attempts rows + attempt files (Postgres + S3)
                              │
                              ▼
                     judge/  (grade_from_db.py, grade_with_orchestration.py)
                              │
                              ▼
            gradings rows + grading bundles (Postgres + S3)
```

Every box also has a local form: tasks from a folder, attempts to a folder, the
judge on a folder (see "Two ways to run").

Every run names its benchmark (`benchmark: v1|v2`, or `--benchmark` for the
judge). That one key selects the database, the S3 root, the default prompt
set and the rubric together, and each pipeline refuses a database whose name
does not match. A run names its agent by one label, `agent_model_name`, that an
append-only identity registry resolves to the exact model, effort and settings
(so a database row always says what produced it), and names its prompt text by
one `prompt_version` number from an append-only prompt registry.

## Repository layout

```text
README.md                  This page.
CheatSheet.md              One page: configure, identify and launch a run of each pipeline.
docs/data_stores.md        The database tables and S3 layout the code expects.
setup.sh                   Environment setup: `uv sync` for the whole workspace.
pyproject.toml, uv.lock    uv workspace root (every member below) and the pinned resolution.
config/                    Two-tiered config: config_default.yaml (committed) + config.yaml
                           (gitignored overrides, created on first run). Shared by everything.
house_standards/           The modelling conventions handed to every v2 attempt (versioned).
gui-agents-master/         claude.ai / chatgpt.com through a real Chrome (Playwright + CDP).
excel-agents-master/       The Claude and ChatGPT add-ins inside Excel Online (OneDrive session).
cli-agents-master/         Raw model APIs + a local Excel MCP tool server; LibreOffice recalc.
coding-agents-master/      Claude Code / Codex CLIs, one Docker sandbox per attempt.
judge/                     The grader: single-pass LLM judge + deterministic checks.
operation/                 Maintainers' analysis scripts (need the benchmark database).
judge-annotator/           Web app for human annotation of gradings (maintainers' deployment).
```

Each pipeline and the judge has its own README with the full configuration
reference; `CheatSheet.md` is the one-page launch reference across all of them.

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- LibreOffice (`soffice`) for formula recalculation: the CLI pipeline recalculates
  after every write, and the judge recalculates workbooks that were not saved by
  Excel. `apt-get install libreoffice-calc` on Linux; LibreOffice.app on macOS.
- Per pipeline: Google Chrome and a paid consumer account for the GUI and Excel
  pipelines (plus Microsoft 365 with OneDrive for Excel); Docker for the coding
  pipeline; model API keys for the CLI pipeline, the coding pipeline and the judge.
- For DB-backed runs: a Postgres database and an S3 bucket laid out as in
  [docs/data_stores.md](docs/data_stores.md). Nothing in the repository depends on
  the maintainers' own stores; the local modes below need neither.

## Setup

```bash
./setup.sh          # creates .venv (or venv_path from config/config.yaml) and runs uv sync
```

Dependencies are declared per member in each `pyproject.toml`, resolved together
as one uv workspace and pinned in `uv.lock`; the script installs every member
editable, including the shared `config` module, and the Playwright Chromium build
the GUI pipeline's fallback path needs. Run commands with `uv run ...` or after
`source .venv/bin/activate`.

## Configuration

Non-secret settings live in `config/config_default.yaml` (committed). The first
load creates `config/config.yaml` next to it from the defaults; edit that file
(it is gitignored and wins over the defaults). Secrets are never in the committed
file: they are `${env:VAR}` references that resolve from your shell, or you write
the values into `config/config.yaml` directly.

**Local runs need no database and no AWS key.** Leave `database.*` and `aws.*`
unset; the only required values are the model key(s) for the lane and judge you
run (`keys.*`, or the matching environment variable). The GUI and Excel lanes
need no key at all, only a signed-in browser.

| key | what it is | used by |
|---|---|---|
| `database.v1_url`, `database.v2_url` | Postgres connection strings (`${env:V1_DATABASE_URL}` / `V2_...`) | every DB-backed run; `benchmark` picks one |
| `aws.s3_bucket`, `aws.access_key_id`, `aws.secret_access_key` | the task/attempt bucket (default name `mbabench`); keys may also come from boto3's default chain | every DB-backed run |
| `aws.gui_key_name`, `aws.gui_sg_name`, `aws.gui_ami` | EC2 fleet names for the GUI dispatcher (optional) | `gui-agents-master/infra/dispatcher` |
| `keys.anthropic_api_key`, `keys.openai_api_key`, `keys.openrouter_api_key`, `keys.gemini_api_key`, `keys.forge_api_key` | model API keys (`${env:...}`); environment variables win when both are set | CLI, coding, judge |
| `venv_path` | where `setup.sh` puts the environment (`null` = `.venv`) | `setup.sh` |
| `libreoffice_path` | the `soffice` binary; `null` = auto-detect (PATH, then `/Applications/LibreOffice.app`) | CLI pipeline, judge |

The GUI and Excel pipelines have a second, member-local layer for machine
settings such as Chrome ports and OneDrive paths (`infra/configs/configs.yaml`,
gitignored); their READMEs describe it.

## Two ways to run

**Locally, without any database or bucket.** Each pipeline and the judge read
tasks from local files and write results to local folders. You supply the task
(a starting workbook plus any case PDF) and the agent's own access (an API key,
or a signed-in browser):

| component | how | where results land |
|---|---|---|
| CLI (`cli-agents-master`) | batch config with `local_mode: true` and `workspaces: [{path: ...}]` (`examples/local/test_local.yaml`) | `results_dir/<task>/solution.xlsx` + `results_dir/attempts.jsonl` |
| GUI (`gui-agents-master`) | a task-shaped run config (`task_name`, `upload_files`; `sink.kind: local`), see `infra/configs/run_configs/local_run_examples/` | the attempt's working dir under `paths.scratch_dir`, plus `attempts.ndjson` under `sink.output_dir` |
| Coding (`coding-agents-master`) | `mode: external` with `--task-dir` (a `task.yaml` + `starting_files/`) and `--results-dir` | `<results-dir>/<task>_<ts>_<pid>/solution.xlsx` with transcript and telemetry |
| Excel (`excel-agents-master`) | `source.kind: yaml` + `sink.kind: local`; the task workbook must already sit in OneDrive under the path the engine navigates to | the attempt's working dir under `paths.scratch_dir`, plus `attempts.ndjson` |
| Judge (`judge/`) | `judge/main_scripts/judge.py -f <folder>` on a folder holding the attempt, the golden solution and optional context | `<folder>/judge_results/` (`scores.json`, `ai_judgement.json`, `det_checks.json`) |

**With your own benchmark stores.** Point `database.v2_url` (or `v1_url`) and
`aws.*` at a Postgres database and bucket with the tables and key layout in
[docs/data_stores.md](docs/data_stores.md). Tasks come from the `tasks` table,
attempts are written to `task_attempts` and S3, and the judge grades from the
database.

## Running a pipeline

Launch each pipeline from its own directory. `CheatSheet.md` has the local and
DB launch commands side by side; each member README has the full reference.

```bash
# GUI — supply: your task workbook + case PDF in upload_files; Chrome running on port 9222, signed in to claude.ai / chatgpt.com
cd gui-agents-master && uv run python -m infra.run --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml --dry-run
#   DB alternative: a run config with source/sink postgres_s3, e.g. infra/configs/run_configs/v2_fable5_claude.yaml

# Excel — supply: your workbook already in OneDrive; Chrome signed in to Microsoft 365 with both add-ins installed (scripts/setup_chrome.sh)
cd excel-agents-master && uv run python -m infra.run --run-config my_run.yaml --dry-run
#   local: source.kind yaml + sink.kind local in my_run.yaml; DB: source/sink postgres_s3

# CLI — supply: a folder of task files in workspaces[].path; an API key for the agent_model_name you pick; LibreOffice
cd cli-agents-master && uv run excel-agent --batch-config examples/local/test_local.yaml
#   DB alternative: examples/batch_config_template_auto.yaml (auto_mode: true)

# Coding — supply: a task folder (task.yaml + starting_files/); ANTHROPIC_API_KEY or OPENAI_API_KEY; Docker running with the sandbox image built
cd coding-agents-master && uv run python -m coding_agent.run_task --config run_configs/example_external.yaml --task-dir ./my_task --results-dir ./results
#   DB alternative: run_configs/example_v2_claude.yaml with --task-id N
```

Every run logs the store it resolved before doing anything; the GUI and Excel
pipelines also have a `--dry-run` that resolves config, prompts, identity and
attachments without touching a browser.

## Grading

Local, one attempt folder (needs only the grader's API key):

```bash
JUDGE_SKIP_SUITABILITY=1 uv run python judge/main_scripts/judge.py --benchmark v2 --single-pass --model openai/gpt-5.6-sol -f <folder>
```

`judge/README.md` ("Grade a local task folder") gives the folder layout
(`ai_attempt.xlsx`, `solution/<golden>.xlsx`, optional context) and where each
pipeline leaves its workbook. `JUDGE_SKIP_SUITABILITY=1` grades every rubric
check; drop it only for v2 pool tasks with a suitability annotation. Results
land in `<folder>/judge_results/`.

From the database:

```bash
# one attempt
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --attempt-ids 123 --model openai/gpt-5.6-sol

# every valid attempt of the cohorts you name (latest per task, model and prompt version), four at a time;
# neither driver skips attempts that already have a grading, so a relaunch grades them again
uv run python judge/main_scripts/grade_with_orchestration.py --benchmark v2 --single-pass --all-tasks --workers 4 --models <agent_model_name ...>
```

`judge/README.md` covers the judge's design, flags and outputs.

## Tests

All offline: no database, bucket, browser, Docker or API key.

```bash
uv run pytest gui-agents-master/tests
uv run pytest excel-agents-master/tests
uv run pytest coding-agents-master/tests
(cd cli-agents-master && uv run pytest tests)
(cd judge && uv run pytest detchecks/tests)              # the LibreOffice test skips itself when soffice is absent
for t in judge/tests_offline/*.py; do uv run python "$t"; done
uv run python config/python/test_config.py && bash config/bash/test_config.sh
```

## Benchmarks v1 and v2

`benchmark: v1|v2` (or `--benchmark` for the judge) selects the prompt set and
the rubric together: v1 = `judge/prompts/rubrics/rubric_8.json` (17 checks),
v2 = `rubric_9.json` (132 checks, single-pass judge). Prompt and identity
registries are append-only: never edit an entry that has recorded runs, add a
new one.
