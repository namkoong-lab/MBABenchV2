# SpreadsheetSmith

The single repository for the SpreadsheetSmith experiments: the 101-task benchmark of
financial-modelling cases in Excel, four agent pipelines that attempt the
tasks, and the LLM judge that grades the attempts.

**Start with [`REPRODUCE.md`](REPRODUCE.md)** to recreate the experiments.
Everything needed is in this repository except the task workbooks: the task
rows, every prompt and rubric, and the code. The
starting files and golden solutions are one download away (see "Task files"
below). No database and no object store are involved; only model-provider
access for the cohorts you re-run.

## Layout

```text
REPRODUCE.md               How to recreate every experiment from this repository.
CheatSheet.md              One page: configure, identify and launch a run of each pipeline.
data/                      The 101 tasks: task rows plus the workbooks installed from the
                           download, and their hash manifest (data/README.md describes it and
                           the outputs/ layout).
outputs/                   Where offline runs and gradings are written (gitignored).
config/                    Two-tiered config: config_default.yaml (committed) + config.yaml
                           (gitignored; start from config.yaml.example).
scripts/
  install_task_files.py    Puts the downloaded task workbooks under data/tasks/ and checks their hashes.
  verify_offline_bundle.py Hash and completeness check of data/, no credentials.
  export_benchmark_data.py Regenerates data/ from the cloud stores (maintainers, read-only).
house_standards/           House modelling conventions handed to every v2 attempt with the
                           starting files (append-only, versioned; selected by prompt version).
gui-agents-master/         claude.ai / chatgpt.com via Playwright + CDP.
cli-agents-master/         Raw model APIs + a local Excel MCP server.
coding-agents-master/      Claude Code / Codex CLIs in Docker.
excel-agents-master/       Claude / ChatGPT add-ins inside Excel Online.
judge/                     Grades attempts against golden solutions.
judge-annotator/           Human-annotation web app for judge output (cloud tooling).
pyproject.toml, uv.lock    uv workspace; setup.sh installs it.
```

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- LibreOffice (`soffice`) for formula recalculation in the CLI pipeline and the judge
- Docker for the coding pipeline; Chrome and a provider account for the GUI and Excel pipelines
- API keys for the models you run (`config/config.yaml`, `keys.*`)

## Setup

```bash
./setup.sh                                    # environment at venv_path (default .venv); uv sync
cp config/config.yaml.example config/config.yaml   # fill in keys.*; leave database/aws unset
```

## Task files

The starting files and golden solutions are not in the repository (they exceed
GitHub's file-size limits). Fetch them once:

1. Open <https://anonymous-hf.com/a/v410gr4w0zf8/> in a browser and click
   **Download ZIP** near the top of the page (383 MB; the page sits behind a
   browser check, so it cannot be fetched by script). It lands in `~/Downloads`
   as `v410gr4w0zf8.zip`.
2. Install and verify:

```bash
uv run python scripts/install_task_files.py ~/Downloads/v410gr4w0zf8.zip   # no argument: newest matching zip in ~/Downloads
uv run python scripts/verify_offline_bundle.py
```

The installer copies each workbook to `data/tasks/task_id=<N>/{starting_files,solution_files}/`
and checks the sha256 of every archive copy, and of every workbook already on disk,
against `data/MANIFEST.json`; it never overwrites an existing file (add `--force` to), ignores the zip's `task.json` copies (the
repository's are authoritative), honours `SPREADSHEETSMITH_DATA_ROOT`, and exits
non-zero while any workbook is missing or differs. The verifier fails with a
one-line pointer to these steps until the workbooks are installed.

Dependencies are declared per component in each `pyproject.toml`, resolved
together as a uv workspace and pinned in `uv.lock`; `setup.sh` installs every
member editable, including the `config` module.

## Configuration

Non-secret settings live in `config/config_default.yaml` (committed); the
gitignored `config/config.yaml` overrides it. Secrets are referenced as
`${env:VAR}` and can be set in the shell or written into `config.yaml`:

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
  `FORGE_API_KEY` → `keys.*`
- `local.data_root` / `local.output_root` (default `data` / `outputs`) — where
  the bundle is read and runs are written; env `SPREADSHEETSMITH_DATA_ROOT` /
  `SPREADSHEETSMITH_OUTPUT_ROOT`.

With no database url configured every pipeline and the judge use the offline
source and sink. Each pipeline's README describes that path first.

## Running things

- **Re-run a cohort**: one run config per leaderboard cohort under each
  pipeline's `offline/` config folder; commands, expected time and cost in
  `REPRODUCE.md`. Attempts land under `outputs/<label>/` with a
  `task_attempts.jsonl` row each.
- **Grade**: `judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass
  --source local --sink local --all-local …` grades every attempt under
  `outputs/` and writes `outputs/gradings/`.

## Cloud profile (optional)

The maintainers keep a Postgres database and an object store (`<bucket>`).
The names the cloud profile expects (the database name, the object-store
prefix, the sandbox image tag, the OneDrive folder) follow the project name;
stores created under an earlier name need renaming, or the presets in each
package's `BENCHMARKS` table adjusting, before the cloud profile connects.
Setting `database.v2_url` (and `v1_url`) and `aws.*` in `config/config.yaml`
switches every pipeline and the judge to that source and sink, with the same
`benchmark` guards as before; `scripts/export_benchmark_data.py` regenerates
`data/tasks/` from it read-only.
