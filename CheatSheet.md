# Cheatsheet

One page: launch a run of each pipeline locally (your own task, no database) or
against your own stores (`docs/data_stores.md`). One-time: `./setup.sh`. Keys go
in the gitignored `config/config.yaml` (`keys.*`) or the matching environment
variable; local runs leave `database.*` and `aws.*` unset. Every run names
`benchmark: v1|v2` (prompts + rubric), one `agent_model_name` (resolved in the
pipeline's append-only identity registry; an unregistered label refuses and
prints the stanza to add), and `prompt_version`. DB runs log a `Database:` line
at startup; read it before letting a run proceed.

## GUI — `gui-agents-master/` (claude.ai / chatgpt.com through a real Chrome)

**Chrome first** (from `gui-agents-master/`; the runner connects over CDP on
port 9222, the default in `infra/configs/configs.default.yaml`). Swap
`chrome-claude` for `chrome-chatgpt` for ChatGPT runs; sign in once in the
window that opens, the profile keeps the login.

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

A second lane needs its own port and profile dir, started by hand, with
`<provider>_web.browser.cdp_port` / `profile_dir` set in that lane's run config.

**(a) Local.** Copy `infra/configs/run_configs/local_run_examples/sample_task.yaml`
(ChatGPT: `sample_task_chatgpt.yaml`) and fill `upload_files` (your workbook +
case PDF). Defaults already local: `provider.kind: claude`, `sink.kind: local`,
`benchmark: v2`, `prompt_version: 205`; override any of them in the same file
(optional `claude_web.project_id`). No API key.

```bash
cd gui-agents-master
uv run python -m infra.run --dry-run --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml
uv run python -m infra.run -y        --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml
```

**(b) DB.** A `postgres_s3` run config, e.g. `infra/configs/run_configs/v2_fable5_claude.yaml`;
narrow with `source.filters.task_ids`. Same command; `--task-id N`, `--start/--end`,
`--skip-if-attempted`, `--auth-precheck`.

## Excel — `excel-agents-master/` (Claude / ChatGPT add-ins inside Excel Online)

Setup once: `./scripts/setup_chrome.sh` (Chrome on `browser.cdp_port` 9222, profile
`browser_profiles/chrome-excel`, sign in to Microsoft 365), then install both
add-ins by hand in that Chrome. No API key. Machine settings (ports, OneDrive
base path) go in the gitignored `infra/configs/configs.yaml`.

**(a) Local.** Put the task workbook in OneDrive at
`<onedrive_base_path>/<task_source>/<task_name>/Task/`. Write a task YAML
(`task_name`, `task_source`, `upload_files` for the non-workbook files) and a run
config with `agent_model_name`, `source: {kind: yaml, yaml_path: <file or dir>}`,
`sink: {kind: local}`.

```bash
cd excel-agents-master
uv run python -m infra.run --dry-run --run-config my_run.yaml
uv run python -m infra.run           --run-config my_run.yaml
```

**(b) DB.** `uv run python scripts/provision_onedrive.py --task-sources jp` (then
`--verify`) to lay the tasks out in OneDrive; run config with
`sink: {kind: postgres_s3, schema: mbabenchv2}` and `source.filters`. Same
command; `--task-id`, `--start/--end`, `--skip-if-attempted`, `--timeout`.

## CLI — `cli-agents-master/` (raw model APIs + local Excel MCP server)

Needs LibreOffice (`soffice`; startup fails without it) and the API key for the
identity's provider: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`
or `FORGE_API_KEY` (or `keys.*`). Labels: `excel_cli_agent/agent_identities.yaml`.
No dry run: read the startup banner.

**(a) Local.** Copy `examples/local/test_local.yaml` (`local_mode: true`); fill
`workspaces[].path` (a folder with the workbook + PDFs), `agent_model_name`,
`results_dir`, `max_iterations`, `prompt_version`.

```bash
cd cli-agents-master && uv run excel-agent --batch-config examples/local/test_local.yaml
```

**(b) DB.** Copy `examples/batch_config_template_auto.yaml` (`auto_mode: true`);
fill `benchmark`, `agent_model_name`, `tasks:` or `task_filter:`, `max_trials`.
Same command with your file. Long runs:
`nohup uv run excel-agent --batch-config my.yaml > run.log 2>&1 &`.

## Coding — `coding-agents-master/` (Claude Code / Codex CLIs in Docker)

Docker running; build the image once:
`cd docker && docker build -t mbabench-coding-agent:v2 --build-arg CLAUDE_CODE_VERSION=2.1.251 --build-arg CODEX_VERSION=0.150.1 .`
Key: `ANTHROPIC_API_KEY` (claude identities) or `OPENAI_API_KEY` (codex).
Labels: `coding_agent/agent_identities.yaml`. One invocation = one attempt.

**(a) Local.** Task folder = `task.yaml` (`task_name`, `task_source`) +
`starting_files/` (workbook + PDF). Config: `run_configs/example_external.yaml`
(`mode: external`, `agent_model_name`).

```bash
cd coding-agents-master && uv run python -m coding_agent.run_task --config run_configs/example_external.yaml --task-dir ./my_task --results-dir ./results
```

**(b) DB.** `run_configs/example_v2_claude.yaml` (`mode: internal`, `benchmark`,
`agent_model_name`):

```bash
cd coding-agents-master && uv run python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11
```

## Judge — `judge/`

Key for the grader's provider (`openai/*` → `OPENAI_API_KEY`, `anthropic/*` →
`ANTHROPIC_API_KEY`, `tensorblock/*` → `FORGE_API_KEY`). Labels:
`judge/judge_identities.yaml`. `--benchmark` picks the rubric (`v1` rubric_8,
`v2` rubric_9 with `--single-pass`). LibreOffice for `--run-calculation`.

**(a) Local.** One folder per attempt (`ai_attempt.xlsx`, `solution/<golden>.xlsx`,
optional `context.pdf`; layout in `judge/README.md`, "Grade a local task folder"):

```bash
JUDGE_SKIP_SUITABILITY=1 uv run python judge/main_scripts/judge.py --benchmark v2 --single-pass --model openai/gpt-5.6-sol -f <folder>
```

Results in `<folder>/judge_results/`. Add `--run-calculation` for workbooks
saved without cached values (openpyxl output).

**(b) DB.**

```bash
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --attempt-ids 123 124 --model openai/gpt-5.6-sol
uv run python judge/main_scripts/grade_with_orchestration.py --benchmark v2 --single-pass --all-tasks --workers 4 --models <agent_model_name ...>
```

grade_from_db: `--attempt-ids | --task-ids`, `--dry-run`, `--no-db-write`,
`--run-calculation`, `--det-checks harness|llm|off`, `--accuracy-check harness|llm`.
Orchestration: `--all-tasks | --task-ids`, `--workers N`, `--models`; latest
attempt per (task, model, prompt_version) unless `--no-dedup`. Neither skips
attempts that already have a grading.
