# MBABenchV2

The single repo for the MBABench experiments. It hosts every agent pipeline
plus the judge, and each run declares which **benchmark** it belongs to:

|                | **v1** (BizbenchV1 wave)                                           | **v2** (MBABenchV2 task set)                                                           |
| -------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| DB (Neon)      | `BizbenchV1`                                                       | `MBABenchV2`                                                                           |
| S3             | `s3://mbabench/BizbenchV1/…`                                       | `s3://mbabench/MBABenchV2/…`                                                           |
| Agent prompts  | single-prompt pv9 (`gui-agents-master/tasks_configs/prompts_pv9/`) | rubric prompts + house standards (`gui-agents-master/tasks_configs/prompts_v4/`, `house_standards/`) |
| Grading rubric | 3 categories / 17 checks (`judge/prompts/rubrics/rubric_8.json`)   | 12 categories / 132 checks (`judge/prompts/rubrics/rubric_9.json`, agentic judge only) |

How each pipeline selects the benchmark at launch:

- **`gui-agents-master/`** — Playwright drives claude.ai / chatgpt.com.
  Set `benchmark: v1|v2` in the run config; it gates identity labels, the
  source/sink schema (`bizbench` vs `mbabenchv2`), S3 defaults, and provider
  preflight. Prompts come from `prompt_version` via
  `tasks_configs/prompts/registry.yaml` (default 204 = the v2 3-step set
  with the house standards attached; 205 is its single-pass twin; v1
  configs use 9, the pv9 payload). Examples:
  `infra/configs/run_configs/{bizbenchv1,mbabenchv2}_run_examples/`.
- **`cli-agents-master/`** — our own harness on raw model APIs.
  Set `benchmark: v1|v2` in the batch config (S3 + a DATABASE_URL sanity
  check); `prompt_version` defaults from the benchmark and must embed its
  rubric (v11 = the frozen pv1105 v1-wave prompts; v12/v13/v14 = the
  v2-rubric sets generated from the GUI `prompts_v2/`, `prompts_v3/`,
  `prompts_v4/` sources; v14, the default, embeds the house standards). A
  mismatched pairing fails at startup (`EXCEL_AGENT_SKIP_RUBRIC_GUARD=1`
  overrides).
- **`coding-agents-master/`** — vendor coding agents (Claude Code, Codex),
  one sandboxed container per attempt. Set `benchmark: v1|v2` in the run
  config; v2 flips S3/DB and defaults `template_version` to v10 (the
  v2-rubric mirror with the house standards seeded into the workspace; v7
  is the v1 pv9 mirror).
- **`judge/`** — grades attempts from either benchmark. Pass
  `--benchmark v1|v2`; it selects the DB (`database.{v1,v2}_url` in
  `config/config.yaml`), the S3 grading root and the rubric pair
  (`BENCHMARKS` in `judge/utils/misc_utils.py`). The 12-category v2 rubric
  must be graded through the agentic judge (`--agentic`).

Cross-benchmark misconfiguration fails at startup in every pipeline (schema
guards + DATABASE_URL checks) rather than writing to the wrong store.

Tasks live in the Neon `MBABenchV2` database (`tasks` table), with starting
and solution files in S3 under `s3://mbabench/MBABenchV2/tasks/<task_name>/`.
Every v2 attempt also receives the house financial-modelling conventions,
`house_standards/House_Standards_v1.md`, alongside the starting files; the
prompt version selects that attachment, so a recorded row implies it
(see `house_standards/README.md`).
Each pipeline and the judge has its own README; `CheatSheet.md` is the
one-page "how do I launch a run" reference across all of them.

## Layout

```text
pyproject.toml             uv workspace root; exposes `config` as a module.
setup.sh                   Environment setup: reads venv_path from the config,
                           then `uv sync` for the whole workspace.
uv.lock                    The pinned resolution for every workspace member.
config/                    The two-tiered config system (ThomsonYen/config).
  config_default.yaml      Committed defaults (DB URLs, S3 bucket, API keys as
                           ${env:VAR} references, EC2 fleet names).
  config.yaml              Local overrides (gitignored, auto-created).
  python/                  Upstream package; config.py is installed as `config`.
scripts/
  export_good_attempts.py  Export the banked good-attempt manifest per
                           (pipeline, model, task) for the v2 study cohorts.
operation/v1/              v1 results assembly and paper figures.
house_standards/           House modelling conventions handed to every v2
                           attempt with the starting files (append-only,
                           versioned; selected by prompt version).
gui-agents-master/         claude.ai / chatgpt.com via Playwright + CDP.
cli-agents-master/         Raw model APIs + a local Excel MCP server.
coding-agents-master/      Claude Code / Codex CLIs in Docker.
excel-agents-master/       Claude / ChatGPT add-ins inside Excel Online.
judge/                     Grades attempts against golden solutions.
judge-annotator/           Human-annotation web app for judge output.
```

[ThomsonYen/config](https://github.com/ThomsonYen/config) is the config system.

## Configuration

Non-secret settings live in `config/config_default.yaml` (committed). On first
run a local `config/config.yaml` is created from the defaults — edit it for
machine-specific overrides; it is gitignored and takes precedence.

Secrets are **not** stored in the committed YAML. They are referenced through
`${env:VAR}` and can be set either in your shell or directly in the gitignored
`config/config.yaml`:

- `V1_DATABASE_URL` / `V2_DATABASE_URL` → `database.v1_url` / `database.v2_url`
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY` → `keys.*`
- AWS keys → `aws.access_key_id` / `aws.secret_access_key` (or the standard
  `~/.aws/credentials` / `AWS_*` locations)

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- An AWS identity that can read/write the `mbabench` S3 bucket
- The Neon connection string(s) for the database(s) you will run against
- LibreOffice (`soffice`) for formula recalculation in the CLI pipeline and judge

## Setup

Dependencies are declared per component in each `pyproject.toml` and resolved
together as a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/),
pinned in the repo-root `uv.lock`. `setup.sh` creates the environment, installs
that locked set, and installs every workspace member editable — including the
`config` module.

```bash
#    Create the environment and install everything (needs uv).
#    Location comes from venv_path in config/config.yaml; default is .venv.
./setup.sh

# [Optional] Confirm AWS access
aws sts get-caller-identity
aws s3 ls s3://mbabench/
```

## Exporting the good-attempt manifest

```bash
python scripts/export_good_attempts.py --out scripts/good_attempts_v2.json
```

Writes one entry per (pipeline, model, task) for the eight study cohorts,
picking the latest non-deprecated, non-failed `task_attempts` row per cell.
The output is DB-derived and should not be committed.
