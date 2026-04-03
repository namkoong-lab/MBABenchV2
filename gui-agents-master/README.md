# Web Agent Automation

Automated pipeline for running AI agents on Excel tasks through browser GUIs. Supports **Claude.ai** and **ChatGPT** (Agent mode and Extended Pro mode) as providers.

## Prerequisites

- **Python 3.10+** (3.12 recommended)
- **[uv](https://docs.astral.sh/uv/)** package manager
- **Google Chrome** installed (regular Chrome recommended; Chrome Canary v148+ has a CDP compatibility issue with Playwright)
- **Playwright Chromium browser** — installed via `playwright install chromium` (see Setup below)
- **Logged-in browser session** for the target provider (claude.ai or chatgpt.com)
- **Claude.ai account** (for Claude runs) or **ChatGPT Plus/Pro subscription** (for ChatGPT runs)

> **Note:** This system automates AI agents through their web GUIs — no OneDrive plugins or Excel add-ins are required. Task files (PDFs, Excel workbooks) are uploaded directly into the chat.

## Architecture

This system follows a composable six-layer pipeline. Green components are user-configurable; blue components are stable framework internals.

![Architecture Diagram](docs/architecture_diagram.png)

**Layers:**

| Layer | Role | Key files |
|-------|------|-----------|
| **Input** | Task definitions, prompt templates, agent parameters | `tasks_configs/templates/*.yaml`, `tasks_configs/examples/*.yaml` |
| **Orchestration** | Batch retry logic, subprocess isolation | `claude_web_batch_runner.py` |
| **Engine** | Single-task pipeline (setup → navigate → AI → download) | `claude_web_agent/claude_web_engine.py` |
| **Navigation** | Browser navigates to claude.ai or chatgpt.com | Engine config |
| **AI Interaction** | Claude, ChatGPT, or your custom agent | `claude_web_agent/claude_web_agent.py`, `chatgpt_web_agent.py` |
| **Output** | Downloaded Excel files, validation, JSON logs | `claude_web_agent/file_validator.py`, `completion_logger.py` |

### Adapting for Your Research

1. **Edit prompt templates** — `tasks_configs/template_claude_web.yaml` (or `template_chatgpt_web.yaml`) contains the prompt sequence sent to the AI. Replace with your own instructions.
2. **Define your task list** — Create a YAML in `tasks_configs/examples/` listing your tasks and files.
3. **Choose your model** — Set `model:` in the template (e.g., `opus_4_6`, `sonnet_4_6`, `haiku`).
4. **Customize validation** — Edit validation checks in `file_validator.py` to match your expected output schema.
5. **Add a new agent** — Extend `WebAgent` base class with provider-specific selectors.

> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full architecture guide.

## Setup

### 1. Install dependencies

```bash
git clone <repo-url> && cd gui-system
uv sync
```

### 2. Install Playwright browsers

Playwright requires browser binaries to be installed separately:

```bash
# Install Chromium (required)
.venv/bin/python -m playwright install chromium

# On Linux, you may also need system dependencies:
.venv/bin/python -m playwright install-deps chromium
```

### 3. Launch Chrome with CDP

The automation connects to a real Chrome browser via the Chrome DevTools Protocol (CDP). You must launch Chrome with remote debugging enabled.

**macOS:**
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

**Linux:**
```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --remote-allow-origins=*
```

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\.chrome-web-agent" `
  --no-first-run `
  --no-default-browser-check `
  --disable-background-timer-throttling `
  --disable-backgrounding-occluded-windows `
  --disable-renderer-backgrounding `
  --remote-allow-origins=*
```

> **Note on `--user-data-dir`**: This creates an isolated Chrome profile. Your login session persists across runs as long as you use the same directory. Each parallel browser needs its own profile directory.

### 4. Log into the provider

In the Chrome window that opens:

- **For Claude runs**: Navigate to https://claude.ai and log in
- **For ChatGPT runs**: Navigate to https://chatgpt.com and log in (requires Plus or Pro subscription)

Leave the browser open. The automation connects to it.

### 5. Set up ChatGPT project (ChatGPT runs only)

The automation navigates to a specific ChatGPT project page to start each task. You need to create a project in ChatGPT first:

1. Go to https://chatgpt.com
2. Click **"Projects"** in the left sidebar
3. Create a new project (e.g. "excel-tasks")
4. Note the project URL -- it looks like: `https://chatgpt.com/g/g-p-{project_id}-{slug}/project`
5. Copy the `project_id` (the hex string after `g-p-`) and `slug` into your template config

**For Extended Pro mode**: In the project settings, set the default model to "Extended Pro" (or "Pro"). This way every new chat in the project will use Extended Pro without needing a toggle.

**For Agent mode**: The automation enables agent mode automatically via the `+` menu before sending the first prompt. No project-level setting needed.

## Task Configuration

Configuration is split into two files:

| File | Purpose | You edit per... |
|------|---------|-----------------|
| **Tasks YAML** (`tasks_configs/examples/`) | Which files to upload, what to name output | Each task / project |
| **Template YAML** (`tasks_configs/`) | Which agent, what prompts, retry/timeout settings | Each agent type |

### Task List Format

```yaml
task_source: "my_tasks"

tasks:
  - task_name: "My_Analysis"

    # LOCAL — Files on your machine to upload into the AI chat.
    # Paths are relative to local_files_base (in template) or CWD.
    upload_files:
      - "tasks/My_Analysis/problem_statement.pdf"
      - "tasks/My_Analysis/data.xlsx"

    # OUTPUT — Base name for the solution file (optional).
    # Omit to default to "{task_name}_Solution_{agent}_Model".
    solution_name: "My_Analysis_Solution"
```

> **Note:** The legacy field name `files_to_upload` still works as a fallback for `upload_files`.

### Template Config

Templates live in `tasks_configs/`. The `prompts` list is what gets sent to the AI — **replace these with your own instructions** for your use case. The default prompts are financial modeling prompts; delete them and write whatever you need.

```yaml
# tasks_configs/template_claude_web.yaml
template:
  agent_type: "claude_web"      # or "chatgpt_web"

  # Base directory for resolving relative paths in upload_files.
  # If omitted, resolved from current working directory.
  # local_files_base: "project_data/"

  prompts:
    - "Analyze the attached dataset and summarize key findings."
    - "Build a model on a new sheet called 'model_main'."
    - "Create an 'answers' sheet with your conclusions."
```

### Recommended directory layout

```
my_tasks/
├── Task-Name-1/
│   ├── problem_statement.pdf
│   └── data.xlsx
├── Task-Name-2/
│   ├── problem_statement.pdf
│   └── starting_model.xlsx
```

## Running Tasks

### Claude (default provider)

```bash
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml
```

### ChatGPT -- Agent mode

Agent mode uses ChatGPT's Code Interpreter to build Excel models. The automation enables it automatically.

```bash
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --provider chatgpt
```

### ChatGPT -- Extended Pro mode (no agent)

Extended Pro uses ChatGPT's extended thinking without Code Interpreter. Set this as the default model in your ChatGPT project settings.

```bash
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --template tasks_configs/template_chatgpt_web.yaml \
  --provider chatgpt
```

### Batch runner options

| Flag | Default | Description |
|------|---------|-------------|
| `--tasks FILE` | required | Path to task list YAML |
| `--template FILE` | auto | Template config (auto-selects by provider if omitted) |
| `--provider` | `claude` | `claude` or `chatgpt` |
| `--start N` | 0 | Start from task index N |
| `--end N` | all | Stop at task index N (exclusive) |
| `--stop-on-failure` | off | Abort on first failure |
| `--dry-run` | off | Preview tasks without executing |
| `--timeout` | none | Default timeout per task in seconds |

### Task list YAML format

```yaml
task_source: "my_tasks"

tasks:
  - "Task-Name-1"
  - "Task-Name-2"
  - "Task-Name-3"
```

Each task entry specifies a task name. Task files (PDFs, Excel starting files) should be listed in the task config or template under `files_to_upload`.

## Model Selection

Both providers support configurable model selection. If omitted or set to `null`, the automation uses whatever model is currently active in your session.

### Claude

```yaml
claude_web:
  model: opus_4_6  # Options: opus_4_6, sonnet_4_6, haiku_4_5
```

| Config value | Claude.ai model |
|---|---|
| `opus_4_6` | Opus 4.6 |
| `sonnet_4_6` | Sonnet 4.6 |
| `haiku_4_5` | Haiku 4.5 |
| `null` / omitted | Uses current session default |

### ChatGPT

```yaml
chatgpt_web:
  model: thinking  # Options: instant, thinking, pro
```

| Config value | ChatGPT model |
|---|---|
| `instant` | Instant 5.3 (everyday chats) |
| `thinking` | Thinking 5.4 (complex questions) |
| `pro` | Pro 5.4 (research-grade) |
| `null` / omitted | Uses current session default |

Model selection happens via the provider's UI dropdown before enabling other features (Extended Thinking, Agent mode). If the specified model is not available, the automation falls back to the current default.

## Running Claude + ChatGPT in Parallel

You can run both providers simultaneously using two Chrome instances on different ports.

### Step 1: Launch two Chrome instances

```bash
# Browser A -- port 9222 (Claude)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent-claude \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &

# Browser B -- port 9333 (ChatGPT)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 \
  --user-data-dir=~/.chrome-web-agent-chatgpt \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &
```

### Step 2: Log into each provider in its browser

### Step 3: Run both in parallel

```bash
# Claude (port 9222)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml &

# ChatGPT (port 9333)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --template tasks_configs/template_chatgpt_web.yaml \
  --provider chatgpt &

wait  # Wait for both to finish
```

## Template Configuration

Templates control agent behavior, timeouts, and retry logic. Key settings:

```yaml
template:
  agent_type: "claude_web"      # claude_web | chatgpt_web

  prompts:
    - "Your full prompt text here..."

  download_artifacts: true       # Download Excel files from chat

  claude_web:                    # Provider-specific section
    model: opus_4_6              # opus_4_6, sonnet_4_6, haiku_4_5, or null
    project_id: "your-project-id-here"

    max_sec_per_task: 7200       # 120 min total timeout

    browser:
      type: "chrome"
      headless: false
      timeout: 30000
      cdp_port: 9222             # Change per browser instance

    retry:
      max_agent_attempts: 3      # Retries for agent-phase failures
      max_total_attempts: 10     # Total retries including pipeline failures
      max_sec_per_attempt: 7200
      sleep_between_retries: 5

    output:
      folder_prefix: "claudeGUI"

    logging:
      level: "INFO"
      save_to_file: true
      log_directory: "claude_web_logs"
```

### How "Continue" works

When the model finishes responding but no Excel file is found, the engine can automatically send a "Continue" message asking the model to complete the task and provide the Excel file.

- **ChatGPT Agent mode**: Up to 5 continues.
- **ChatGPT Extended Pro**: No continues. Extended Pro finishes end-to-end in one response.
- **Claude**: Up to 5 continues.

## Provider Comparison

| | Claude | ChatGPT Agent Mode | ChatGPT Extended Pro |
|---|---|---|---|
| Flag | `--provider claude` (default) | `--provider chatgpt` | `--provider chatgpt` |
| Template setting | `agent_type: claude_web` | `agent_mode: true` | `agent_mode: false` |
| Model selection | `model: opus_4_6` (configurable) | Set in ChatGPT project | Set in ChatGPT project |
| How it works | Extended thinking | Code Interpreter builds Excel | Extended thinking builds Excel |
| Typical task time | 5-15 min | 15-30 min | 15-45 min |
| Log directory | `claude_web_logs/` | `chatgpt_web_logs/` | `chatgpt_web_logs/` |
| Output prefix | `claudeGUI` | `chatgptGUI_agent` | `chatgptGUI_extended` |

## Output Structure

Each run creates a date-prefixed output folder:

```
20260320_chatgptGUI_agent/
  solutions/        # Downloaded Excel artifacts
  json_logs/        # Per-task completion JSON logs

chatgpt_web_logs/   # Detailed per-task log files
```

## Project Structure

```
gui-system/
├── claude_web_batch_runner.py        # Main batch runner (both providers)
├── claude_web_agent/
│   ├── claude_web_agent.py           # Claude.ai provider
│   ├── chatgpt_web_agent.py          # ChatGPT provider
│   ├── claude_web_engine.py          # Shared per-task engine
│   ├── browser_manager.py            # Chrome CDP connection
│   ├── completion_logger.py          # JSON crash-safe logging
│   ├── file_validator.py             # Excel file validation
│   ├── task_status.py                # Status enums
│   └── web_agent.py                  # Abstract base class
├── tasks_configs/
│   ├── template_claude_web.yaml      # Claude base template
│   ├── template_chatgpt_web.yaml     # ChatGPT base template
│   └── examples/
│       └── sample_tasks.yaml         # Example task list
├── pyproject.toml                    # Python dependencies
└── requirements.txt                  # Pip dependencies
```

## Troubleshooting

### Chrome won't start / "port not open"

Make sure no other Chrome instance is using the same `--user-data-dir`:

```bash
# Check what's on the port
lsof -i :9222 -sTCP:LISTEN

# Or check all debugging Chrome instances
ps aux | grep remote-debugging-port
```

### "Protocol error (Browser.setDownloadBehavior): Browser context management is not supported"

This happens with Chrome Canary v148+. Use **regular Chrome** instead of Chrome Canary.

### "Agent mode menu not found"

ChatGPT's UI changes frequently. The agent activates agent mode by clicking the `+` menu (`[data-testid="composer-plus-btn"]`) and selecting "Agent mode". If selectors break, update `chatgpt_web_agent.py`.

### "0 artifact preview cards found"

ChatGPT responded with text only and didn't produce an Excel file. This usually means agent mode didn't engage or the prompt didn't trigger file creation. Check the conversation in the browser.

### "You don't have access to this project"

The `project_id` in the template doesn't match the ChatGPT account logged into that browser. Each ChatGPT account has its own project IDs. Update the template with the correct ID from your account's project URL.

### Browser session expired

Re-launch Chrome and log in again. The `--user-data-dir` flag ensures sessions persist, but they can still expire after extended periods.

### CDP connection error on non-default port

The batch runner will NOT auto-launch Chrome on non-default ports (anything other than 9222). You must start Chrome yourself on ports like 9333, 9334, etc.

### Playwright not installed

If you see `playwright._impl._errors.Error: Executable doesn't exist`, run:

```bash
.venv/bin/python -m playwright install chromium
```

On Linux, also install system dependencies:

```bash
.venv/bin/python -m playwright install-deps chromium
```
