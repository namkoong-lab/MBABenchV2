# Excel CLI Agent

AI-powered Excel automation agent that builds financial models from case materials using OpenAI + Excel MCP Server.

## Quick Start

Everything the benchmark needs is in this repository plus the task-file download
(root README, "Task files"). A run needs a model API key and nothing else — no database, no object store, no credentials beyond
the model provider's.

```bash
# 1. Install LibreOffice (required for formula recalculation)
apt-get update && apt-get install -y libreoffice-calc
# macOS: install LibreOffice.app (e.g. `brew install --cask libreoffice`);
# the default location is auto-detected, no config needed.

# 2. Install the package
pip install .

# 3. Set the API key for the provider the cohort you want to run uses
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# 4. Run one leaderboard cohort over all 101 tasks
excel-agent --batch-config examples/offline/openpyxl_anthropic__claude-fable-5-1-max.yaml
```

One config per cohort lives in `examples/offline/`, named after the cohort
label with `/` written as `__`:

| config | model | provider key |
|---|---|---|
| `openpyxl_anthropic__claude-fable-5-1-max.yaml` | claude-fable-5-1, effort max | `ANTHROPIC_API_KEY` |
| `openpyxl_openai__gpt-6-astra-xhigh.yaml` | gpt-6-astra, effort xhigh | `OPENAI_API_KEY` |
| `openpyxl_tensorblock__grok-4.6-xhigh.yaml` | grok-4.6, effort xhigh | `FORGE_API_KEY` |
| `openpyxl_tensorblock__kimi-k3-max.yaml` | Kimi-K3, effort max | `FORGE_API_KEY` |
| `openpyxl_tensorblock__gemini-3.8-flash-high.yaml` | gemini-3.8-flash, effort high | `FORGE_API_KEY` |
| `openpyxl_tensorblock__qwen3.8-max-xhigh.yaml` | qwen3.8-max, effort xhigh | `FORGE_API_KEY` |
| `openpyxl_tensorblock__glm-5.3-max.yaml` | glm-5.3, effort max | `FORGE_API_KEY` |

Each runs the same 101 tasks under prompt version v16 (recorded as
`prompt_version` 1609), 40 iterations and a 3600 s per-call timeout — the
settings the published cohorts ran under. One lane through 101 tasks takes a
long time; to split it, copy a config, cut `task_ids` into ranges and run the
copies side by side. They append to the same JSONL under a lock, and a
relaunch skips tasks that already have a row.

### Where the inputs and outputs live

```
data/tasks/task_id=<N>/       the bundled inputs: task.json (the task row) and
                              starting_files/ (the workbook the agent opens)
outputs/<cohort label>/
  task_attempts.jsonl         one row per attempt, the same columns as the
                              benchmark's task_attempts table
  task_id=<N>/<timestamp>/    that attempt's files: solution.xlsx first (the
                              judged workbook), then the prompt files as sent,
                              the request log and the batch config
  prompts/                    the prompt snapshot for the batch
```

`data/README.md` is the contract for both. Move either with `local.data_root`
/ `local.output_root` in `<repo>/config/config.yaml`, or per run with
`SPREADSHEETSMITH_DATA_ROOT` / `SPREADSHEETSMITH_OUTPUT_ROOT`.

One network call is left in an otherwise offline run: at batch start the
per-token prices are looked up from the pricing provider's public endpoint.
With no network it prints `Live pricing fetch failed (...); using static
MODEL_PRICING fallback` and continues on the bundled price tables, so the
run works air-gapped — only the recorded `cost` is then the table's estimate
rather than the provider's current figure.

## What Gets Created

When you run the agent, it creates:

```
workspace/
├── solution.xlsx          # Generated Excel file with formulas
└── agent_logs/           # Detailed execution logs
    ├── openai_requests.csv
    ├── task_execution.log
    └── iteration_*.json

batch_logs/               # Batch run summaries
├── batch_<timestamp>/
    ├── summary.md         # Overall batch results
    └── aggregated_metrics.json
```

## Usage

### Single Workspace

```bash
excel-agent \
  --storage-path /path/to/workspace \
  --model gpt-4o \
  --max-iterations 100
```

### Auto Batch Pipeline (Recommended)

The auto pipeline handles everything: task lookup, staging the starting files
into a fresh workspace, execution, recording the result, and trial
management. Two interchangeable backends sit at each end:

| | tasks read from | attempts written to |
|---|---|---|
| `source: local` / `sink: local` | `data/tasks/` | `outputs/<label>/` |
| otherwise | the `tasks` table | the `task_attempts` table + the object store |

A config that says neither uses the database when a URL resolves for its
benchmark and goes local when none does, saying which on the way in. A
configured URL is never silently bypassed. Only the v2 task set is bundled;
a v1 config asking for the local source is refused.

Copy `examples/offline/` for a reproduction run, or
`examples/batch_config_template_auto.yaml` for something new, and customize:

```yaml
batch_name: "Auto Batch - My Model"
# Cohort label from excel_cli_agent/agent_identities.yaml; its entry supplies
# model, reasoning_effort, thinking_budget_tokens, max_completion_tokens,
# base_url, fresh_context_mode, enhanced_excel_context, recent_history_count.
agent_model_name: "openpyxl_openai/gpt-5.2-none"
auto_mode: true

workspace_base_dir: "./workspaces"

max_trials: 7                       # Skip after 7 attempts per task
trials_since: "2026-02-05"          # Ignore old attempts before this date

# Auto-discover all FMWC tasks missing for this model
task_filter:
  task_source: "fmwc"
  missing_for_model: true

max_iterations: 40        # the default when omitted (models_config.DEFAULT_MAX_ITERATIONS)
```

Run limits: `max_iterations` (one model call per iteration) defaults to 40, and
one model call may take `api_timeout_seconds` — when omitted, 60 minutes for
the `max` and `xhigh` effort tiers (240 s for `high`, 180 s otherwise). Both
effective values are written to every attempt row (`extra_configs.max_iterations`,
`extra_configs.api_timeout_seconds`).

Run:
```bash
nohup excel-agent --batch-config my_auto_config.yaml > run.log 2>&1 &
tail -f run.log  # monitor progress
```

### Legacy Batch Processing

For manual workspace setup with explicit paths:

```yaml
batch_name: "My Analysis Batch"
model: "gpt-4o"
max_iterations: 100

task_template: |
  Build a financial model in solution.xlsx using the case materials.

workspaces:
  - path: "./workspace1/"
  - path: "./workspace2/"
```

```bash
excel-agent --batch-config batch_config.yaml
```

## Configuration

### Local roots

`<SpreadsheetSmith>/config/config.yaml` (read through the shared `config` module
the workspace installs) names where an offline run reads and writes:

- `local.data_root` — the bundled inputs, default `data`.
- `local.output_root` — where runs write, default `outputs`.

`SPREADSHEETSMITH_DATA_ROOT` / `SPREADSHEETSMITH_OUTPUT_ROOT` override both for one run; a
relative value resolves against the repository root. Nothing else is needed
to run the benchmark.

### Database and object store (optional cloud tooling)

Only for a run that records into a database and an object store instead of
`outputs/` — the setup the published waves used. None of it is required to
reproduce them; skip this section for an offline run.

- `database.v1_url` / `database.v2_url` — the batch config's `benchmark:`
  key selects between them, so switching benchmarks never means editing
  `.env`. The startup log prints which database was resolved and why.
- `aws.access_key_id` / `aws.secret_access_key` — object-store credentials
  (falls back to boto3's default chain: env vars, `AWS_PROFILE`, `~/.aws`).
- `aws.s3_bucket` — bucket name (`<bucket>`).

### Model API keys

- `keys.anthropic_api_key` / `keys.openai_api_key` /
  `keys.openrouter_api_key` / `keys.forge_api_key` — model API keys. Env
  vars (below) take precedence when both are set. `forge_api_key` is the
  TensorBlock Forge gateway (`base_url: https://api.forge.tensorblock.co/v1`,
  model ids `tensorblock/<name>`); it bills Forge credits and never falls
  back to another key.

### Environment Variables

API keys can also live in `.env` (loaded automatically from your working
directory) or the shell, and win over the monorepo config when set.
`DATABASE_URL` / `AWS_*` are for the cloud backend only, in a standalone
checkout where the monorepo config isn't installed; an offline run needs
neither:

```bash
# Required
OPENAI_API_KEY=sk-...

# Optional - cloud backend outside the monorepo (not needed offline)
DATABASE_URL=postgresql://user:pass@host:5432/dbname
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

# Optional - Langfuse observability
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

### System Prompt

Versioned prompt files are bundled in `excel_cli_agent/prompts/`. Set the version in your batch config:

```yaml
prompt_version: "v10"  # uses system_prompt_v10.txt + task_template_fmwc_v4.txt
```

In auto mode the default follows the `benchmark` key (v1 → v10, v2 → v16),
and an explicit choice must belong to the same benchmark: v1..v11 carry the
17-check v1 rubric; v12..v14 embed the 132-check v2 rubric (generated from
the GUI `prompts_v{2,3,4}/` sources by `tools/build_v1{2,3,4}_prompts.py`);
v15 is v14 with every rubric-derived passage scrubbed (`tools/
build_v15_prompts.py`, the same scrub as coding template v11) — the agent
gets the task, the Questions-sheet mechanics, the tool guidance and the house
standards, nothing about grading; v16 (2026-09-19, `tools/build_v16_prompts.py`,
prompt_version 1609) is v15 with the five passages of the tool manual that
contradicted the House Standards removed (outflows-positive sign convention,
Calibri 11/12, `0.00%` and no-decimal number codes, "font color, NOT cell
fill") and one sentence giving the standards precedence over the manual's
style advice — the standards' values are not restated. A mismatched pairing fails at startup;
`EXCEL_AGENT_SKIP_RUBRIC_GUARD=1` forces a deliberate cross-benchmark run.

**Attachments (v14+).** A prompt version can declare `attachments` in
`PROMPT_VERSIONS` — monorepo-root-relative files (v14:
`house_standards/House_Standards_v1.md`, the house financial-modelling
standards). The version, never the batch config, selects them, so the
recorded `prompt_version` and the standards the agent saw cannot disagree.
Both runners copy each attachment into the workspace — under its bare name
(v14) or the name the version's `attachment_names` maps it to (v15+:
`HOUSE_STANDARDS.md`, matching the coding pipeline) — and fail the task if
it is missing; `*.md` files in the workspace are detected as
text context and their full text is embedded in every model call under a
`HOUSE STANDARDS (<file>)` header (the agent has no file-reading tool for
`.md`, and Excel tools called on a `.md` name are refused like `.pdf`).
Provenance: the file rides along with the prompt snapshot
(`task_attempts.prompt_files`) and `extra_configs.house_standards` records
`{version, file, sha256}` computed at run time (`file` = the delivered name; `source` = the versioned source when they differ). The MCP formula validator
whitelists `LET` and `XMATCH` from v14 on (the standards recommend them;
LibreOffice 24.8+ evaluates both).

## Key Features

- **Offline by default**: the bundled task set in, `outputs/` out — a model API key is the only credential
- **Auto Pipeline**: task discovery, trial management and resume against either backend
- **Any LLM Provider**: Works with OpenRouter, OpenAI, Anthropic, TensorBlock Forge, vLLM, SGLang via unified `base_url`
- **21 Excel Tools**: File ops, worksheets, cells, formulas, formatting, validation via MCP
- **Formula Recalculation**: LibreOffice auto-recalc after every formula change
- **Structured Logging**: `outputs/<label>/task_attempts.jsonl` — the same columns as the `task_attempts` table, either backend

## Customization

The architecture has three modular layers. Swap what you need:

### Change Agent Behavior (Prompts)
```
excel_cli_agent/prompts/
├── system_prompt_v10.txt          # Main agent instructions (~866 lines)
├── task_template_fmwc_v4.txt      # Task-specific template (FMWC/ModelOff)
└── task_template_wsp_v1.txt       # Task-specific template (WSP)
```
Create a new `_v{N+1}.txt` file, register it in `PROMPT_VERSIONS` in `excel_cli_agent/prompt_versions.py`, and set `prompt_version` in your config. Never edit a versioned file in place once it has been used in a run — results must stay reproducible against the exact prompt text that produced them.

### Add Domain-Specific Tools
```
excel_mcp_server/tools/
├── file_tools.py                  # create_file, list_files, copy_file, ...
├── cell_write_tools.py            # edit_cells, set_cell_formula
├── analysis_tools.py              # scan_structure, search, summarize
└── formatting_tools.py            # format_cells, freeze_panes, ...
```
Add a new `@mcp.tool()` async function that returns a JSON string, following the existing tools in the same module.

### Configure Runs
```
examples/
├── offline/                       # one per leaderboard cohort: all 101 tasks,
│                                  # data/ in, outputs/ out, no credentials
├── batch_config_template_auto.yaml  # Full auto mode template with all options
├── local/
│   └── test_local.yaml            # Workspaces from local folders, results_dir out
├── v1/                            # benchmark: v1, cloud backend
│   ├── test_quick.yaml            # Auto mode, single task, 3 iters
│   └── test_mini_batch.yaml       # Auto mode, 3 tasks
└── v2/                            # benchmark: v2, cloud backend
    └── v2_task_corpbond_haiku45.yaml  # Single v2 task end-to-end check
```

### Output Format
- **Auto mode, `sink: local`**: `outputs/<label>/task_attempts.jsonl` — one JSON line per attempt carrying exactly the `task_attempts` columns, with repo-relative paths to that attempt's files under `outputs/<label>/task_id=<N>/<timestamp>/`.
- **Auto mode, cloud backend**: the `task_attempts` table + object storage.
- **Local mode** (`local_mode`, workspaces from folders you provide): `results_dir/attempts.jsonl` — one JSON line per attempt with model, cost, timing, status.

The per-batch `summary.md` / `aggregated_metrics.json` go to `batch_logs/` **under the current working directory** in every mode, so launch from the directory where you want them.

## Documentation

For detailed information, see:

- **docs/ARCHITECTURE.md** - System architecture, data flow, DB schema, config reference
- **excel_cli_agent/agent_identities.yaml** - Adding a model cohort (`agent_model_name`)
- **pyproject.toml** - Dev extras (`pip install -e ".[dev]"`), ruff and pytest settings

## Troubleshooting

**Empty Excel files?** Check that:
- OpenAI API key is set correctly
- PDF files in workspace are readable
- Agent completed without hitting max_iterations

**Circular reference errors?** The agent has built-in prevention for:
- Self-referencing formulas
- Empty worksheet issues
- Label vs formula confusion
- Placeholder formulas

Check `agent_logs/task_execution.log` for detailed execution trace.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

---

Built with OpenAI API and Excel MCP Server. See docs/ for implementation details and fix history.
