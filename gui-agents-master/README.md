# Web Agent Automation

Automated batch execution of AI agents that work *inside the web chat UIs* of Claude.ai and ChatGPT. The system connects to a real Chrome browser via the Chrome DevTools Protocol, navigates to the chat, uploads task files, sends one or more prompts, and downloads the Excel workbooks the model produces.

> **Looking at the SpreadsheetSmith repo as a whole?** See [`../AGENTS.md`](../AGENTS.md) for an orientation across all agent suites in this repo.

---

## Reproduce the GUI cohorts offline (the default path)

The benchmark's 101 tasks live under [`../data/tasks/`](../data/README.md) (task rows in the repository, workbooks installed from the download; root README, "Task files") and every run writes under `../outputs/`. Nothing on this path needs a database, an object store, or a credential file — only a browser logged in to the provider.

### Setup

```bash
git clone <repo-url>
cd SpreadsheetSmith
uv sync                                          # the workspace environment
uv run python -m playwright install chromium     # Linux: also `install-deps chromium`
```

Then launch regular Google Chrome with remote debugging and log in (see [Launch Chrome and log in](#launch-chrome-and-log-in)). Leave `config/config.yaml` absent: with no database url configured, the runner reads the bundle and writes locally.

### One command per cohort

Every command runs from `gui-agents-master/`. Each config is benchmark v2, prompt version 205 (the House Standards single-pass prompt plus the `House_Standards_v1.md` attachment the version declares), the identity the production lanes ran, the default 5-hour per-task caps, and all 101 tasks.

| Leaderboard cohort (`agent_model_name`) | Browser login | Command |
|---|---|---|
| `claude_fable_5_1_cowork_max` | claude.ai — Fable 5.1, cowork, Max | `uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/claude_fable_5_1_cowork_max.yaml` |
| `claude_opus_5_cowork_max` | claude.ai — Opus 5, cowork, Max | `uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/claude_opus_5_cowork_max.yaml` |
| `chatgpt_gpt_6_astra_work_ultra` | chatgpt.com (Pro) — GPT-6 Astra, work mode, Ultra | `uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/chatgpt_gpt_6_astra_work_ultra.yaml` |
| `chatgpt_gpt_6_pro` | chatgpt.com (Pro) — "Latest" (GPT-6 generation), chat mode, Pro | `uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/chatgpt_gpt_6_pro.yaml` |

The label is derived from the provider settings by [`infra/configs/agent_identity.py`](infra/configs/agent_identity.py), never typed into the config, so a run cannot record itself under a model it did not use. The production runs split the 101 ids across several browsers and accounts; the configs list them all — slice with `--start N --end M`, or run one task with `--task-id N`.

```bash
# Preview first: resolves the task list, the prompt text, the attachment
# list and the output paths for every task, and opens no browser.
uv run python -m infra.run --dry-run --task-id 1 \
  --run-config infra/configs/run_configs/offline/claude_fable_5_1_cowork_max.yaml
```

Re-running a config **resumes**: `skip_already_attempted` reads `outputs/<label>/task_attempts.jsonl` and skips every task that already has a successful row for that label and prompt version (failed rows do not count, so a task that failed is retried). On macOS wrap long runs in `caffeinate -dimsu …` so the machine does not sleep mid-task.

### What a run writes

The record of truth of an offline run is the tree described in [`../data/README.md`](../data/README.md):

```
outputs/
  <agent_model_name>/                    the cohort label verbatim
    task_attempts.jsonl                  one row per attempt — exactly the task_attempts
                                         columns (id, task_id, agent_model_name, …, extra_configs)
    task_id=<N>/<YYYYmmdd_HHMMSS>/       the artifact set: the solution workbook FIRST (the
                                         judged file), every completion JSON, the chat
                                         transcript, the runtime log, then the prompts JSON
                                         (prompt text, version, attachment text + sha256)
```

`id` is the millisecond epoch time of the write, file paths are repo-relative, timestamps carry an offset. The row is built by the same function the database sink uses for its INSERT ([`task_io/sinks/attempt_row.py`](task_io/sinks/attempt_row.py)), so offline rows and database rows have one shape. Failed and timed-out attempts are recorded too (`agent_failed: true` with a reason). The per-attempt staging directory under `scratch/` is deleted once the files are copied.

### Grading the outputs

The judge grades the first `.xlsx` in `attempt_files` against the task's golden solution under `../data/tasks/task_id=<N>/solution_files/`. Point it at `outputs/<label>/task_attempts.jsonl` — the offline replacement for the `task_attempts` table — and it writes `outputs/gradings/` in the layout [`../data/README.md`](../data/README.md) specifies. See [`../judge/README.md`](../judge/README.md) for the judge itself.

### Roots

`local.data_root` (default `data`) and `local.output_root` (default `outputs`) in `config/config.yaml` name the two trees, relative to the repository root; `SPREADSHEETSMITH_DATA_ROOT` / `SPREADSHEETSMITH_OUTPUT_ROOT` override them. A run config's `sink.output_dir`, when set to something other than the default, redirects the output root for that run.

---

## One runner, three ways to feed it

`python -m infra.run` is the entry point for every run. What changes between them is the **run config** you hand it — where tasks come from and where results go.

| `--run-config` says… | Tasks come from | Results go to | Needs |
|---|---|---|---|
| `source.kind: bundle`, `sink.kind: local` (**default**) | the bundled benchmark under `../data/tasks/` | `../outputs/<label>/` | a browser login |
| `source.kind: yaml`, `sink.kind: local` | a YAML file you write ([your own tasks](#your-own-tasks-yaml-source)) | `../outputs/<label>/` | a browser login |
| `source.kind: postgres_s3`, `sink.kind: postgres_s3` (optional) | a Postgres `tasks` table + `s3://<bucket>/` | `s3://<bucket>/` + a `task_attempts` row | database url + object-store keys |

A config that names `postgres_s3` while **no database url resolves** for its benchmark is switched to `bundle` + `local` at startup, with one log line saying so. A configured url is never bypassed: with one present the run goes to the database, without one it goes offline, and neither happens silently.

---

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **Regular Google Chrome** (Chrome Canary v148+ has a CDP compatibility issue with Playwright — stick with the stable channel)
- **Playwright Chromium browser** binaries (installed via `playwright install chromium`)
- **Web GUI login** to your provider — this system uses your existing Claude.ai or ChatGPT browser session, **not** API keys. There's nothing to configure with `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
- For ChatGPT runs: a paid **ChatGPT Plus or Pro subscription** (the leaderboard cohorts used Pro).

`gui-agents-master` is a member of the SpreadsheetSmith uv workspace, so dependencies install from the repo root (`uv sync`). Every command in this README runs from `gui-agents-master/`, prefixed with `uv run` so the interpreter is the one `uv sync` provisioned.

---

## Launch Chrome and log in

The automation connects to a real Chrome browser via the Chrome DevTools Protocol. Launch Chrome with remote debugging enabled, on port 9222 with a dedicated profile directory.

Run these **from `gui-agents-master/`** — the profile lives under the gitignored `browser_profiles/`. The commands below use the Claude lane's profile; for ChatGPT runs swap `chrome-claude` for `chrome-chatgpt`, so each provider keeps its own login. Use regular Chrome, not Canary (see [Troubleshooting](#troubleshooting)).

**macOS:**
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/browser_profiles/chrome-claude" \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

**Linux:**
```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/browser_profiles/chrome-claude" \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --remote-allow-origins=*
```

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$PWD\browser_profiles\chrome-claude" `
  --no-first-run --no-default-browser-check `
  --disable-background-timer-throttling `
  --disable-backgrounding-occluded-windows `
  --disable-renderer-backgrounding `
  --remote-allow-origins=*
```

The `--user-data-dir` flag creates an isolated Chrome profile. Your login session persists across runs as long as you launch Chrome with the same directory — typically a few weeks until cookies expire. Each parallel browser instance needs its own profile dir (and its own port).

This must agree with `<provider>_web.browser.profile_dir` in the run config, which defaults to `browser_profiles/chrome-claude` / `browser_profiles/chrome-chatgpt`. A relative value there is resolved against the repo root, so it names the same profile no matter where you invoke the runner from. The offline cohort configs leave the browser block at its defaults (port 9222, those profile dirs); override `cdp_port` / `profile_dir` in `infra/configs/configs.yaml` (gitignored) or in a copy of the config to run several browsers side by side.

In the Chrome window that just opened:

- **For Claude runs**: navigate to https://claude.ai and log in.
- **For ChatGPT runs**: navigate to https://chatgpt.com and log in (Plus or Pro account).

Leave the browser open. The runner connects to it.

**Project id (optional).** Both providers can start every task inside a "project"; set `<provider>_web.project_id` in the run config to pin that. The leaderboard cohorts ran with `project_id: null` (no project scope), which is what the offline configs say. For Claude.ai copy `{project_id}` out of `https://claude.ai/project/{project_id}`; for ChatGPT copy the hex id after `g-p-` in `https://chatgpt.com/g/g-p-{project_id}-{slug}/project` (`project_slug` is optional).

---

## Your own tasks (yaml source)

A run config is a YAML file under `infra/configs/run_configs/`. If its top level contains task fields (`task_name`, `upload_files`, `tasks`, …) the runner treats the file itself as the task list; everything else in it is overlaid on the project-wide config for that run. Copy [`infra/configs/run_configs/local_run_examples/sample_task.yaml`](infra/configs/run_configs/local_run_examples/sample_task.yaml) and edit:

```yaml
task_name: "My_Analysis"
task_source: "my_tasks"
upload_files:
  - "data/My_Analysis/problem_statement.pdf"
  - "data/My_Analysis/data.xlsx"
solution_name: "My_Analysis_Solution"   # optional

# ── below: project-wide overrides for this run ──
benchmark: v2
prompt_version: 205          # see "Prompts and prompt_version"

provider:
  kind: "claude"

sink:
  kind: local

claude_web:
  model: "opus_4_8"
  project_id: "your-project-id-here"
```

`upload_files` paths are relative to `local_files_base` if set, else to the working directory. You supply the starting workbook — the repo ships no sample `.xlsx` (the blanket `*.xlsx` gitignore rule keeps workbooks out), so edit the placeholder path in `sample_task.yaml` before the first run. To bundle several tasks in one file, use a `tasks:` list instead of top-level task fields — see [`sample_task.yaml`](infra/configs/run_configs/local_run_examples/sample_task.yaml) for the shape.

```bash
# Preview — merges the config, resolves prompts and identity, runs no browser
uv run python -m infra.run --dry-run \
  --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml

# For real
uv run python -m infra.run -y \
  --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml

# Slice the task list
uv run python -m infra.run -y --start 0 --end 5 \
  --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml
```

Results land under `../outputs/<label>/` in the same layout as the offline cohorts. Yaml-sourced tasks have no numeric task id, so `--task-id` and the resume check do not apply to them.

> **Laptop operators (macOS):** these runs drive a real browser for many minutes per task, and if the Mac sleeps it suspends Chrome and drops Wi-Fi mid-generation — the page closes, the run burns a retry, and the whole prompt sequence restarts. Wrap long runs in `caffeinate`:
> ```bash
> caffeinate -dimsu uv run python -m infra.run -y --run-config ...
> ```

---

## Optional: database + object-store tooling

The `postgres_s3` source and sink, the `infra/dispatcher/` EC2 fan-out and the `infra/worker/` box-side loop are the tooling the original runs used against a Postgres database and an object-store bucket. They are in the repo for transparency and for anyone who wants to run the same pipeline at scale, but they are **not needed to reproduce the results** — the offline path above is the supported one.

To use them you need: an AWS account with EC2 permissions, a Postgres database holding the `tasks` and `task_attempts` tables, and an S3 bucket (`s3://<bucket>/SpreadsheetSmith/...`). Put `database.v1_url` / `database.v2_url` and `aws.access_key_id` / `aws.secret_access_key` in `config/config.yaml` at the repo root (gitignored); the run config's `benchmark:` picks which url applies. Example configs: [`infra/configs/run_configs/spreadsheetsmith_run_examples/`](infra/configs/run_configs/spreadsheetsmith_run_examples/) and the tutorial configs [`v2_fable5_claude.yaml`](infra/configs/run_configs/v2_fable5_claude.yaml) / [`v2_sol56_chatgpt.yaml`](infra/configs/run_configs/v2_sol56_chatgpt.yaml). The dispatcher operator guide is [`infra/README.md`](infra/README.md), with the full CLI reference in [`infra/dispatcher/common_commands.md`](infra/dispatcher/common_commands.md). No schema migration ships with the repo; the table shapes are the ones [`../data/README.md`](../data/README.md) documents row by row.

---

## Configuration reference

Config merges in three layers, later winning, all of them project-wide (there is no per-task override layer):

1. [`infra/configs/configs.default.yaml`](infra/configs/configs.default.yaml) — every knob and its default. This is the canonical schema; a key it doesn't declare is rejected.
2. `infra/configs/configs.yaml` — your long-lived local overrides (gitignored: ports, profile dirs, project ids).
3. `--run-config <file>` — what to run this time.

Credentials are not part of that stack: the database url and object-store keys, when a run uses them, come from `config/config.yaml` at the repo root (see [`task_io/registry.py`](task_io/registry.py) for the precedence).

### Prompts and `prompt_version`

The prompt text the agent receives is **not** written in the run config. A run sets `prompt_version`, and [`tasks_configs/prompts/registry.yaml`](tasks_configs/prompts/registry.yaml) maps that number to an ordered list of prompt files, each sent as one chat turn:

| Version | What it sends |
|---|---|
| `0` | Infrastructure smoke test — one turn, returns the workbook plus a `TEST SHEET`. Never grade its output. |
| `9` | The BizbenchV1 (benchmark v1) single-turn payload with the 17-check rubric. |
| `200` | The v2 3-step set: analyze → build (132-check rubric) → QA + download. |
| `201` | The same v2 deliverables and rubric folded into one large turn. |
| `202` | 200 + the Questions-sheet convention: answers go into the starting workbook's `Questions` sheet as live formulas. |
| `203` | 201 + the same Questions-sheet convention, one turn. |
| `204` | The House Standards 3-step set (rubric-free) + Questions-sheet answers; **attaches** `House_Standards_v1.md`. |
| `205` | The House Standards single-pass prompt (rubric-free) + Questions-sheet answers, one turn; same attachment. **The leaderboard cohorts ran this.** |

The same number is written to `task_attempts.prompt_version`, so a row always names the text it was produced from. Registry entries are immutable — new text gets a new number, never an edit to an existing one. See [`tasks_configs/prompts/README.md`](tasks_configs/prompts/README.md).

**Attachments.** A registry entry may declare `attachments:` — files uploaded to the chat after the task's own starting files, on every task of every run of that version (204 and 205 attach `../house_standards/House_Standards_v1.md`). The version selects them; a run config never names the file, so the recorded `prompt_version` and the files the agent saw cannot disagree. The runner resolves them once at startup and refuses to run if one is missing, lists them in `--dry-run` output (the trailing entries of `upload_files`, and a `prompt_attachments` key), and records each one's name, sha256 and full text in the per-attempt `prompts_*.json` that the sink stores — evidence, not a pointer.

`prompt_version` is the only way to choose prompts. To send different text, add it to the registry under a new version — there is no per-run prompt override. The pre-registry keys `prompts_file` and `prompts` are **deprecated** and no longer part of the config schema; a config that still sets one loads with a deprecation warning.

### `benchmark`

`benchmark: v1 | v2` selects which experiment a run belongs to. It picks the identity namespace (see [Agent identity](#agent-identity)), and — on the database path — which url and object-store prefix apply. The offline bundle holds the v2 task set; `source.kind: bundle` refuses a v1 run. Set it explicitly in every run config.

### Task source

- `source.kind: bundle` — the 101 bundled tasks (`../data/tasks/task_id=<N>/task.json` + `starting_files/`). Filters: `task_ids`, `task_sources`, `skip_deprecated`, `skip_already_attempted` (against `outputs/<label>/task_attempts.jsonl`) — the same four the database source applies, in the same order, ordered by id. The starting files are read in place.
- `source.kind: yaml` — tasks you describe yourself (`source.yaml_path`, or a task-shaped `--run-config`).
- `source.kind: postgres_s3` — the database `tasks` table with the same filters (`source.schema: spreadsheetsmith` for v2, `bizbench` for v1).

### Agent identity

`task_attempts.agent_model_name` and the folder segment are derived from the config fields that change agent output, not written by hand — so a row cannot claim a model the run didn't use. The tables live in [`infra/configs/agent_identity.py`](infra/configs/agent_identity.py) and are **append-only**: existing rows point at existing labels. An axis combination with no entry is refused before the browser opens.

- **v2** bifurcates on model, Claude's chat/cowork mode and effort, and ChatGPT's mode plus intelligence (chat) or effort + speed (work).
- **v1** additionally bifurcates on every UI axis that wave pinned.

### Where output lands

- `paths.scratch_dir` (default `scratch/gui-agents`) — the per-attempt working directory the engine writes into. Deleted once the sink has copied its contents.
- `sink.kind: local` — the offline record: `<output root>/<label>/task_id=<N>/<ts>/` plus `<output root>/<label>/task_attempts.jsonl` ([What a run writes](#what-a-run-writes)). The output root is `SPREADSHEETSMITH_OUTPUT_ROOT`, else `local.output_root` in `config/config.yaml`, else `../outputs`; a non-default `sink.output_dir` redirects it.
- `sink.kind: postgres_s3` — uploads the same artifact set to `s3://<bucket>/SpreadsheetSmith/attempts/{agent}/{task}/{ts}_{run_id}/` and inserts one `task_attempts` row. `paths.output_dir` (default `outputs`) additionally keeps a best-effort local mirror under the same relative key path; set it to `""` to disable.

### Model selection

Both providers support model selection through the provider's own UI picker. If omitted or `null`, the runner uses whatever is currently active in your session — benchmark runs must pin it, and v2 preflight refuses `null` for Claude.

**Claude** (`claude_web.model`) — `fable_5_1`, `fable_5`, `opus_5`, `opus_4_8`, `opus_4_6`, `sonnet_4_6`, `haiku_4_5`. `claude_web.effort` (`low` | `medium` | `high` | `xhigh` | `max`) drives the reasoning-effort submenu; `claude_web.mode` (`chat` | `cowork`) drives the Chat/Cowork toggle, which persists across sessions and is therefore asserted on every task; `cowork_approval: auto` approves cowork actions without pausing.

**ChatGPT** — the composer pill splits into two axes, and which one applies depends on `chatgpt_web.mode`:

| `mode` | Keys that apply | Values |
|---|---|---|
| `chat` | `model` + `intelligence` | `model`: `gpt_6` (the "Latest" radio), `gpt_5_6_sol`, `gpt_5_5`, `gpt_5_4`, `gpt_5_3`, `o3` · `intelligence`: `instant`, `medium`, `high`, `xhigh`, `pro` |
| `work` | `model` + `effort` + `speed` | `model`: `gpt_6_astra`, `gpt_5_6_sol`, `gpt_5_6_terra`, `gpt_5_6_luna`, `gpt_5_5` · `effort`: `light`…`ultra` · `speed`: `standard`, `fast` |

Setting the other mode's key is a misconfiguration; preflight rejects it in `work` mode and the agent warns in `chat` mode. `chatgpt_web.model` also accepts three **one-axis** values — `instant`, `thinking`, `pro` — which name an intelligence level rather than a model; they exist so older cohorts can be reproduced.

Selection is **by visible label text**, not a fixed element id — neither provider ships a stable `data-testid` on these rows, so if they relabel a picker the thing to update is the label maps in `claude_web_agent/chatgpt_web_agent.py` / `claude_web_agent/claude_web_agent.py`. The chat-mode `gpt_6` cohort is the strictest case: chat lists the GPT-6 generation only as "Latest", so the agent verifies the composer pill reads exactly `6Pro` before sending and stops the lane otherwise.

> **Heads-up:** if ChatGPT model selection silently fails, a project falls through to its **default** model. Set the project default to something cheap so a missed selection doesn't strand you on a slow tier.

### "Continue" auto-retry

If the model finishes responding but no Excel file appears, the engine can automatically send a "Continue" message asking it to complete the task and provide the file. Both providers allow up to 5 continues.

---

## CLI options (`python -m infra.run`)

| Flag | Default | Description |
|---|---|---|
| `--run-config FILE` | none | Run profile: a task-shaped YAML, or an overlay merged as the 3rd config layer |
| `--dry-run` | off | Resolve the task list, the prompt text, the attachment list and the output paths for every task; print them and the engine configs; open no browser |
| `-y`, `--yes` | off | Skip the interactive "proceed?" confirmation |
| `--start N` | 0 | Start from task index N |
| `--end N` | all | Stop at task index N (exclusive) |
| `--task-id N` | none | Run exactly one task by id (`bundle` or `postgres_s3` source), re-running it even if an attempt exists |
| `--skip-if-attempted` | off | Force `skip_already_attempted`, making an already-attempted task a no-op |
| `--timeout SEC` | none | Per-task timeout override |
| `--auth-precheck` | off | Probe the provider session over CDP first; exit 4 if it's dead |

Exit codes: `0` all attempts succeeded · `1` at least one failed · `2` config/preflight error, nothing attempted · `3` no tasks matched · `4` an environment gate blocked the run (CDP port in use, dead session, or the account hit its usage cap — relaunch the same config after the reset; it resumes).

---

## Running Claude + ChatGPT in parallel

Run both providers simultaneously using two Chrome instances on different ports.

```bash
# Browser A — port 9222 (Claude)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/browser_profiles/chrome-claude" \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &

# Browser B — port 9333 (ChatGPT)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 \
  --user-data-dir="$PWD/browser_profiles/chrome-chatgpt" \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &
```

Log into each provider in its own browser, then run both configs in parallel. Each run config (or `infra/configs/configs.yaml`) must set `<provider>_web.browser.cdp_port` to match its browser:

```bash
uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/claude_fable_5_1_cowork_max.yaml &
uv run python -m infra.run -y --run-config infra/configs/run_configs/offline/chatgpt_gpt_6_astra_work_ultra.yaml &
wait
```

> The runner does **not** auto-launch Chrome on non-default ports (anything other than 9222). Start Chrome yourself on ports like 9333, 9334, etc., and set `cdp_port` to match. Two lanes of the **same** cohort on two browsers can share one `outputs/<label>/` tree — give them disjoint task slices; the resume check and the per-second attempt folders keep their rows apart.

---

## Output structure

Each attempt gets one working directory and one destination folder holding everything it produced:

```
scratch/gui-agents/attempts/{ts}_{task}_p{pid}/   # working dir, deleted after the sink copies it
  solutions/                                      #   downloaded workbooks
  json_logs/                                      #   one completion_*.json per agent attempt
  logs/                                           #   runtime log + chat transcript
  prompts_{task}_{ts}.json                        #   the prompt text actually sent

../outputs/{agent_model_name}/task_id={N}/{ts}/   # the attempt (local sink) — workbook first
../outputs/{agent_model_name}/task_attempts.jsonl # its row
outputs/SpreadsheetSmith/attempts/{agent}/{task}/{ts}_{run_id}/   # local mirror of the object-store prefix (postgres_s3 sink only)
```

---

## Tests

Offline checks — no database, object store, or browser:

```bash
uv run python -m pytest tests/
```

- `tests/test_offline_bundle.py` — parity of the offline path with the database path: the bundle source yields the same TaskSpec as the postgres source's own row-to-spec code (fixture `tests/fixtures/tasks_row_task_1.json`), the prompt payload and attachment list are byte-identical, the local sink's row carries exactly the `task_attempts` columns, and the no-url fallback switches (only) `postgres_s3` configs to bundle + local. Bundle-dependent checks skip when `../data/tasks/` is absent.
- `tests/test_checked_in_configs.py` — loads every run config (including `run_configs/offline/`) and dispatcher template and asserts it merges, resolves prompts, resolves an identity, and clears preflight. Run it after touching anything under `infra/configs/`.

Credential-resolution tests are skipped unless the workspace's monorepo `config` module is importable, since worker boxes deliberately run without it.

---

## Troubleshooting

**Browser session expired.** Re-launch Chrome with the same `--user-data-dir` and log in again. Sessions typically last weeks but can expire after long idle periods.

**Chrome won't start / "port not open".** Make sure no other Chrome instance is using the same `--user-data-dir`:
```bash
lsof -i :9222 -sTCP:LISTEN          # what's on the port
ps aux | grep remote-debugging-port  # all debugging Chrome instances
```

**`Chrome not reachable on CDP port 9222` immediately after launching Chrome.** This is almost always a setup-vs-runtime mismatch — the launch flags and the runner's expectations have drifted. Check that:
- The Chrome you launched uses `--remote-debugging-port=9222` (or whatever is in `<provider>_web.browser.cdp_port`).
- The `--user-data-dir` matches what you used for login (sessions are scoped per profile dir).
- The Chrome binary is regular Chrome, not Canary v148+ (which has a CDP incompatibility — see next entry).
- For parallel runs, the run config's `cdp_port` matches the actual port that browser is on.

**`Protocol error (Browser.setDownloadBehavior): Browser context management is not supported`.** Chrome Canary v148+ incompatibility — switch to regular Chrome.

**`0 artifact preview cards found` (ChatGPT).** The model responded with text only and didn't produce an Excel file. Check the conversation in the browser. ChatGPT's non-agentic web UI sometimes describes the model in text instead of producing a workbook; Work mode is the surface that most reliably produces files.

**`You don't have access to this project` (ChatGPT).** The `project_id` in the run config doesn't match the ChatGPT account logged into that browser. Each account has its own project IDs — update the config with the correct ID from your account's project URL, or leave it `null`.

**Exit code 2 with a `PromptVersionError` or `UnknownAgentCombination`.** The run config's prompt version isn't in the registry, or its provider axes name no identity. Both fail before the browser opens, by design — `--dry-run` reproduces them in a second.

**`bundle task source: .../data/tasks does not exist`.** The offline bundle is not where the runner looks: the repository's `data/` folder, or `local.data_root` / `SPREADSHEETSMITH_DATA_ROOT` if you moved it.

**Playwright not installed.** If you see `playwright._impl._errors.Error: Executable doesn't exist`:
```bash
uv run python -m playwright install chromium
# Linux: also uv run python -m playwright install-deps chromium
```

---

## Architecture

The system follows a composable six-layer pipeline. Green components are user-configurable; blue components are stable framework internals.

![Architecture Diagram](docs/architecture_diagram.png)

| Layer | Role | Key files |
|---|---|---|
| **Input** | Run configs, prompt registry, task source | `infra/configs/`, `tasks_configs/prompts/`, `task_io/sources/` |
| **Orchestration** | Config merge, preflight, per-task subprocess, retry | `infra/run.py` |
| **Engine** | Single-task pipeline (setup → navigate → AI → download) | `claude_web_agent/claude_web_engine.py` |
| **Navigation** | Browser connects to Chrome and navigates to the provider | `claude_web_agent/browser_manager.py` |
| **AI Interaction** | Claude, ChatGPT, or your own agent | `claude_web_agent/claude_web_agent.py`, `chatgpt_web_agent.py` |
| **Output** | Validation, JSON logs, the attempt record | `claude_web_agent/file_validator.py`, `completion_logger.py`, `task_io/sinks/` |

> See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full architecture guide and instructions on adding your own provider.

---

## Project structure

```
gui-agents-master/
├── infra/                            # the runner and its orchestration
│   ├── run.py                        # entry point — one task per engine subprocess
│   ├── configs/                      # configs.default.yaml + run_configs/
│   │   └── run_configs/offline/      # one config per leaderboard cohort (bundle -> local)
│   ├── dispatcher/                   # optional: laptop-side EC2 dispatch CLI + box templates
│   └── worker/                       # optional: box-side worker loop + systemd units
├── task_io/                          # the source/sink seam
│   ├── sources/                      # bundle_source.py, yaml_source.py, postgres_s3.py
│   └── sinks/                        # attempt_row.py (the row), local_sink.py, postgres_s3.py
├── claude_web_agent/
│   ├── claude_web_agent.py           # Claude.ai provider
│   ├── chatgpt_web_agent.py          # ChatGPT provider
│   ├── claude_web_engine.py          # shared per-task engine
│   ├── browser_manager.py            # Chrome CDP connection
│   ├── completion_logger.py          # crash-safe JSON logging
│   ├── file_validator.py             # Excel file validation
│   ├── task_status.py                # status enums
│   └── web_agent.py                  # abstract base class
├── tasks_configs/prompts{,_v2,_pv9}/ # prompt payloads + registry.yaml
├── tests/                            # offline pytest checks (+ fixtures/)
├── docs/                             # architecture diagram + ARCHITECTURE.md
└── pyproject.toml
```
