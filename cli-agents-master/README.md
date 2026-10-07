# cli-agents — raw model APIs driving an Excel tool server

The CLI pipeline is our own agent harness: a model is called through its API
(Anthropic, OpenAI, OpenRouter or the TensorBlock Forge gateway), reads the
task's case materials and the current state of `solution.xlsx`, and edits the
workbook through 21 Excel tools served by a local MCP server (`excel_mcp_server/`).
Every formula write is validated and recalculated in LibreOffice, so the agent
always sees computed values and the delivered workbook carries cached results.
Attempts are recorded like every other pipeline's
(`task_attempts.agent_model_type = "api"`).

## Quick start

```bash
# from the repo root, once
./setup.sh                                  # uv workspace: installs excel-agent and its MCP server
# LibreOffice: apt-get install libreoffice-calc (Linux) or LibreOffice.app (macOS); found automatically

# API keys: keys.* in <repo>/config/config.yaml, or OPENAI_API_KEY / ANTHROPIC_API_KEY /
# OPENROUTER_API_KEY / FORGE_API_KEY in the shell or a .env in the working directory

cd cli-agents-master

# local mode: your own task folder, no database or bucket
uv run excel-agent --batch-config examples/local/test_local.yaml

# auto mode: tasks from the benchmark database, results to the database and S3
uv run excel-agent --batch-config examples/v2/v2_task_corpbond_haiku45.yaml
```

There is no dry run: the startup banner prints the resolved database (`Database:
MBABenchV2 (from config/config.yaml database.v2_url)`), the resolved identity and
the prompt version; read it before letting a long run proceed. LibreOffice is
required and startup fails loudly without it (`--allow-recalc-fallback` runs
with the limited built-in evaluator instead, and records `recalc_engine` on
every attempt).

## Two modes, one agent

| | **local mode** (`local_mode: true`) | **auto mode** (`auto_mode: true`) |
|---|---|---|
| tasks from | `workspaces: [{path: ./folder/}]`: one folder per task; the model sees its `*.xlsx` (any name but `solution.xlsx`), `*.pdf` and `*.md`; other files (`.txt` included) are copied but ignored | the `tasks` table, by name (`tasks:`) or by filter (`task_filter:`) |
| needs | an API key | an API key + `database.<benchmark>_url` + `aws.*` in `config/config.yaml` |
| results to | `results_dir/<task>/solution.xlsx` (+ `transcript.md`, `task.json`, `openai_requests.csv`) and one line per attempt in `results_dir/attempts.jsonl`; a rerun of the same folder overwrites `solution.xlsx` and appends a line | a `task_attempts` row + a local copy in `run_logs/attempt-<model>-<ts>/` + the attempt files under `s3://<bucket>/<root>/attempts/cli_agents/<model>/task_id=<id>/<ts>/` |
| examples | `examples/local/test_local.yaml` | `examples/batch_config_template_auto.yaml` (every option), `examples/v2/v2_task_corpbond_haiku45.yaml` |

`cli.py` also offers a legacy single-workspace mode (`excel-agent --storage-path DIR --model ...`)
and a legacy batch shape (`model:` + `task_template:` + `workspaces:`); both bypass the identity
registry and are for debugging the tool server, never for recorded runs.

## Batch config

A batch config is one self-contained YAML; nothing is layered on top. The keys:

| key | mode | meaning |
|---|---|---|
| `batch_name` | both | run label (also the `batch_logs/` folder name) |
| `agent_model_name` | both | **the only model key**: a label in `excel_cli_agent/agent_identities.yaml` |
| `benchmark` | auto | `v1` (BizbenchV1) or `v2` (MBABenchV2); selects the database, the S3 root and the default `prompt_version` |
| `prompt_version` | both | `v1`..`v16` (see below); default `v16` for v2, `v10` for v1; must belong to the benchmark |
| `workspaces` | local | task folders |
| `task_type` | local | `fmwc` or `wsp`: which task template the prompt set uses for a local folder |
| `tasks` / `task_filter` | auto | task names, or `{task_source: jp, missing_for_model: true}` |
| `max_trials`, `trials_since` | auto | skip a task once it has this many attempts under the label since the date |
| `max_iterations` | both | model calls per task (default 40) |
| `api_timeout_seconds` | both | per model call; default 60 min for `max`/`xhigh` effort, 240 s for `high`, 180 s otherwise |
| `workspace_base_dir`, `results_dir`, `cleanup_workspace` | both / local / both | where workspaces are created (local default `./workspaces`; auto default `batch_logs/batch_<ts>/workspaces/`), where local results go (default `./results`), whether to delete the workspace afterwards (local default `false`, auto default `true`) |
| `allow_recalc_fallback` | both | run without LibreOffice (debugging only) |
| `verbose`, `enable_langfuse` | both | logging |

Both effective run limits are written to every attempt row
(`extra_configs.max_iterations`, `extra_configs.api_timeout_seconds`).

### Agent identities

`excel_cli_agent/agent_identities.yaml` is the append-only registry: the config
names a cohort with one key, `agent_model_name` (the value written to
`task_attempts.agent_model_name`), and the entry pins everything that changes
what the agent does: `model`, `reasoning_effort`, `thinking_budget_tokens`,
`max_completion_tokens`, `base_url` (which also selects the provider code path),
`fresh_context_mode`, `enhanced_excel_context`, `recent_history_count`. A config
that sets any of them refuses to run; an unknown label refuses and prints a
paste-ready stanza; the resolved settings are stamped into
`task_attempts.extra_configs` on every v2 row. Registered today:

| label | model | effort | route |
|---|---|---|---|
| `openpyxl_anthropic/claude-fable-5-1-max` | claude-fable-5-1 | max | Anthropic |
| `openpyxl_anthropic/claude-fable-5-max` | claude-fable-5 | max | Anthropic |
| `openpyxl_anthropic/claude-sonnet-5-xhigh` | claude-sonnet-5 | xhigh | Anthropic |
| `openpyxl_anthropic/claude-haiku-4-5-think16k` | claude-haiku-4-5 | 16k thinking budget | Anthropic |
| `openpyxl_openai/gpt-6-astra-xhigh` | gpt-6-astra | xhigh | OpenAI |
| `openpyxl_openai/gpt-5.6-sol-xhigh`, `.../gpt-5.6-sol-max` | gpt-5.6-sol | xhigh / max | OpenAI |
| `openpyxl_openai/gpt-5.2-none`, `openpyxl_openai/gpt-4o-mini` | gpt-5.2 / gpt-4o-mini | none / - | OpenAI |
| `openpyxl_tensorblock/gpt-6-astra-xhigh` | tensorblock/gpt-6-astra | xhigh | Forge |
| `openpyxl_tensorblock/grok-4.6-xhigh` | tensorblock/grok-4.6 | xhigh | Forge |
| `openpyxl_tensorblock/kimi-k3-max` | tensorblock/Kimi-K3 | max | Forge |
| `openpyxl_tensorblock/gemini-3.8-flash-high` | tensorblock/gemini-3.8-flash | high | Forge (native function calls, v16 Gemini prompt variant) |
| `openpyxl_tensorblock/qwen3.8-max-xhigh` | tensorblock/qwen3.8-max | xhigh | Forge |
| `openpyxl_tensorblock/glm-5.3-max` | tensorblock/glm-5.3 | max | Forge |
| `openpyxl_tensorblock/claude-opus-5-max` | tensorblock/claude-opus-5 | max | Forge |

To run a new model or setting, add a stanza with a new label; never edit one that
has recorded rows (the label is what groups rows into a cohort).

### Prompt versions

`excel_cli_agent/prompt_versions.py` maps `prompt_version` to a system prompt
and a task template in `excel_cli_agent/prompts/`. The integer recorded in
`task_attempts.prompt_version` (and in `attempts.jsonl`) is `system version x
100 + template version`. Versions are immutable once used; new text is a new
number.

The v2 experiment runs `v16` (`system_prompt_v16.txt` + `task_template_shared_v9.txt`,
recorded as `1609`), the v2 default; `v10` is the v1 default. `v1`..`v11` carry the
v1 rubric and `v12`..`v16` are v2 sets, so a config pairing a v1 prompt with
`benchmark: v2` (or the reverse) fails at startup
(`EXCEL_AGENT_SKIP_RUBRIC_GUARD=1` forces a deliberate cross-benchmark run).
`tensorblock/gemini-3.8-flash` gets a function-call variant of the v16 system
prompt (`MODEL_SYSTEM_PROMPT_VARIANTS`); every other model gets the set's file.

**Attachments.** `v14`+ declare `attachments` (repo-root-relative:
`house_standards/House_Standards_v1.md`) and `v15`+ the name it is delivered
under (`HOUSE_STANDARDS.md`). Both runners copy the file into the workspace and
fail the task if it is missing. Every `.md` in the workspace is embedded in
every model call (the agent has no tool to read them): files whose name starts
with `house_standards` go under a `HOUSE STANDARDS (<file>)` header, any other
`.md` under `ATTACHED TEXT (<file>)`. Provenance: the file is uploaded with the
prompt snapshot (`prompt_files`) and `extra_configs.house_standards` records
`{version, file, sha256}` computed at run time.

## What gets created

```text
<workspace_base_dir>/<task>_<run_id>/
├── <starting files>       # copied from the task folder, plus HOUSE_STANDARDS.md
├── solution.xlsx          # the delivered workbook, recalculated after every formula write
└── agent_logs/
    ├── openai_requests.csv    # one row per model call (tokens, cost, timing)
    └── task_<id>/
        ├── task.json
        └── transcript.md
batch_logs/batch_<timestamp>/  # under the current working directory
    ├── summary.md
    └── aggregated_metrics.json
```

Local mode copies `solution.xlsx`, `transcript.md`, `task.json` and
`openai_requests.csv` to `results_dir/<task>/` (overwriting a previous run's
files) and appends one JSON line per attempt to `results_dir/attempts.jsonl`;
auto mode keeps a full copy in `run_logs/attempt-<model>-<ts>/`, uploads to S3
and writes the `task_attempts` row (the workspace is deleted afterwards unless
`cleanup_workspace: false`).

## Configuration sources

- **`<repo>/config/config.yaml`**: `database.v1_url` / `v2_url` (the batch
  config's `benchmark` picks one), `aws.*`, `keys.*`, `libreoffice_path`.
- **Environment / `.env`** (loaded from the working directory): `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `FORGE_API_KEY` and
  `LIBREOFFICE_PATH` win over the repo config when set. `DATABASE_URL` and
  `AWS_*` are only fallbacks for a standalone checkout without the `config`
  module. Optional `LANGFUSE_*` for tracing.
- LibreOffice resolution: `LIBREOFFICE_PATH`, then `libreoffice_path` in the repo
  config, then `soffice` on PATH, then the macOS app bundle.

## Layout

```text
excel_cli_agent/
  cli.py                 entry point (excel-agent): routes a batch config to the auto or local runner
  auto_batch_runner.py   DB + S3 pipeline: task lookup, download, run, upload, task_attempts row
  local_batch_runner.py  local folders in, results_dir out
  batch_runner.py        shared base (legacy batch shape lives here too)
  task_executor.py       the agent loop: context -> model call -> tool calls via MCP, per iteration
  mcp_client.py          launches excel_mcp_server as a subprocess, JSON-RPC over stdio
  agent_identity.py / agent_identities.yaml   the cohort registry
  prompt_versions.py / prompts/              the prompt registry and its versioned text
  models_config.py       pricing, context windows, run-limit defaults, Forge stall timeouts
  repo_config.py         reads <repo>/config/config.yaml
  db/                    SQLAlchemy models for tasks / task_attempts
excel_mcp_server/        the 21 Excel tools (file, worksheet, cell read/write, analysis, formatting, meta),
                         formula_validator.py (seven rejection gates), libreoffice_calc.py (recalc engine)
tools/build_v1*_prompts.py   generators of the v12..v16 prompt sets from the GUI sources (regenerate, never hand-edit)
examples/                the batch-config template, a local-mode config, a v2 single-task config
tests/                   offline pytest suite
docs/ARCHITECTURE.md     data flow, components, the full config reference
```

## Tests

```bash
cd cli-agents-master && uv run pytest tests      # offline: no API, DB, S3 or LibreOffice
```

Covers the identity registry, the prompt sets (byte pins against their GUI
sources, rubric-free guards for v15/v16), the formula validator and circular-
reference detection, the context budget, the workspace guard, the attempt
package and the repo-config resolution ladder.
