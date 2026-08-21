# gui-agents-master — Architecture Guide

> How a task becomes a graded workbook, and where to cut in if you want to change something.

![Architecture Diagram](architecture_diagram.png)

Setup, prerequisites, and troubleshooting live in the [README](../README.md). This file is about the seams.

---

## The path of one task

```
run config  ──▶  infra/run.py  ──▶  claude_web_engine.py  ──▶  WebAgent  ──▶  Chrome
   +                  │              (one subprocess per task)      │
configs.default       │                        │                    ▼
   +                  │                        │              claude.ai /
configs.yaml          │                        ▼               chatgpt.com
                      │                  solutions/, json_logs/, logs/
                      │                        │
                      ▼                        ▼
                 TaskSource               AttemptSink
              (yaml | postgres_s3)     (local | postgres_s3)
```

`infra/run.py` owns everything above the engine: the three-layer config merge, prompt resolution, agent identity, preflight, the per-task subprocess, and handing the result to the sink. The engine owns one task inside one browser. Neither imports the other's concepts — the engine sees only a plain dict.

### Layer by layer

| Layer | Where | What it decides |
|---|---|---|
| Config | `infra/configs/` | Every knob. `configs.default.yaml` is the schema; unknown keys are rejected |
| Prompts | `tasks_configs/prompts/registry.yaml` | Which text `prompt_version` sends, as an ordered list of files |
| Identity | `infra/configs/agent_identity.py` | `agent_model_name` + S3 folder, derived from the axes that change output |
| Task source | `task_io/sources/` | Where tasks and their starting files come from |
| Orchestration | `infra/run.py` | Preflight, subprocess, timeout, quality gate, custody of the staging dir |
| Engine | `claude_web_agent/claude_web_engine.py` | Two-tier retry: pipeline phase (browser, auth, upload) vs agent phase (prompts, download, validate) |
| Provider | `claude_web_agent/{claude,chatgpt}_web_agent.py` | Every selector and UI affordance for one provider |
| Attempt sink | `task_io/sinks/` | Where the workbook, logs, and the DB row go |

The two-tier retry in the engine is the load-bearing distinction: a pipeline-phase failure is infrastructure (no completion JSON, no attempt counted), an agent-phase failure is the model's (JSON written, attempt counted). Getting a failure into the wrong tier either hides a broken box or poisons the benchmark with retries.

---

## Example: running your own tasks end-to-end

### 1. Write the prompts

Prompt text is not written in a run config. Add a file (or files) under `tasks_configs/prompts*/`, then add a **new** numbered entry in `tasks_configs/prompts/registry.yaml` naming them in send order — one chat turn per file:

```yaml
  300:
    label: "my experiment"
    description: >
      What this asks for, and why it is not one of the existing versions.
    files:
      - "tasks_configs/prompts/my_step1.txt"
      - "tasks_configs/prompts/my_step2.txt"
```

Never edit an existing entry. Rows in `task_attempts` point at these numbers, so rewriting the text under a live number silently changes what that history means.

### 2. Write a run config

Anything from `configs.default.yaml` can be overridden here. A file with task fields at its top level *is* the task list:

```yaml
# infra/configs/run_configs/local_run_examples/my_run.yaml
task_name: "Q1-Revenue-Analysis"
task_source: "my_tasks"
upload_files:
  - "data/Q1/q1_data.csv"
  - "data/Q1/problem_statement.pdf"
solution_name: "Q1_Revenue_Solution"    # optional

benchmark: v2
prompt_version: 300

provider:
  kind: "claude"

sink:
  kind: local
  output_dir: "outputs/my_tasks"

claude_web:
  model: "opus_4_8"
  effort: "max"
  project_id: null
```

Use a top-level `tasks:` list instead of the single-task fields to bundle several tasks in one file.

### 3. Run it

```bash
uv run python -m infra.run --dry-run --run-config infra/configs/run_configs/local_run_examples/my_run.yaml
uv run python -m infra.run -y        --run-config infra/configs/run_configs/local_run_examples/my_run.yaml
```

`--dry-run` performs the whole merge — config layers, prompt resolution, identity, preflight — and prints the engine config without opening a browser. Every class of config mistake surfaces there in about a second.

### 4. Swap anything

Change `provider.kind` to move to ChatGPT; change `model` to move models; change `prompt_version` to change what is asked. The orchestration, retry logic, and output pipeline stay the same.

---

## Adding your own provider

1. **Subclass `WebAgent`** (`claude_web_agent/web_agent.py`) and implement its abstract methods:

   | Method | Purpose |
   |---|---|
   | `navigate_to_new_chat()` | Open a fresh conversation |
   | `get_state()` | Current agent state (running, ready, auth required, rate limited, error) |
   | `upload_files(file_paths)` | Upload local files into the chat |
   | `submit_prompt(prompt)` | Type and send a prompt |
   | `wait_for_response()` | Wait for the model to finish |
   | `download_all_artifacts()` | Download the files it produced |
   | `get_conversation_history()` | Return the conversation messages |
   | `process_all_prompts(files)` | Orchestrate the full prompt sequence |
   | `ensure_features_enabled()` | Model selection, mode toggles, effort pickers |

2. **Register it** in `create_agent()` in `claude_web_engine.py`:
   ```python
   if provider_key == "my_agent_web":
       return MyAgent(page, config, shutdown_event, completion_logger)
   ```
   and add an entry to `PROVIDER_DEFAULTS` in the same file.

3. **Declare its config block** in `infra/configs/configs.default.yaml` (the loader rejects keys it doesn't declare) and add `my_agent` to `PROVIDER_AGENT_TYPE` in `infra/run.py`.

4. **Add its identity rows** to `infra/configs/agent_identity.py` for each axis combination you intend to run, plus a `_preflight_provider_*` branch in `infra/run.py` that rejects invalid axes before the browser opens.

Steps 3 and 4 are not optional bookkeeping: without them a run either fails the unknown-key check or writes a DB row whose `agent_model_name` doesn't say what actually produced it.

### Selectors

Provider UIs change often. Two conventions keep that survivable:

- A selector carries a `verified live <date>` note, so its age is visible.
- A fallback branch states the condition under which it fires, not the era it came from — "no element carries `data-message-author-role`", not "legacy DOM".

`claude_web_agent/dom_diagnostics.py` dumps the final message's DOM when extraction fails, which is the fastest way to see what the provider is actually serving.

---

## Output

One attempt = one working directory + one destination prefix:

```
scratch/gui-agents/attempts/{ts}_{task}/
├── solutions/                     # downloaded .xlsx
├── json_logs/                     # one completion_*.json per agent attempt
├── logs/                          # runtime log + chat transcript
└── prompts_{task}_{ts}.json       # the prompt TEXT that was sent
```

The prompts JSON records text, not paths — a path stops being evidence the moment the file changes.

The working directory is deleted once the sink reports it has taken custody (`retains_files`). The `local` sink does not copy files elsewhere, so it leaves them in place.

**JSON logs** carry `task_name`, `task_status` (`success` / `agent_failure` / `pipeline_failure`), `duration_seconds`, `attempt_number`, per-prompt timing, the agent name, and the prompt version.

**Validation** (`file_validator.validate_excel_file`) checks each downloaded file: exists, non-empty, openable by `openpyxl`. `infra/run.py` then applies a separate post-run quality gate (`check_output_quality`) that flags obviously-degraded workbooks — it runs *outside* the engine's retry loop, so it records a verdict and never triggers a re-run.

```python
# Example: read logs programmatically
import json
from pathlib import Path

for log in Path("scratch/gui-agents/attempts").glob("*/json_logs/*.json"):
    data = json.loads(log.read_text())
    print(f"{data['task_name']}: {data['task_status']} in {data['duration_seconds']:.0f}s")
```

---

## What you don't need to change

| File | Role |
|---|---|
| `infra/run.py` | Config merge, preflight, subprocess, custody |
| `claude_web_agent/claude_web_engine.py` | Pipeline phases and the two-tier retry loop |
| `claude_web_agent/browser_manager.py` | Chrome discovery, CDP connection, profile dirs |
| `claude_web_agent/completion_logger.py` | Crash-safe JSON logging |
| `task_io/` | The source/sink protocols and their reference implementations |

> Full setup, prerequisites, and troubleshooting are in the [README](../README.md). The cloud/EC2 stack is documented in [`infra/plan.md`](../infra/plan.md).
