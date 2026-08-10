# Excel CLI Agent

AI-powered Excel automation agent that builds financial models from case materials using OpenAI + Excel MCP Server.

## Quick Start

```bash
# 1. Install LibreOffice (required for formula recalculation)
apt-get update && apt-get install -y libreoffice-calc

# 2. Install the package
pip install .

# 3. Set your API key
echo 'OPENAI_API_KEY=sk-...' > .env

# 4. Run in local mode (no DB/S3 needed — just an API key)
excel-agent --batch-config examples/test_local.yaml

# Or with DB/S3 for production benchmarking (add DATABASE_URL + AWS keys to .env)
# excel-agent --batch-config examples/test_quick.yaml
```

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

The auto pipeline handles everything: DB task lookup, S3 file download, task execution, result upload, and trial management.

Copy `batch_config_template_auto.yaml` and customize:

```yaml
batch_name: "Auto Batch - My Model"
model: "openai/gpt-5.2"
auto_mode: true

workspace_base_dir: "./workspaces"
agent_folder: "openpyxl_openai/gpt-5.2"

max_trials: 7                       # Skip after 7 attempts per task
trials_since: "2026-02-05"          # Ignore old attempts before this date

# Auto-discover all FMWC tasks missing for this model
task_filter:
  task_source: "fmwc"
  missing_for_model: true

max_iterations: 30
max_completion_tokens: 64000
fresh_context_mode: true
enhanced_excel_context: true
```

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

### Environment Variables

Set in `.env` file (loaded automatically from your working directory) or export in shell:

```bash
# Required
OPENAI_API_KEY=sk-...

# Required for auto pipeline (DB + S3)
DATABASE_URL=postgresql://user:pass@host:5432/dbname
AWS_ACCESS_KEY_ID=AKIA...
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

## Key Features

- **Local Mode**: Run from local folders with just an API key — no database or cloud setup
- **Auto Pipeline**: DB-driven task discovery, S3 upload, trial management for production benchmarking
- **Any LLM Provider**: Works with OpenRouter, OpenAI, Anthropic, vLLM, SGLang via unified `base_url`
- **21 Excel Tools**: File ops, worksheets, cells, formulas, formatting, validation via MCP
- **Formula Recalculation**: LibreOffice auto-recalc after every formula change
- **Structured Logging**: `attempts.jsonl` (local) or DB `task_attempts` table (auto mode)

## Customization

The architecture has three modular layers. Swap what you need:

### Change Agent Behavior (Prompts)
```
excel_cli_agent/prompts/
├── system_prompt_v10.txt          # Main agent instructions (~866 lines)
├── task_template_fmwc_v4.txt      # Task-specific template (FMWC/ModelOff)
└── task_template_wsp_v1.txt       # Task-specific template (WSP)
```
Create a new `_v{N+1}.txt` file and set `prompt_version` in your config. See `docs/EXTENDING.md`.

### Add Domain-Specific Tools
```
excel_mcp_server/tools/
├── file_tools.py                  # create_file, list_files, copy_file, ...
├── cell_write_tools.py            # edit_cells, set_cell_formula
├── analysis_tools.py              # scan_structure, search, summarize
└── formatting_tools.py            # format_cells, freeze_panes, ...
```
Add a new `@mcp.tool()` function. See `docs/EXTENDING.md` for template.

### Configure Runs
```
examples/
├── test_local.yaml                # Local mode template (no DB/S3)
├── test_quick.yaml                # Auto mode, single task, 3 iters
└── test_mini_batch.yaml           # Auto mode, 3 tasks
batch_config_template_auto.yaml    # Full auto mode template with all options
```

### Output Format
- **Local mode**: `results_dir/attempts.jsonl` — one JSON line per attempt with model, cost, timing, status
- **Auto mode**: PostgreSQL `task_attempts` table + S3 file storage

## Documentation

For detailed information, see:

- **docs/ARCHITECTURE.md** - System architecture, modular layers, data flow
- **docs/EXTENDING.md** - Adding tools, prompts, and models
- **CONTRIBUTING.md** - Dev setup and code style
- **docs/ARCHITECTURE.md** - Full reference (DB schema, config, design patterns)

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
