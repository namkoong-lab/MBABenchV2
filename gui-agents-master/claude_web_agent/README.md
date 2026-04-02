# Claude Web Agent

Browser automation for running tasks through Claude.ai and ChatGPT web interfaces.

This module provides Playwright-based automation for interacting with AI web interfaces via Chrome DevTools Protocol (CDP).

## Overview

The Claude Web Agent allows you to:
- Automatically navigate to claude.ai or chatgpt.com
- Submit prompts and capture responses
- Upload files to conversations
- Download generated artifacts (Excel files, etc.)
- Run tasks in batch (sequentially)

## Directory Structure

```
claude_web_agent/
├── __init__.py                 # Package exports
├── claude_web_agent.py         # Claude.ai agent class
├── chatgpt_web_agent.py        # ChatGPT agent class
├── web_agent.py                # Abstract base class
├── browser_manager.py          # Browser setup (Chrome CDP)
├── claude_web_engine.py        # Main per-task entry point
├── completion_logger.py        # JSON crash-safe logging
├── file_validator.py           # Excel file validation
├── task_status.py              # Status enums
└── README.md                   # This file
```

## Quick Start

### 1. Install Dependencies

```bash
uv sync
.venv/bin/python -m playwright install chromium
```

### 2. Run a Single Task

Create a config file (`my_task.yaml`):

```yaml
task_name: "my-test-task"
task_source: "test"

prompts:
  - "Hello! Please confirm you're working."
  - "What is 2 + 2?"

claude_web:
  browser:
    type: "chrome"
    headless: false
  max_wait_per_prompt_seconds: 300
```

Run:

```bash
cd claude_web_agent
python claude_web_engine.py --config my_task.yaml
```

### 3. Run Batch Tasks

```bash
python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --template tasks_configs/template_claude_web.yaml
```

## Browser Setup

### Chrome CDP Mode (Recommended)

Chrome with Chrome DevTools Protocol is recommended because it:
- Bypasses Cloudflare bot detection
- Uses real browser TLS fingerprint
- Maintains persistent login sessions

### Manual Browser Setup

1. Start Chrome with debugging:
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent
```

2. Log in to claude.ai or chatgpt.com manually in the browser

3. Run your tasks -- the agent will use the existing session

## Output Files

After running tasks, you'll find:

```
claude_web_logs/
├── claude_web_20260119_123456_task-name.log  # Execution log
├── json_logs/
│   └── completion_claude_web_20260119_123456_task-name.json  # Timing data
└── conversations/
    └── conversation_20260119_123456_task-name.json  # Full conversation
```

## Troubleshooting

### "Authentication required"
- Log in manually in the browser first
- The agent will detect the login and proceed

### "Could not find input field"
- The web UI may have changed
- Update selectors in `claude_web_agent.py` or `chatgpt_web_agent.py`

### "Rate limit reached"
- Wait and retry
- The engine has built-in retry logic

### Cloudflare blocking
- Use regular Chrome (not headless)
- Ensure `headless: false` in config
