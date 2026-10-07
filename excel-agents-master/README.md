# excel-agents — Excel Online add-in pipeline

Drives AI add-ins *inside Excel Online* — the Claude add-in (model picked by
its dropdown label, e.g. "Fable 5.1", "Opus 5") and the ChatGPT add-in (model
and thinking-effort pills, e.g. "GPT-5.6 Sol" at "Extra High") — through a real
Microsoft 365 / OneDrive browser session, and records one attempt per task.

The pipeline attaches to a real Chrome over CDP, like `gui-agents-master/`.
The task workbook lives in the signed-in account's OneDrive; the engine opens
it there, copies it, drives the add-in panel, and downloads the result.

## How a task runs

`python -m infra.run` reads the task list (a YAML you write, or the benchmark
DB), and per task spawns `excel_agent/engine.py`, which:

1. navigates OneDrive to `<onedrive_base_path>/<task_source>/<task_name>/Task/`
2. opens the task's template workbook there (every attempt — never a blank
   workbook for a template task) and "Create a Copy" under a standard name;
   with no template it creates a blank workbook in that folder
3. opens the add-in panel, selects **and UI-verifies** the identity's model /
   thinking effort (an unverified selection aborts the attempt as infra)
4. attaches the non-workbook starting files (and the prompt version's
   attachments) to the panel composer and sends them **with the first prompt
   turn**, then sends the remaining turns, waiting each one out
5. downloads the workbook, validates it (openpyxl), records the exact path

Attempt semantics: successes and agent failures (prompt failed / timeout) are
recorded — agent failures with `agent_failed=true`. Infra failures (nav /
Excel UI / panel / download / runner deadman) are retried in place up to
`runner.max_infra_tries` (default 3) and **never recorded**.

## Prerequisites

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)**; `./setup.sh`
  from the repo root (uv workspace + Playwright Chromium), once.
- **Google Chrome** (or Chrome Canary). With `browser.chrome_binary` null the
  engine tries **Canary first**, then regular Chrome
  (`excel_agent/core/browser_manager.py`); set the key in
  `infra/configs/configs.yaml` to pin one.
- A **Microsoft 365 account with OneDrive** (Excel Online).
- The **Claude** and **ChatGPT add-ins installed by hand** in that account,
  and a **vendor sign-in** completed inside each add-in panel you intend to
  use (Anthropic account; OpenAI account).
- **Your own task workbook** (`.xlsx`) and, optionally, a case PDF.

## Local run (your workbook, no database)

### 1. Chrome + Microsoft 365 session

```bash
./scripts/setup_chrome.sh
```

Launches the automation Chrome (port/profile/binary from `infra/configs`,
defaults `9222` / `browser_profiles/chrome-excel`) and leaves it open. In that
window sign in at https://onedrive.live.com (2FA included) until you can browse
files without a prompt. The session persists in the profile; the engine
relaunches Chrome as needed.

### 2. Install the add-ins and sign in (once, by hand)

In that Chrome, open any workbook in Excel Online → **Add-ins** → add "Claude
by Anthropic" and/or "ChatGPT". Open each panel once and complete its sign-in.

### 3. Place the workbook in OneDrive (by hand)

The engine clicks through `onedrive_base_path + task_source + task_name +
"Task"` (default base `My files / mbabench_tasks`). Create that folder chain
in OneDrive web and upload your workbook into `Task/` under its original
filename:

```
My files / mbabench_tasks / <task_source> / <task_name> / Task / <your workbook>.xlsx
```

`task_source` and `task_name` are the values from your run config below.
(`task_source: wallstreetprep` maps to folder `wsp`; any other name is used
as-is.)

### 4. Write a run config

A YAML whose top level has task fields (`task_name`, `upload_files`, …) *is*
the task list; `--run-config` then forces `source.kind: yaml`. Every other key
is overlaid on the project-wide config. Copy
[`infra/configs/run_configs/local_task_example.yaml`](infra/configs/run_configs/local_task_example.yaml):

```yaml
task_name: "My_Task"                 # = the OneDrive folder name
task_source: "my_tasks"              # = the OneDrive folder above it
upload_files:
  - "/path/to/My_Task/starting_file.xlsx"   # local copy of the workbook you uploaded
  - "/path/to/My_Task/case.pdf"             # optional; uploaded into the panel
solution_name: "My_Task_Solution"    # optional

agent_model_name: "claude_excel_fable_5_1"  # one label from agent_identities.yaml
prompt_version: 205                  # the default; see "Prompts"
sink:
  kind: local
  output_dir: "outputs"
```

- The workbook in `upload_files` is used for its **name** — the engine opens
  the file of that name in the OneDrive `Task/` folder. Generic rule: the first
  `.xlsx`/`.xlsm` without "solution" in its name (`fmwc` → `*model.xlsx`,
  `wallstreetprep` → `*-before.xlsx`). Every other listed file is uploaded
  into the add-in panel and must exist locally. Use absolute paths.
- `agent_model_name` is the **only** model-selecting key allowed; the label's
  entry in `agent_identities.yaml` pins provider, UI model label and thinking
  effort.

### 5. Run

```bash
# from excel-agents-master/; always dry-run first
uv run python -m infra.run --dry-run --run-config infra/configs/run_configs/local_task_example.yaml
uv run python -m infra.run -y        --run-config infra/configs/run_configs/local_task_example.yaml
```

`--dry-run` merges the config, resolves the identity and the prompt, checks
the upload files and prints the engine config without touching the browser.

### 6. Where the output lands

```
scratch/excel-agents/attempts/<ts>_<task>/
  solutions/                 # the downloaded workbook
  json_logs/                 # completion_*.json
  general_logs/              # runtime log
  prompts_<task>_<ts>.json   # the prompt text sent (+ attachments, sha256)
outputs/attempts.ndjson      # one JSON line per attempt (sink.output_dir)
```

With `sink.kind: local` the attempt directory is **kept** (the sink records
paths and copies nothing). With `sink.kind: postgres_s3` it is uploaded to S3
and deleted.

## DB-backed run (Postgres + S3)

For a team that keeps tasks in the benchmark Postgres schema and starting
files in S3. Credentials come from `<repo>/config/config.yaml`
(`database.v2_url`, `aws.*`), selected by `benchmark:`; machine overrides
(port, base path) go in the gitignored `infra/configs/configs.yaml`.
`infra/configs/configs.default.yaml` documents every key.

Provision the OneDrive tree from the DB + S3 (watch it run — OneDrive's UI
drifts):

```bash
uv run python scripts/provision_onedrive.py --dry-run             # list the plan
uv run python scripts/provision_onedrive.py --task-sources jp      # automated upload
uv run python scripts/provision_onedrive.py --stage                # or: build the tree under
                                                                   # onedrive_staging/ and drag it into OneDrive web
uv run python scripts/provision_onedrive.py --verify               # writes onedrive_manifest.json
```

An overlay-shaped run config (no task fields) names the cohort and narrows
the task set — nothing else about the model:

```yaml
benchmark: v2
agent_model_name: "claude_excel_fable_5_1"
source:
  kind: postgres_s3
  schema: mbabenchv2
  filters:
    task_sources: ["jp"]
    skip_already_attempted: true
sink:
  kind: postgres_s3
  schema: mbabenchv2
```

```bash
uv run python -m infra.run --dry-run --run-config <run.yaml>   # check the logged `Database:` line
uv run python -m infra.run --run-config <run.yaml>
uv run python -m infra.run --task-id 2 --run-config <run.yaml> # one task (postgres_s3 source only)
```

Each recorded attempt becomes a `task_attempts` row
(`agent_model_type = "excel"`) with the resolved identity settings in
`extra_configs`.

## Agent identities

`agent_identities.yaml` (repo-member root) is the append-only registry: one
label = one cohort, pinning `provider`, `ui_model_label` (Claude dropdown
text) / `thinking_effort` (ChatGPT pill label), `agent_folder` and
`agent_model_type: excel`. Configs may set **only** `agent_model_name`;
setting a pinned key refuses to run, and an unknown label prints a paste-ready
stanza.

| label | add-in | UI selection |
|---|---|---|
| `claude_excel_fable_5_1` | Claude | model "Fable 5.1" |
| `claude_excel_opus_5` | Claude | model "Opus 5" |
| `claude_excel_fable_5` | Claude | model "Fable 5" |
| `claude_excel_opus_4_6`, `claude_excel_sonnet_4_6` | Claude | model "Opus 4.6" / "Sonnet 4.6" |
| `chatgpt_excel_gpt_5_6_sol_xhigh` | ChatGPT | model "GPT-5.6 Sol", thinking "Extra High" |
| `chatgpt_excel_heavy` | ChatGPT | thinking "Heavy" — pins no model; refused at resolve time |

Claude identities also pin the add-in's "Toggle extended thinking" button ON.
The engine selects the identity's model / effort in the panel **and re-reads
it from the UI**; a mismatch aborts the attempt as an infra failure.

## Prompts

`tasks_configs/prompts/registry.yaml` maps `prompt_version` → prompt files
(append-only; one key selects the text AND labels the row). Every version is
**byte-identical** to its gui-agents-master copy (`tests/test_prompt_parity.py`),
so a gui-vs-excel delta is attributable to the interface:

| Version | Set | Files | Attachments |
|---|---|---|---|
| 0 | pipeline smoke test (throwaway rows) | `prompts/v000_test.txt` | — |
| 200 | rubric-v9 3-step | `prompts_v2/` | — |
| 202 | 200 + Questions-sheet answers | `prompts_v3/` | — |
| 203 | 202 folded into one panel turn | `prompts/v2_2.txt` | — |
| 204 | 202 + House Standards, rubric-free | `prompts_v4/` | `../house_standards/House_Standards_v1.md` |
| 205 | 203 + House Standards, rubric-free (**default**) | `prompts/v2_3.txt` | `../house_standards/House_Standards_v1.md` |

A version's `attachments:` (repo-root-relative; `..` reaches the monorepo's
`house_standards/`) are uploaded into the panel after the task's non-workbook
starting files on every run of that version; `infra/run.py` refuses to start
if one is missing. The sent text is snapshotted into each attempt's prompts
JSON with each attachment's name, path, sha256 and text.

## Tests

```bash
uv run pytest excel-agents-master/tests   # offline: no browser, no DB
```

Covers the identity registry's refusal semantics, config guards
(benchmark↔schema mismatch, unknown keys, prompt dual-knob), engine-config
assembly (workbook/panel split) and the gui prompt-parity byte guard.

## Layout

```
excel-agents-master/
├── agent_identities.yaml        # append-only cohort registry
├── infra/
│   ├── run.py                   # runner: yaml or postgres_s3 source, local or postgres_s3 sink
│   └── configs/                 # loader + identity + prompt registry code, run_configs/
├── task_io/                     # source/sink seam (yaml | postgres_s3 / local | postgres_s3)
├── excel_agent/
│   ├── engine.py                # one attempt of one task (exit 0/1/2/3)
│   ├── chrome_browser.py        # interactive login setup (config-driven)
│   └── core/                    # add-in cores, navigation, browser, files
├── tasks_configs/prompts*/      # prompt registry + registered text
├── scripts/
│   ├── setup_chrome.sh
│   └── provision_onedrive.py    # DB+S3 -> OneDrive tree (+ --stage, --verify)
└── tests/                       # offline pytest suite
```
