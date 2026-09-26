# Cheatsheet

How to configure, identify, and launch a run of each pipeline. One-time setup: `./setup.sh`
(uv workspace, installs every member), then `cp config/config.yaml.example config/config.yaml`
and fill in the model keys, then install the task workbooks from the download (`README.md`,
"Task files"). **Offline is the default**: with no database url configured, every
pipeline reads the bundled tasks under `data/tasks/` and writes attempts under
`outputs/<label>/` (`data/README.md`), and the judge reads those rows and writes
`outputs/gradings/`. One ready-made run config per leaderboard cohort sits in each pipeline's
`offline/` config folder; `REPRODUCE.md` lists them with the recorded time and cost. The cloud
profile (`database.*` + `aws.*` in `config/config.yaml`) is optional: it switches every pipeline
to the Postgres + object-store source and sink. Every run sets `benchmark: v1|v2`, which selects
prompts + rubric (and, in the cloud profile, the data stores) **together**; guards refuse
mismatches; v1 has no bundled data. Every v2 prompt version (gui/excel 204/205, cli v15, coding
v13) also attaches `house_standards/House_Standards_v1.md` with the starting files — the version,
not the run config, selects it. Always check the logged source/sink line (`Database: SpreadsheetSmith
(from config/config.yaml database.v2_url)` or the offline `data/` + `outputs/` line) before
letting a run proceed. All registries are **append-only**: never edit an entry that has recorded
runs, add a new one.

## GUI — `gui-agents-master/` (claude.ai / chatgpt.com via Playwright + CDP)

- **Config**: three layers, later wins — `infra/configs/configs.default.yaml` (every knob +
  default) → `infra/configs/configs.yaml` (gitignored machine overrides) → `--run-config <file>`
  (per-experiment overlay). Ready-made: `infra/configs/run_configs/{v1_fable5_claude_cowork,
  v2_fable5_claude, v1_sol56_chatgpt_work, v2_sol56_chatgpt}.yaml` (+ more under
  `spreadsheetsmith_run_examples/`); offline cohorts: `infra/configs/run_configs/offline/<label>.yaml`
  (`source.kind: bundle`, `sink.kind: local`) — edit `source.filters.task_ids` in the one you pick.
- **Identity**: derived, not named — the DB label is a pure function of the behavior-changing
  config fields (provider, mode, model, effort) looked up in append-only Python tables; unknown
  combinations refuse to run. File: `infra/configs/agent_identity.py`. To add: append one entry
  to the benchmark's `_V2_*_IDENTITIES` dict mapping the new axis tuple to `AgentIdentity(label, s3_folder)`.
- **Run**: first start Chrome on the port your run config's `browser.cdp_port` names (9223 =
  the Claude lane; 9222 = the ChatGPT lane). The profile dir is keyed by the port, so a new lane
  only needs a new `PORT=` — Chrome allows one process per profile dir, and a launch against a
  running profile hands off to it and silently ignores the new port. First launch on a new port
  opens a blank profile: log in once there; it persists. Keep the `$HOME` spelling — zsh does not
  expand `~` after `=` (a bare `~/...` creates a literal `./~` dir in whatever cwd you launch from):

```bash
PORT=9223; "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=$PORT \
  --user-data-dir="$HOME/.chrome-web-agent-$PORT" \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

```bash
cd gui-agents-master && uv run python -m infra.run --run-config infra/configs/run_configs/v2_fable5_claude.yaml --dry-run
```

  Drop `--dry-run`, add `-y` for real. Key args: `--task-id N` (one task, re-runs even if
  attempted), `--start/--end` (slice), `--skip-if-attempted`, `--auth-precheck` (probe login, exit 4 if dead).

## CLI — `cli-agents-master/` (raw model APIs + local Excel MCP server)

- **Config**: one self-contained batch YAML, no layering — copy an offline cohort config from
  `examples/offline/<label>.yaml` (`source: local`, `sink: local`) or, for the cloud profile,
  `examples/batch_config_template_auto.yaml` (keep `auto_mode: true`); it sets `benchmark`,
  `agent_model_name`, `prompt_version`, task selection (`tasks:` or `task_filter:`), `max_trials`.
- **Identity**: the config names only `agent_model_name`; that label's stanza in the YAML
  registry pins model, reasoning effort, token limits, base_url and context settings, and the
  run refuses to start if the config sets any of them. Files:
  `excel_cli_agent/agent_identities.yaml` (resolver `excel_cli_agent/agent_identity.py`).
  To add: append a stanza with a new unique label — an unregistered label refuses and prints a paste-ready stanza.
- **Run** (no `--dry-run` — verify the startup banner's database + resolved identity):

```bash
cd cli-agents-master && excel-agent --batch-config my_config.yaml
```

  Long runs: `nohup excel-agent --batch-config my.yaml > run.log 2>&1 &`. Everything else comes
  from the YAML; `EXCEL_AGENT_SKIP_RUBRIC_GUARD=1` forces a deliberate cross-benchmark pairing.
  Needs LibreOffice (`soffice`) installed for formula recalc — startup fails loudly without it.

## Coding — `coding-agents-master/` (Claude Code / Codex CLIs in Docker)

- **Config**: one tiny run YAML — `mode: internal`, `benchmark`, `agent_model_name`.
  Offline cohorts: `run_configs/offline/<label>.yaml` (`source: local`, `sink: local`); cloud
  examples: `run_configs/example_v2_claude.yaml`, `example_codex.yaml`, `example_external.yaml`.
- **Identity**: same one-key pattern — `agent_model_name` resolves in the YAML registry pinning
  `cli` (claude|codex), `model`, `effort`, `extra_args`, `env`; pinned keys in the config refuse.
  Files: `coding_agent/agent_identities.yaml` (resolver `coding_agent/agent_identity.py`).
  To add: append a stanza; label and (cli, model, effort) must each be unique.
- **Run**: `run_task` = **one attempt**; `run_sweep` walks a config's task range and skips tasks
  that already have a row. Docker must be running:

```bash
cd coding-agents-master && uv run python -m coding_agent.run_task --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-max.yaml --task-id 11
cd coding-agents-master && uv run python -m coding_agent.run_sweep --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-max.yaml --dry-run
```

  Key args: `--config` (required), `--task-id` (internal/DB mode); `--task-dir` + `--results-dir`
  (external mode, local folders). Infra failures record nothing — rerun freely.

## Excel — `excel-agents-master/` (Claude/ChatGPT add-ins inside Excel Online)

- **Config**: same three-layer merge as GUI — `infra/configs/configs.default.yaml` →
  gitignored `infra/configs/configs.yaml` → `--run-config`. Offline cohorts:
  `infra/configs/run_configs/offline/<label>.yaml` (`source.kind: bundle`, `sink.kind: local`);
  set `sink: {kind: postgres_s3, schema: spreadsheetsmith}` to record to the cloud profile instead
  (full example in `excel-agents-master/README.md`). One-time: `scripts/setup_chrome.sh` (M365 login on the
  config's port/profile), install both add-ins by hand in that Chrome, then
  `scripts/provision_onedrive.py --stage` → drag into OneDrive web → `--verify`.
- **Identity**: one-key pattern — `agent_model_name` pins provider + `ui_model_label` (Claude
  dropdown) / `thinking_effort` (ChatGPT pill), which the engine selects **and re-reads in the
  UI**, aborting unrecorded on mismatch. File: `agent_identities.yaml` (member root; resolver
  `infra/configs/agent_identity.py`). To add: append a stanza; (provider, ui_model_label, thinking_effort) unique.
- **Run**:

```bash
cd excel-agents-master && uv run python -m infra.run --run-config my_run.yaml --dry-run
```

  Same args as GUI: `-y`, `--task-id`, `--start/--end`, `--skip-if-attempted`, `--timeout` (engine deadman).

## Judge — `judge/`

- **Config**: CLI flags (or `--run-config <yaml>`, which expands to them) +
  `judge/project_configs.yaml` (defaults/limits, env-overridable as `BIZBENCHJUDGE_*`) +
  repo `config/config.yaml` (keys; DB/AWS only for the cloud profile). `--source local|db` /
  `--sink local|db` default to local when no database url resolves. `--benchmark` picks rubric
  (and, in the cloud profile, DB + object store): `judge/prompts/rubrics/rubric_8.json` (v1, classic
  3-stage judge) / `rubric_9.json` (v2 — must be graded with `--agentic`).
- **Identity**: `--model <label>` resolves in the YAML registry, pinning provider (endpoint),
  wire model id, and reasoning effort; the label is stored verbatim in `gradings.grader_model`.
  Files: `judge/judge_identities.yaml` (resolver `judge/utils/judge_identity.py`). To add:
  append a stanza — label and (provider, model, effort) unique; OpenAI models always
  `provider: openai`, never openrouter.
- **Run** (single attempts vs. parallel batch):

```bash
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --source local --sink local --all-local --model openai/gpt-5.6-sol --accuracy-check harness --dry-run
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --agentic --attempt-ids 123 124      # cloud profile
uv run python judge/main_scripts/grade_with_orchestration.py --benchmark v2 --agentic --all-tasks --workers 4
```

  grade_from_db: `--attempt-ids | --task-ids` (one required), `--model <label>`, `--dry-run`,
  `--no-db-write`, `--run-calculation` (LibreOffice recalc first), `--reasoning-effort` (override pin).
  Orchestration: `--all-tasks | --task-ids`, `--workers N`, `--models` (agent cohorts to grade);
  dedups to the latest attempt per (task, model, prompt_version) unless `--no-dedup`.
