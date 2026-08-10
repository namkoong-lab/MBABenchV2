# Excel CLI Agent Documentation

## Quick Start

### Installation
```bash
pip install .
```

### Auto Pipeline (Recommended)
```bash
# Single command: DB task discovery → S3 download → execute → S3 upload → DB write
excel-agent --batch-config auto_config.yaml
```

#### Auto Configuration Example
```yaml
auto_mode: true
workspace_base_dir: "/path/to/workspaces"
agent_folder: "openpyxl_PROVIDER/MODEL_SLUG"
model: "openai/gpt-5.2"
max_iterations: 30
max_trials: 7
trials_since: "2026-02-05"
prompt_version: "v10"

# Task selection: explicit names OR auto-discover
tasks: ["Task-Name-1"]          # Option A
task_filter:                     # Option B
  task_source: "fmwc"
  missing_for_model: true
```

### Manual Batch (Legacy)
```bash
# Run batch with YAML config
excel-agent --batch-config batch_config.yaml
```

#### Batch Configuration Example
```yaml
batch_name: "Financial Model Analysis"
model: "gpt-4o-mini"
max_iterations: 60
batch_size: 2

task_template: |
  Build a comprehensive financial model in solution.xlsx using the data and
  case materials provided in the workspace.

workspaces:
  - path: "./workspace1/"
  - path: "./workspace2/"
```

### Single Task (Interactive CLI)
```bash
# Start interactive agent
excel-agent --storage-path /path/to/workspace --model gpt-4o-mini

# With custom iterations and verbose mode
excel-agent --storage-path /path/to/workspace --model gpt-4o-mini --max-iterations 50 --verbose
```

### CLI Commands
```bash
# Add context files
addcontext report.pdf
addexcel data.xlsx

# Execute task
task "Create budget analysis with formulas"

# Configure settings
config iterations 50
config verbose true
```

## Directory Structure

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) - System architecture, modular layers, data flow
- [`DEVELOPMENT_HISTORY.md`](./DEVELOPMENT_HISTORY.md) - Complete evolution timeline
- [`EXTENDING.md`](./EXTENDING.md) - How to add tools, prompts, and models

## Current Status

- 21 Excel MCP tools available
- Dual logging (CSV + Langfuse)
- Auto pipeline with DB + S3 integration
- 206 tasks (148 FMWC + 60 ModelOff + 47 WSP, minus deprecated)
- Multiple models benchmarked via OpenRouter + Anthropic direct
- v10 prompt as current default
