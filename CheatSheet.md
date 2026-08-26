# Cheatsheet

How to configure, identify, and launch a run of each pipeline. One-time setup: `./setup.sh`
(uv workspace, installs every member). Secrets (DB URLs, AWS keys, API keys) live in the
gitignored `config/config.yaml` (template: `config/config_default.yaml`). Every run sets
`benchmark: v1|v2`, which selects DB + S3 root + prompts + rubric **together**; guards refuse
mismatches — always check the logged `Database:` line (e.g. `Database: MBABenchV2 (from
config/config.yaml database.v2_url)`) before letting a run proceed. All registries are
**append-only**: never edit an entry that has recorded runs, add a new one.

## GUI — `gui-agents-master/` (claude.ai / chatgpt.com via Playwright + CDP)

- **Config**: three layers, later wins — `infra/configs/configs.default.yaml` (every knob +
  default) → `infra/configs/configs.yaml` (gitignored machine overrides) → `--run-config <file>`
  (per-experiment overlay). Ready-made: `infra/configs/run_configs/{v1_fable5_claude_cowork,
  v2_fable5_claude, v1_sol56_chatgpt_work, v2_sol56_chatgpt}.yaml` (+ more under
  `mbabenchv2_run_examples/`) — edit `source.filters.task_ids` in the one you pick.
- **Identity**: derived, not named — the DB label is a pure function of the behavior-changing
  config fields (provider, mode, model, effort) looked up in append-only Python tables; unknown
  combinations refuse to run. File: `infra/configs/agent_identity.py`. To add: append one entry
  to the benchmark's `_V2_*_IDENTITIES` dict mapping the new axis tuple to `AgentIdentity(label, s3_folder)`.
- **Run**: first start Chrome with the logged-in profile, on the port your run config's
  `browser.cdp_port` names (9223 for the Claude profile below; ChatGPT uses its own port + profile):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9223 \
  --user-data-dir=~/.chrome-web-agent-claude2 \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

```bash
cd gui-agents-master && python -m infra.run --run-config infra/configs/run_configs/v2_fable5_claude.yaml --dry-run
```

  Drop `--dry-run`, add `-y` for real. Key args: `--task-id N` (one task, re-runs even if
  attempted), `--start/--end` (slice), `--skip-if-attempted`, `--auth-precheck` (probe login, exit 4 if dead).

## CLI — `cli-agents-master/` (raw model APIs + local Excel MCP server)

- **Config**: one self-contained batch YAML, no layering — copy
  `examples/batch_config_template_auto.yaml` (keep `auto_mode: true`, the DB/S3 pipeline); it
  sets `benchmark`, `agent_model_name`, `prompt_version`, task selection (`tasks:` or
  `task_filter:`), `max_trials`.
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
  Examples: `run_configs/example_v2_claude.yaml`, `example_codex.yaml`, `example_external.yaml`.
- **Identity**: same one-key pattern — `agent_model_name` resolves in the YAML registry pinning
  `cli` (claude|codex), `model`, `effort`, `extra_args`, `env`; pinned keys in the config refuse.
  Files: `coding_agent/agent_identities.yaml` (resolver `coding_agent/agent_identity.py`).
  To add: append a stanza; label and (cli, model, effort) must each be unique.
- **Run**: one invocation = **one attempt** (batching is deliberately left to you); Docker must be running:

```bash
cd coding-agents-master && python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11
```

  Key args: `--config` (required), `--task-id` (internal/DB mode); `--task-dir` + `--results-dir`
  (external mode, local folders). Infra failures record nothing — rerun freely.

## Excel — `excel-agents-master/` (Claude/ChatGPT add-ins inside Excel Online)

- **Config**: same three-layer merge as GUI — `infra/configs/configs.default.yaml` →
  gitignored `infra/configs/configs.yaml` → `--run-config`. Minimal run config =
  `agent_model_name` + source filters; the sink defaults to `local`, so set
  `sink: {kind: postgres_s3, schema: mbabenchv2}` to record to the DB (full example in
  `excel-agents-master/README.md`). One-time: `scripts/setup_chrome.sh` (M365 login on the
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

- **Config**: no run config — CLI flags + `judge/project_configs.yaml` (defaults/limits,
  env-overridable as `BIZBENCHJUDGE_*`) + repo `config/config.yaml` (DB/AWS/keys).
  `--benchmark` picks DB + S3 + rubric: `judge/prompts/rubrics/rubric_8.json` (v1, classic
  3-stage judge) / `rubric_9.json` (v2 — must be graded with `--agentic`).
- **Identity**: `--model <label>` resolves in the YAML registry, pinning provider (endpoint),
  wire model id, and reasoning effort; the label is stored verbatim in `gradings.grader_model`.
  Files: `judge/judge_identities.yaml` (resolver `judge/utils/judge_identity.py`). To add:
  append a stanza — label and (provider, model, effort) unique; OpenAI models always
  `provider: openai`, never openrouter.
- **Run** (single attempts vs. parallel batch):

```bash
python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 123
python judge/main_scripts/grade_from_db.py --benchmark v2 --agentic --attempt-ids 123 124
python judge/main_scripts/grade_with_orchestration.py --benchmark v2 --agentic --all-tasks --workers 4
```

  grade_from_db: `--attempt-ids | --task-ids` (one required), `--model <label>`, `--dry-run`,
  `--no-db-write`, `--run-calculation` (LibreOffice recalc first), `--reasoning-effort` (override pin).
  Orchestration: `--all-tasks | --task-ids`, `--workers N`, `--models` (agent cohorts to grade);
  dedups to the latest attempt per (task, model, prompt_version) unless `--no-dedup`.
