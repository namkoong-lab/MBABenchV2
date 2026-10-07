# Claude Web Agent

Browser automation for running tasks through Claude.ai and ChatGPT web interfaces.

This module provides Playwright-based automation for interacting with AI web interfaces via Chrome DevTools Protocol (CDP).

## Overview

The Claude Web Agent allows you to:
- Automatically navigate to claude.ai or chatgpt.com
- Submit prompts and capture responses
- Upload files to conversations
- Download generated artifacts (Excel files, etc.)

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
├── dom_diagnostics.py          # Final-message DOM dumps for selector drift
└── README.md                   # This file
```

## Quick Start

### 1. Install Dependencies

From the repo root:

```bash
uv sync
uv run python -m playwright install chromium
```

### 2. Run Tasks

`python -m infra.run --run-config <file>` is the supported entry point — it
merges the config layers, resolves the prompt version and agent identity,
and invokes this engine once per task. See the [repo README](../README.md).

### 3. Run the Engine Standalone

For provider-level debugging you can drive the engine directly with a
hand-written engine config. It skips the config loader, the prompt
registry, agent identity, and the source/sink, so its output is **not**
benchmark data.

```yaml
# my_task.yaml
task_name: "my-test-task"
task_source: "test"
prompt_version: 0

prompts:
  - "Hello! Please confirm you're working."
  - "What is 2 + 2?"

claude_web:
  browser:
    type: "chrome"
    headless: false
  max_wait_per_prompt_seconds: 300
```

```bash
uv run python claude_web_agent/claude_web_engine.py --config my_task.yaml --no-hold
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
# from the repo root; swap chrome-claude for chrome-chatgpt on ChatGPT runs
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/browser_profiles/chrome-claude"
```

2. Log in to claude.ai or chatgpt.com manually in the browser

3. Run your tasks -- the agent will use the existing session. On port 9222
   the runner launches Chrome itself if nothing is listening; on any other
   port you must start it as above.

## Output Files

Every run gets one working directory (`create_run_directory` in
`claude_web_engine.py`): under `paths.scratch_dir` (`scratch/gui-agents/attempts/<ts>_<task>_p<pid>/`)
when `infra.run` drives the engine, or a date-stamped folder named with
`<provider>_web.output.folder_prefix` under `output.base_dir` for a standalone `--config` run.

```
<run dir>/
├── solutions/      # the downloaded workbooks
├── json_logs/      # completion_*.json, one per agent attempt (status, timing, prompt version)
└── logs/           # the runtime .log and conversations/ (the chat transcript)
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
