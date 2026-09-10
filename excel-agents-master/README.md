# excel-agents — Excel Online add-in pipeline

Drives AI add-ins *inside Excel Online* — the Claude add-in ("Opus 4.6" /
"Sonnet 4.6") and the ChatGPT add-in (Thinking effort Fast/Standard/Heavy) —
through a real Microsoft 365/OneDrive browser session, and records attempts
to the benchmark DB exactly like the gui / cli / coding pipelines
(`task_attempts.agent_model_type = "excel"`).

Ported 2026-08-26 from the public `namkoong-lab/MBABench` excel-agents tree
(@ e17a27e) with the V2 conventions and the verified correctness fixes —
see `plan/excel_agents_port.md` at the repo root for the full decision and
fix record. TabAI/Firefox support was dropped in the port.

## How a task runs

`python -m infra.run` pulls tasks from the benchmark DB, downloads their
starting files from S3, and — per task — spawns `excel_agent/engine.py`,
which attaches to the automation Chrome over CDP and:

1. navigates OneDrive to `<onedrive_base_path>/<task_source>/<task_name>/Task/`
2. opens the task's template workbook (**every attempt** — never a blank
   workbook for a template task) and "Create a Copy" under a standard name
3. opens the add-in panel, pins **and UI-verifies** the identity's model /
   thinking effort (an unverified pin aborts the attempt as infra)
4. attaches the non-workbook starting files (and the version's attachments)
   to the panel composer and sends them **with the first prompt turn** —
   never as a text-less turn, which an agentic add-in treats as "go" on the
   open workbook — then sends the remaining turns, waiting each one out
5. downloads the workbook, validates it (openpyxl), records the exact path

Attempt semantics (coding-agents style): successes and agent failures
(prompt_failed / timeout) are published — agent failures with
`agent_failed=true`; infra failures (nav / Excel UI / panel / download /
runner deadman) are retried in place up to `runner.max_infra_tries` and
**never recorded** — no trial burned, the task stays eligible.

## One-time setup

```bash
uv sync && uv run playwright install   # from the repo root, once

# 1. Chrome + Microsoft 365 session (interactive; handles 2FA).
#    Port/profile/binary come from infra/configs — the engine reads the
#    same config, so setup and runtime cannot drift.
./scripts/setup_chrome.sh

# 2. Install the add-ins once, by hand, in that Chrome:
#    open any workbook -> Add-ins -> add "Claude by Anthropic" and "ChatGPT".

# 3. Provision the OneDrive task tree from the DB + S3 (watch it run):
uv run python scripts/provision_onedrive.py --dry-run
uv run python scripts/provision_onedrive.py --task-sources jp
uv run python scripts/provision_onedrive.py --verify   # writes onedrive_manifest.json
```

DB/AWS credentials come from `<repo>/config/config.yaml` (selected by
`benchmark:`); machine-specific overrides (ports, base path) go in the
gitignored `infra/configs/configs.yaml`. `infra/configs/configs.default.yaml`
documents every key.

## Running

```bash
# ALWAYS dry-run first; check the logged `Database:` line.
uv run python -m infra.run --dry-run --run-config <run.yaml>
uv run python -m infra.run --run-config <run.yaml>
uv run python -m infra.run --task-id 2 --run-config <run.yaml>   # one task
```

A run config names its cohort and (optionally) narrows the task set —
nothing else about the model:

```yaml
agent_model_name: "claude_excel_opus_4_6"
sink:
  kind: postgres_s3
  schema: mbabenchv2
source:
  filters:
    task_sources: ["jp"]
```

## Agent identities

`agent_identities.yaml` (repo-member root) is the append-only registry: one
label = one cohort, pinning `provider`, `ui_model_label` (Claude dropdown
text) / `thinking_effort` (ChatGPT pill label), `agent_folder`, and
`agent_model_type: excel`. Configs may set **only** `agent_model_name`;
setting a pinned key refuses to run, and an unknown label prints a
paste-ready stanza. The resolved settings are stamped into
`task_attempts.extra_configs` (probe + raw SQL — the column is never mapped
in an ORM model) so every row records what it actually ran under.

## Prompts

`tasks_configs/prompts/registry.yaml` maps `prompt_version` → prompt files
(append-only; one key selects the text AND labels the row). Every
benchmark version is **byte-identical** to its gui-agents-master copy
(enforced by `tests/test_prompt_parity.py`) — a gui-vs-excel delta is
attributable to the interface, not the text:

| Version | Set | Files | Attachments |
|---|---|---|---|
| 0 | pipeline smoke test (throwaway rows) | `prompts/v000_test.txt` | — |
| 200 | rubric-v9 3-step | `prompts_v2/` | — |
| 202 | 200 + Questions-sheet answers | `prompts_v3/` | — |
| 203 | 202 folded into one panel turn | `prompts/v2_2.txt` | — |
| 204 | 202 + House Standards | `prompts_v4/` | `../house_standards/House_Standards_v1.md` |
| 205 | 203 + House Standards (**default**) | `prompts/v2_3.txt` | `../house_standards/House_Standards_v1.md` |

A version's `attachments:` (repo-root-relative; `..` reaches the monorepo's
`house_standards/`) are uploaded into the add-in panel after the task's
non-workbook starting files on every run of that version; `infra/run.py`
refuses to start if one is missing. The sent text is snapshotted into each
attempt's prompts JSON (with each attachment's name, path, sha256 and text)
and uploaded to S3, and a `House_Standards_v<N>.md` attachment is stamped
into `task_attempts.extra_configs.house_standards` as
`{version, file, sha256}`.

## Tests

```bash
uv run pytest excel-agents-master/tests   # offline: no browser, no DB
```

Covers the identity registry's refusal semantics, config guards
(benchmark↔schema mismatch, unknown keys, prompt dual-knob), engine-config
assembly (workbook/panel split), the gui prompt-parity byte guard, and
source-level regression guards for the port's fixes (blank-workbook retry,
substring navigation, global browser kills, unscoped upload fallbacks).

## Layout

```
excel-agents-master/
├── agent_identities.yaml        # append-only cohort registry
├── infra/
│   ├── run.py                   # DB-driven runner (the only runner)
│   └── configs/                 # loader + identity + prompt registry code
├── task_io/                     # source/sink seam (postgres_s3 / yaml / local)
├── excel_agent/
│   ├── engine.py                # one attempt of one task (exit 0/1/2/3)
│   ├── chrome_browser.py        # interactive login setup (config-driven)
│   └── core/                    # add-in cores, navigation, browser, files
├── tasks_configs/prompts*/      # prompt registry + registered text
├── scripts/
│   ├── setup_chrome.sh
│   └── provision_onedrive.py    # DB+S3 -> OneDrive tree (+ --verify manifest)
└── tests/                       # offline pytest suite
```
