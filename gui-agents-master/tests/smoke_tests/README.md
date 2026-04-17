# Smoke Tests

Quick end-to-end tests to verify the full pipeline works for each provider. Uses trivial prompts (create Excel sheets, write text) with no file uploads.

## Prerequisites

- Chrome launched with CDP on port 9222 (see main README)
- Logged into Claude.ai (for Claude test) and/or ChatGPT.com (for ChatGPT test)
- Update `project_id` (and `project_slug` for ChatGPT) in the template configs

## How to Run

```bash
# From project root:

# Individual providers
./tests/smoke_tests/run_smoke_claude.sh
./tests/smoke_tests/run_smoke_chatgpt.sh

# Both sequentially
./tests/smoke_tests/run_all_smoke.sh
```

## What Each Test Does

1. Connects to Chrome via CDP
2. Navigates to Claude.ai / ChatGPT.com
3. Opens a new conversation
4. Sends prompts to create an Excel workbook with test sheets
5. Downloads the resulting .xlsx artifact
6. Renames and saves to output directory
7. Saves JSON completion log

## What to Verify

1. Console exits with `SUCCESS`
2. `json_logs/` contains a JSON with `"task_status": "success"`
3. Output directory has a renamed `.xlsx` file

## Settings

- 1 agent attempt (no retries on agent failure)
- 2 total attempts (1 retry for infra flakes)
- 10 min timeout, 2 min per prompt (Claude) / 5 min single prompt (ChatGPT)
- No file uploads
