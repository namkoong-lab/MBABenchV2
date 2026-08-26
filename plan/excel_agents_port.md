# Plan: port the Excel add-in pipeline (excel-agents) into MBABenchV2

Status: **implemented 2026-08-26** (same day as the decisions below; not
yet committed). `excel-agents-master/` exists as the fourth workspace
member with everything in §A–§H: gui-derived loader/task_io/runner
(pump-thread deadman, CDP flock, staging dirs, prompt snapshots, stale-
connection retry), the YAML identity registry (`agent_identities.yaml`,
cli-style refusals, fail-loud UI verification of `ui_model_label` /
`thinking_effort`), `agent_model_type: excel` with identity settings +
runtime stamps written to `extra_configs` via column-probe + raw SQL,
coding-style attempt semantics (engine exits 0/1/2/3; infra retried in
place via `runner.max_infra_tries`, never recorded), prompt registry with
`prompts_v2` **byte-identical** to gui under version 200 (md5-guarded by
`tests/test_prompt_parity.py`), `scripts/provision_onedrive.py`
(--dry-run/--verify + manifest), and every §G fix (blank-workbook retry,
exact-match navigation, host-frame exclusion, scoped upload fallback,
RUNNING-gated base completion, pre-send Claude baseline, real deadman,
profile-scoped Chrome cleanup only, single prompt_version knob, exact
solution-path recording via the completion JSON). TabAI, Firefox,
auth_handler, pdf_upload, batch_automation_runner, install_plugin, and the
dead config keys were not ported.

Verified on 2026-08-26: 27/27 offline tests pass (`uv run pytest
excel-agents-master/tests`); all modules compile+import; identity refusals
exit 2 with the didactic messages (unknown label prints the stanza; pinned
key rejected by the schema); live `--dry-run` against MBABenchV2 resolved
identity/prompts, logged `Database: MBABenchV2 (from config/config.yaml
database.v2_url)`, downloaded task 2's starting file from S3, and built
the engine config with the correct workbook/panel split
(template_file=BasicGrowth.xlsx, 0 panel uploads).

Still owed (needs a browser + M365 account): `./scripts/setup_chrome.sh`
login, add-in installs, `provision_onedrive.py` against the 68 jp tasks
(selector shakedown expected — it is new OneDrive automation), and the §
Acceptance end-to-end smoke per provider (prompt_version 0, then a real
task; confirm the UI-verify abort path and that a forced nav failure
leaves no DB row). gui-agents' own suite re-ran clean (80/80) after the
port copied from it.

Decisions locked with Pat:
`agent_model_type: excel` (new type); coding-style attempt semantics (infra
failures retried in place, never recorded); one-time OneDrive provisioning
script; the verified correctness fixes land as part of the port; **TabAI is
dropped**; `prompts_v2` runs byte-identical (no adapted prompt build tool);
CDP defaults to 9222 with a working `cdp_port` override.

## The situation

MBABenchV2 has three agent pipelines (gui = web chat, cli = api, coding_cli)
plus the judge. A fourth is wanted: AI add-ins running *inside Excel Online*
— the Claude add-in ("Opus 4.6" / "Sonnet 4.6") and the ChatGPT add-in
(thinking effort Fast/Standard/Heavy) — driven through a real Microsoft
365/OneDrive browser session via Chrome CDP.

The only source is the **public repo** `/Users/pat/MBABench/excel-agents-master`
@ `e17a27e` (~12.8k LOC). This repo's history filtered excel-agents out
entirely (`git log --all -- excel-agents-master` is empty here), and the
public copy is frozen at mid-May 2026 — it predates every V2 convention:
repo-level config, identity registries, the `benchmark:` switch, and the
current gui `task_io/`. So this is a fresh re-port (same precedent as the
judge and cli re-ports of 2026-08-10), not a subtree merge.

A full verified review of the source ran 2026-08-26 (16-agent workflow, 51
unique confirmed defects, 1 critical): see the "MBABench Dual-Repo Review"
artifact and the `mbabench-public-repo` memory. The port fixes the
correctness set (§G) and drops the dead weight rather than carrying it.

Operational shape: local-machine only (needs a real browser + persisted M365
session — no EC2/dispatcher analog), one Chrome instance per run on
`cdp_port`, cost always NULL (subscription pricing, like gui).

## Decisions

- **agent_model_type = `excel`** for all attempts from this pipeline. (The
  public/V1 infra labeled excel attempts `gui`; V2 separates them cleanly —
  the v2 DB has no historical excel rows to stay consistent with.)
- **Attempt semantics = coding-style.** Agent failures (prompt_failed,
  timeout) and successes are recorded (`agent_failed=true` rows included);
  pipeline/infra failures (nav, panel, Excel UI, download) are retried in
  place up to a cap and never produce a DB row — no trial burned on OneDrive
  flakiness. Exit codes: 0 success, 1 agent failure, 2 config_error, 3+
  infra.
- **Providers: Claude + ChatGPT add-ins only.** TabAI (the third-party
  Office-Store add-in this codebase originally grew from) is dropped, and
  with it the whole Firefox path, Google `auth_handler`, and `pdf_upload`.
  The `AIAgentCore` base-class structure stays, so a third add-in is cheap
  to add later.
- **Prompts: gui `prompts_v2` byte-identical** (same three-step sequence the
  gui pipeline sends). The text already fits the add-in context ("in the
  open workbook plus any attached files", "the workbook is the deliverable");
  identical prompts make the web-chat-vs-in-Excel comparison clean. If
  pilots show confusion, an adapted-harness + md5-guarded-rubric variant
  (coding-agents v8 pattern) is the fallback.
- **Tasks reach OneDrive via a one-time provisioning script**, not per-run
  uploads. The runner preflights config/identity/DB only and trusts the
  OneDrive tree; a nav miss at runtime is infra (retried, unrecorded).
- **CDP port 9222 by default.** Collides with gui only if both run on the
  same machine simultaneously; the operator manages ports in that case via
  the `cdp_port` config key (which the port makes actually work — see §G).

## What gets built

### A. Workspace member `excel-agents-master/`

New uv workspace member mirroring sibling layout. Ported and adapted:
`excel_agent/engine.py`, `excel_agent/core/` (ai_agent_base, claude_core,
chatgpt_core, navigation, excel_operations, file_manager, file_organizer,
browser_manager, completion_logger, logging_setup, config_loader),
`scripts/setup_chrome.sh`, `chrome_browser.py`. `task_io/` is **not** taken
from the public copy (stale pre-V2 fork) — it is modeled on the current gui
`task_io/` (benchmark-aware source/sink, schema guards). One DB-driven
runner (`python -m infra.run` shape); the public `batch_automation_runner.py`
is not ported (its dual-counter retry loop is superseded by §Decisions).

### B. Config framework

- `repo_config.py` copy (never-raise / never-write), reading repo
  `config/config_default.yaml` → gitignored `config.yaml`, `${env:VAR}`
  supported; DB/AWS resolve config-first (the only benchmark-aware layer),
  API keys are irrelevant here (no API calls).
- `benchmark: v1|v2` in the run config flips DB URL
  (`database.{v1,v2}_url`), S3 root (`BizbenchV1/` vs `MBABenchV2/`), and
  prompt-version default together. Startup guard parses the DB name
  (bizbench / mbabenchv2) and refuses mismatches, like gui/cli. v2 is the
  default and the only exercised path for now; v1 is switch scaffolding for
  uniformity.
- New config keys under `excel_agents:`: `cdp_port` (default 9222),
  `chrome_binary` (null = auto-detect), `onedrive_base_path` (list of folder
  segments), per-phase timeouts, scratch dir. This replaces the public
  repo's `infra/configs/` loader, its `.env` handling (the ONEDRIVE_* env
  vars were dead config — auth is the persisted browser profile), and the
  template/README config drift.

### C. Agent identity registry

`excel_agent/agent_identities.yaml`, following the cli registry exactly
(one label = one cohort; configs name only `agent_model_name`; the runner
copies pinned fields in and **refuses to start** if the config carries any
of them; unknown label refuses and prints a ready-to-paste stanza;
append-only — see the header comment in
`cli-agents-master/excel_cli_agent/agent_identities.yaml`). Pinned fields
per entry:

- `provider` — `claude_excel_agent` | `chatgpt_excel_agent`
- `ui_model_label` — the add-in dropdown text to select and verify
  ("Opus 4.6", "Sonnet 4.6"; null for ChatGPT)
- `thinking_effort` — ChatGPT only ("Fast"/"Standard"/"Heavy"; null for
  Claude); effort baked into the label, e.g. `chatgpt_excel_heavy`
- `agent_model_type: excel`, `agent_folder` (S3), cost NULL

Uniqueness key: (provider, ui_model_label, thinking_effort). Identity
resolution happens inside the guarded startup path with exit 2 =
config_error (not the gui's outside-the-try mistake). **Selection is
fail-loud:** after driving the dropdown/toggle, the core re-reads the UI
state and aborts as infra if it doesn't match the pin — today a miss logs
one line and the run silently proceeds on whatever model was active.

### D. Runner and sink

- Per task: temp engine config → engine subprocess → exit-code taxonomy →
  local attempt package finalized **before** S3 upload **before** DB insert
  (records survive upload failure; the row points at finalized S3 paths).
- `task_attempts` row: `agent_model_type='excel'`, cost NULL,
  `prompt_version` from a **single** knob (the public dual-knob bug is not
  ported), `extra_configs` stamped via column-probe + raw SQL (never mapped
  in `db/models.py`) with `{browser_channel, cdp_port, provider,
  ui_model_label_verified, thinking_effort}`.
- Sink hardening vs the public code: `publish()` wrapped per-task with
  reconnect-on-stale-connection (Neon drops idle sockets during 2h tasks;
  public code loses the finished attempt and aborts the batch); solution
  file located by exact engine-reported name, not mtime/date-folder
  heuristics; timestamps timezone-aware; starting-file cache keyed on full
  S3 key, not basename.
- The judge needs no changes — attempts land in the same S3/DB shape as gui
  attempts under new `agent_model_name` labels.

### E. Prompt registry

Append-only `tasks_configs/prompts/` + `registry.yaml` (gui pattern), with
the v2 entry referencing the shared `prompts_v2` step files byte-identical.
Prompt text snapshotted to S3 + the local attempt package on every run ("a
path is not evidence").

### F. OneDrive provisioning script

`scripts/provision_onedrive.py` (or similar): reads the v2 `tasks` table +
S3 starting files, builds `<onedrive_base_path>/<task_source>/<task_name>/Task/`
via the existing browser automation, uploads each task's starting files.
`--verify` walks the tree and writes a local manifest (task_id → verified
path, timestamp). Idempotent; `--dry-run` first, per repo discipline.

### G. Correctness fixes included in the port

(References are the public source @ e17a27e; all verified in the 2026-08-26
review.)

1. **Blank-workbook retry (critical)** — `engine.py:421` gates opening the
   template on `attempt_number == 0`; retries run the agent on a fresh blank
   workbook and record SUCCESS. Port opens the template on every attempt.
2. **Substring folder/file matching** — `navigation.py:67` (`round1`
   matches a `round10` row; docstring even promises exact). Exact
   normalized-name match only, for folders and workbook selection.
3. **Frame binding** — `chatgpt_core.py:87` / `claude_core.py:121` pass-2
   frame scan can latch onto Excel's own contenteditable (typing the prompt
   into the graded workbook). Require visibility + exclude Wac/Excel frames.
4. **Unscoped upload fallback** — `claude_core.py:1285` can "successfully"
   set files on Excel's hidden file input. Adopt the ChatGPT core's
   frame-scoped version (`chatgpt_core.py:962`) for both.
5. **Completion detection** — base `wait_for_completion` IDLE fallback
   (`ai_agent_base.py:1186`) can fire before the agent starts (relevant if a
   third add-in ever uses the base path — gate on having seen RUNNING);
   Claude baseline sampled after send (`claude_core.py:929`) converts
   finished responses into timeouts — sample before send.
6. **Enforceable timeouts** — `infra/run.py:295` drains stdout before
   `wait(timeout=)`, so `--timeout` can never fire; `--max-runtime` is a
   documented no-op (`engine.py:989`). Drain on a thread, tree-kill on
   expiry, one real timeout knob.
7. **pkill blast radius** — `kill_all_browser_processes()` pkills every
   chrome/firefox on the machine, invoked at setup, atexit, and mid-run CDP
   recovery (`browser_manager.py:434,468`). Scope kills to the automation
   profile/port only; never auto-invoke the global kill.
8. **CDP port override** — module-level `is_cdp_available()` hardcodes 9222
   (`browser_manager.py:81`, same bug as gui). Health-check the configured
   port.
9. **Sink/provenance** — single `prompt_version` knob, reconnect-on-stale
   publish, exact-name solution location, tz-aware timestamps, full-key
   download cache (all §D).

Docs-only and cosmetic findings from the review are *not* ported (most die
with the dropped code: dead `poll:`/`config_arg`/`runtime.*` keys,
`install_plugin`, `connect_to_chrome_canary`, chromium/webkit half-support,
the broken public test scripts and READMEs).

### H. Tests and docs

- Offline, no-browser tests (script-style per repo norms): identity registry
  rules (unique labels, unique axes, pinned-key refusal, unknown-label
  stanza), config guards (benchmark↔DB-name mismatch refusal), exit-code →
  record/skip mapping, exact-match navigation matcher, prompt snapshot
  contents.
- One dry-run smoke config against the v2 DB (`--dry-run` prints the
  `Database:` line per repo discipline).
- In-repo README for the pipeline. No CLAUDE.md (repo rule).

## Order of work

1. Skeleton: workspace member, repo_config copy, config keys, benchmark
   guard, dry-run path that resolves tasks from the v2 DB. (No browser.)
2. Identity registry + refusal semantics + offline tests.
3. Port engine + cores with §G fixes 1–5, 8; Chrome/CDP setup script.
4. Runner + task_io sink/source with §G 6, 9; attempt packaging; exit-code
   taxonomy end-to-end.
5. Provisioning script + `--verify`; provision the 68 jp tasks.
6. Prompt registry entry + snapshot plumbing.
7. Smoke: one real task per provider end-to-end; then acceptance below.

## Acceptance

- Dry-run refuses: wrong-benchmark DB URL, pinned key in config, unknown
  `agent_model_name`.
- One real task per provider: model/effort pinned **and UI-verified**;
  attempt row has `agent_model_type='excel'`, correct `prompt_version`,
  stamped `extra_configs`; S3 bundle gradeable by the judge unchanged.
- Forced nav failure retries in place and leaves no DB row; forced agent
  failure records `agent_failed=true` with reason.
- A retried attempt demonstrably opens the task template (not a blank
  workbook) — regression check on §G.1.
- gui pipeline still passes its own tests (shared `prompts_v2` untouched).

## Out of scope

- Fixing the public repo itself (separate decision — see the review's
  hygiene section: shipped answer key, README drift).
- Running v1/BizbenchV1 excel cohorts (switch scaffolding only).
- TabAI, Firefox, EC2/fleet integration, per-run OneDrive uploads.
- Judge changes (none needed).
