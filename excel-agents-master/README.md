# excel-agents — Excel Online add-in pipeline

Drives AI add-ins *inside Excel Online* — the Claude add-in (model dropdown
"Fable 5.1" / "Opus 5" / ...) and the ChatGPT add-in (combined "Model and
thinking effort" menu) — through a real Microsoft 365/OneDrive browser
session, and records each attempt as a `task_attempts` row
(`agent_model_type = "excel"`) exactly like the gui / cli / coding pipelines.

It runs **offline by default**: tasks come from the bundle under
`<repo>/data/tasks/` (workbooks installed from the download; root README, "Task files") and attempts are written under `<repo>/outputs/`. No
database, no object store. What it cannot avoid is Excel Online itself — the
add-ins only exist inside a signed-in Microsoft 365 browser session, so
"offline" here means DB/S3-free, not network-free.

Ported 2026-08-26 from the original public SpreadsheetSmith excel-agents tree with the
V2 conventions and the verified correctness fixes. TabAI/Firefox support was
dropped in the port.

## Offline run (the reproducible path)

### 1. Environment

```bash
uv sync && uv run playwright install      # from the repo root, once
cd excel-agents-master
uv run pytest tests -q                    # offline: no browser, no DB, no network
```

### 2. Browser and accounts (the one-time, hands-on part)

The add-ins run inside Excel Online, so a run needs:

* a Microsoft 365 account with OneDrive (Excel Online is where the workbook
  is opened and the add-in panel lives);
* the add-in's own sign-in: the Claude add-in needs a paid consumer Claude
  account, the ChatGPT add-in a paid ChatGPT account, each signed in once
  inside the add-in panel of the automation Chrome profile. The vendors'
  **weekly usage caps** apply per account — a full 101-task cohort does not
  fit in one account-week on either add-in, so plan lanes/accounts
  accordingly (a quota notice looks like an ordinary reply; the engine
  treats short vendor notices as infra and holds, but watch the first tasks).

```bash
# Chrome + Microsoft 365 session (interactive; handles 2FA). Port/profile
# come from infra/configs — the engine reads the same config, so setup and
# runtime cannot drift.
./scripts/setup_chrome.sh

# Install the add-ins once, by hand, in that Chrome:
#   open any workbook -> Add-ins -> add "Claude by Anthropic" and "ChatGPT",
#   then sign in to each inside its panel.
```

Machine-specific settings (`browser.cdp_port`, `browser.profile_dir`,
`onedrive_base_path`) go in the gitignored `infra/configs/configs.yaml`;
`infra/configs/configs.default.yaml` documents every key and supplies
working defaults.

### 3. Put the task workbooks on OneDrive

The engine opens each task's template workbook *from OneDrive by name* (never
a blank workbook for a template task), so the workbooks in `data/tasks/`
have to be placed at `<onedrive_base_path>/<task_source>/<task_name>/Task/`
once per account. `scripts/provision_onedrive.py` reads them from the bundle:

```bash
uv run python scripts/provision_onedrive.py --dry-run      # the plan, nothing touched
uv run python scripts/provision_onedrive.py --stage        # builds the exact tree under
                                                           # onedrive_staging/ for ONE drag into
                                                           # OneDrive web (no browser automation)
uv run python scripts/provision_onedrive.py --verify       # read-only check + onedrive_manifest.json
```

(`provision_onedrive.py` without flags drives the upload through the
automation Chrome instead; OneDrive's UI drifts, so watch it.)

### 4. One command per cohort

The three leaderboard cohorts each have a run config under
`infra/configs/run_configs/offline/`, named after the cohort label. Each
pins `benchmark: v2`, the cohort's identity by reference, `prompt_version:
205`, the task range 1–101, `source.kind: bundle` and `sink.kind: local`.
**Always dry-run first** — it resolves the tasks, prints the prompt text,
each task's attachment list and where the sink would write, and exits
before any browser, OneDrive, add-in or provider work:

```bash
uv run python -m infra.run --dry-run --run-config infra/configs/run_configs/offline/claude_excel_fable_5_1.yaml

uv run python -m infra.run --run-config infra/configs/run_configs/offline/claude_excel_fable_5_1.yaml
uv run python -m infra.run --run-config infra/configs/run_configs/offline/chatgpt_excel_gpt_5_6_sol_xhigh.yaml
uv run python -m infra.run --run-config infra/configs/run_configs/offline/claude_excel_opus_5.yaml

uv run python -m infra.run --task-id 2 --run-config <run.yaml>   # one task, ignores resume
```

Runs resume: `skip_already_attempted` skips every task that already has a
live, non-failed row for that label at that prompt_version in the cohort's
`task_attempts.jsonl` (the same rule the database join applied), so a lane
can be stopped and relaunched.

### 5. Where outputs land

Under `<repo>/outputs/` (gitignored; `local.output_root` in
`<repo>/config/config.yaml` or `SPREADSHEETSMITH_OUTPUT_ROOT` move it), the layout
`<repo>/data/README.md` fixes:

```text
outputs/<agent_model_name>/
  task_attempts.jsonl                   one row per attempt, exactly the task_attempts
                                        columns (id, task_id, agent_model_name,
                                        agent_model_type, attempt_files, prompt_files,
                                        start_time, end_time, time_taken_min, cost,
                                        prompt_version, agent_failed, agent_failed_reason,
                                        deprecated, created_at, context_reduced,
                                        deprecated_reason, updated_at, extra_configs)
  task_id=<N>/<YYYYmmdd_HHMMSS>/        the artifact set: the solution workbook FIRST
                                        (the judged file), then the completion JSON and
                                        the .log, then the prompts JSON (prompt text,
                                        prompt_version, attachment name/sha256/text)
```

`attempt_files` / `prompt_files` are repo-relative POSIX paths under that
folder, `id` is the millisecond epoch time of the write, timestamps are
ISO-8601 with an offset, and `extra_configs` carries the identity settings
plus the per-attempt stamps (`cdp_port`, `infra_tries`, `engine_task_status`,
`house_standards {version, file, sha256}`). The row is built by the same
`attempt_row()` the cloud sink uses for its INSERT
(`task_io/sinks/attempt_row.py`). The previous local sink's
`attempts.ndjson` line (a raw dump of the runner's result object) is gone;
`task_attempts.jsonl` replaces it.

### 6. Grading

The judge looks attempts up in every `outputs/**/task_attempts.jsonl` an
offline run wrote, so a fresh cohort is graded with the judge's own offline command (from the repo root;
the grader API key is the only credential):

```bash
python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass \
    --source local --sink local --all-local --dry-run    # what would be graded
python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass \
    --source local --sink local --all-local              # grade every local row
```

It grades the first `.xlsx` in `attempt_files` against the bundled golden in
`data/tasks/task_id=<N>/solution_files/` and appends to
`outputs/gradings/gradings.jsonl` — see `<repo>/judge/README.md` ("Grade
offline") and `<repo>/data/README.md` ("outputs/").

## How a task runs

Per task, `infra/run.py` spawns `excel_agent/engine.py`, which attaches to
the automation Chrome over CDP and:

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

## Task source and attempt sink

`task_io/` is the seam. `source.kind` / `sink.kind` pick the backend:

| kind | source | sink |
|---|---|---|
| `bundle` | `data/tasks/task_id=<N>/task.json` + `starting_files/` read in place; filters `task_ids`, `task_sources`, `skip_deprecated`, `skip_already_attempted` (against `outputs/<label>/task_attempts.jsonl`); ordered by id | — |
| `local` | — | the `outputs/` layout above |
| `postgres_s3` | the benchmark DB + `<bucket>` (needs credentials) | S3 upload + `task_attempts` INSERT |
| `yaml` | an ad-hoc task YAML | — |

Selection: an explicit kind in `configs.yaml` or the run config always
stands. A run config that leaves the `postgres_s3` defaults in place while
**no database url resolves** for its benchmark is switched to `bundle` +
`local` and the runner logs one line saying so; when a url resolves,
nothing changes.

The bundle source yields the same `TaskSpec` the cloud source yields for a
row — same `task_id` / `task_name`, the starting files in row order under
their original names, the same metadata keys — so the prompt payload and the
attachment list the add-in receives are byte-identical either way
(`tests/test_offline_bundle.py` checks this against a saved `tasks` row).

## Agent identities

`agent_identities.yaml` (repo-member root) is the append-only registry: one
label = one cohort, pinning `provider`, `ui_model_label` (the add-in's model
item text) / `thinking_effort` (ChatGPT's effort label), `agent_folder`, and
`agent_model_type: excel`. Configs may set **only** `agent_model_name`;
setting a pinned key refuses to run, and an unknown label prints a
paste-ready stanza. The resolved settings are stamped into every row's
`extra_configs` so a row records what it actually ran under.

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
and stored with the attempt, and a `House_Standards_v<N>.md` attachment is
stamped into `extra_configs.house_standards` as `{version, file, sha256}`.

## Cloud path (optional tooling)

With credentials, the same runner records to the benchmark database and
`<bucket>` instead. Credentials come from `<repo>/config/config.yaml`
(`database.v1_url` / `database.v2_url`, selected by `benchmark:`; `aws.*`);
never from boto3's ambient chain. A run config opts in explicitly:

```yaml
agent_model_name: "claude_excel_opus_5"
source:
  kind: postgres_s3
  schema: spreadsheetsmith
sink:
  kind: postgres_s3
  schema: spreadsheetsmith
```

`--dry-run` then logs the resolved `Database:` target; check it before a
real run. `scripts/provision_onedrive.py` reads the DB + S3 in that mode.

## Tests

```bash
uv run pytest excel-agents-master/tests   # offline: no browser, no DB, no network
```

Covers the identity registry's refusal semantics, config guards
(benchmark↔schema mismatch, unknown keys, prompt dual-knob), engine-config
assembly (workbook/panel split), the gui prompt-parity byte guard, the
offline parity suite (bundle vs cloud `TaskSpec`, byte-identical prompt
payload and attachment list, the local sink's row shape, resume, the
offline selection rule, the three run configs, a full `--dry-run`), and
source-level regression guards for the port's fixes.

## Layout

```
excel-agents-master/
├── agent_identities.yaml        # append-only cohort registry
├── infra/
│   ├── run.py                   # the runner (bundle/local by default; --dry-run)
│   └── configs/                 # loader + identity + prompt registry code
│       └── run_configs/offline/ # one run config per leaderboard cohort
├── task_io/                     # source/sink seam
│   ├── local_layout.py          # data/ and outputs/ path conventions
│   ├── sources/                 # bundle_source / postgres_s3 / yaml_source
│   └── sinks/                   # local_sink / postgres_s3 / attempt_row (shared row)
├── excel_agent/
│   ├── engine.py                # one attempt of one task (exit 0/1/2/3)
│   ├── chrome_browser.py        # interactive login setup (config-driven)
│   └── core/                    # add-in cores, navigation, browser, files
├── tasks_configs/prompts*/      # prompt registry + registered text
├── scripts/
│   ├── setup_chrome.sh
│   └── provision_onedrive.py    # bundle (or DB+S3) -> OneDrive tree (+ --stage / --verify)
└── tests/                       # offline pytest suite (+ fixtures/tasks_row_task_1.json)
```
