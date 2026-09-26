# coding-agents-master

Run SpreadsheetSmith financial-modeling tasks through **vendor coding agents** — Claude
Code (Anthropic) and Codex (OpenAI) — each attempt in its own locked-down
container, producing one Excel workbook per task for the standard SpreadsheetSmith judge.

Everything a run needs is in the repository or the task-file download (root README, "Task files"): **`python -m
coding_agent.run_sweep --config run_configs/offline/<cohort>.yaml`** reads the
bundled task inputs under `data/` and writes attempts under `outputs/`. No
database, no object store, no credentials beyond the model API key the agent
itself spends. See **Running** below; the cloud path is still there and
unchanged for whoever has the credentials.

> **Scope note:** this pipeline runs against either benchmark — set
> `benchmark: v1|v2` in the run config (required for internal runs; there
> is no default). v1 targets the V1 206-task set (the pv9-mirror v7
> template, the 3-category rubric); its inputs are not part of the offline
> bundle, so it runs against the database and object store only. v2 targets
> the 101-task set under `data/` (the v13 template: rubric-free, the
> Questions-sheet answer convention and the House Standards pointer, graded
> by the agentic judge). The benchmark key picks the database URL, the
> object-store root (`s3://<bucket>/<BizbenchV1|SpreadsheetSmith>/…`), the offline
> roots and the template together; the template picks its attachments. It is
> the third agent surface alongside the GUI pipeline (vendor chat products)
> and CLI pipeline (raw model APIs in a purpose-built harness).

## How it differs from the CLI pipeline

The old CLI pipeline *was* the agent: it serialized the workbook to text,
asked the model for one edit at a time, and applied edits with its own code.
Here the vendor ships the whole agent — Claude Code / Codex read files
themselves, write and run their own code, and iterate. This pipeline is only
the proctor: seed a workspace, start the agent in a sandbox, validate what
came back, record it.

```
task store -> workspace prep -> sandboxed agent attempt -> validation -> record -> judge
(unchanged)   (this folder)      (this folder)             (this folder)  (unchanged conventions)
```

## Layout

```
coding_agent/            The single-task runner package
  run_task.py            Entry point: one invocation = one attempt of one task
  run_sweep.py           Every task in a run config's range, one attempt each
  config.py              YAML run config + secrets resolution (fail-fast validation)
  repo_config.py         Reads <SpreadsheetSmith>/config/config.yaml (DB URLs, AWS, keys)
  agent_identity.py      agent_model_name -> pinned cli/model/effort (registry below)
  agent_identities.yaml  THE registry of cohort labels and what each one runs
  task_source.py         bundled data/ (offline) / database + object store (read-only
                         on tasks) / external (a local task folder)
  workspace.py           Per-attempt workspace + seeded-file sha256 manifest (+ template attachments)
  prompt_builder.py      PROMPT.md assembly + prompt_version accounting
  agents.py              claude / codex headless command construction
  sandbox.py             Docker (or host, dev-only) execution, wall-clock kill
  telemetry.py           Per-turn token usage + cost from the CLI's own output
  validate.py            Success criteria + failure taxonomy (see below)
  recorder.py            Chooses the sink: local (offline) / DB row + object-store
                         upload (cloud) / results folder (external)
  local_sink.py          The offline sink: outputs/<cohort>/… + task_attempts.jsonl
  prompts/               System wrapper + task templates (see Prompts)
docker/                  Sandbox image: pinned CLIs + default-deny egress firewall
run_configs/             Example YAML configs (prod configs are untracked)
  offline/               One config per leaderboard cohort, ready to run offline
tools/                   build_v8/v9/v12/v13_template.py + build_v10_v11_templates.py (v2 template generators), validate_trajectory.py
tests/                   Offline tests (no Docker/DB/keys needed)
```

## Setup

1. **Docker** (Docker Desktop on macOS) — the sandbox runtime.
2. Python deps: `pip install -e .` — or, from the repository root, the
   workspace install (`setup.sh`), which also makes the shared `config`
   module importable.
3. Build the sandbox image (pin CLI versions for a wave):

```bash
cd docker && docker build -t spreadsheetsmith-coding-agent:v2 \
  --build-arg CLAUDE_CODE_VERSION=2.1.251 --build-arg CODEX_VERSION=0.150.1 .
```

   The tag is recorded per attempt (`extra_configs.sandbox_image`) as the
   CLI-version pin — use a new tag whenever a rebuild changes the contents.

4. **Agent API key** — the only secret an offline run needs, and never in a
   run config or a workspace: `ANTHROPIC_API_KEY` (claude) /
   `OPENAI_API_KEY` (codex) from the environment or a `.env` next to
   `coding_agent/`, falling back to `keys.anthropic_api_key` /
   `keys.openai_api_key` in `config/config.yaml`. An identity that reaches a
   model gateway is keyed separately (`FORGE_API_KEY` / `keys.forge_api_key`)
   and never falls back to the vendor key.
5. **Where inputs and outputs live** — `local.data_root` (default `data/`)
   and `local.output_root` (default `outputs/`) in `config/config.yaml`, or
   `SPREADSHEETSMITH_DATA_ROOT` / `SPREADSHEETSMITH_OUTPUT_ROOT` in the environment. A
   relative value resolves against the repository root. See `data/README.md`
   for the bundle layout and the row shapes.

### Optional: the cloud path

Only for whoever holds the credentials; nothing above needs it.
`config/config.yaml` supplies the database URLs and object-store credentials
(`database.v1_url` / `database.v2_url`, `aws.access_key_id` /
`aws.secret_access_key`, `aws.s3_bucket`); the run config's `benchmark` picks
the URL, so nothing is swapped between v1 and v2 runs. On a standalone
checkout (no `config` module) `DATABASE_URL` and boto3's default chain are
the fallback, and the URL is checked against the benchmark. The `tasks` table
is read-only to this pipeline; only `task_attempts` is written.

## Run configs and agent identities

A run config names its cohort with **one** key, `agent_model_name`, and says
nothing else about the agent:

```yaml
mode: internal
benchmark: v2
agent_model_name: claudecode_anthropic/claude-haiku-4-5
```

The entry for that label in `coding_agent/agent_identities.yaml` pins `cli`,
`model`, `effort`, `extra_args` and `env`. The runner refuses to start if the
config carries an `agent:` block (or the old `identity:` / `internal:` keys),
if the label is unregistered (it prints the stanza to add), or if two
registry entries share a label or a `(cli, model, effort)` combination. The
label is what the judge, `get_results` and the paper tables group rows by, so
every row under one label is guaranteed to have run the same way. To run
different settings, add a new entry with a new label — don't edit an existing
one.

Everything else in a run config is a run setting with a default:
`template_version` (v7 for v1, v13 for v2), `sandbox` (mode/image/cpus/memory),
`limits` (wall clock, junk guard), `record_trajectory`, `workspaces_dir`,
`tasks` (the `first`/`last` id range a sweep walks) and `source` / `sink`
(`local` | `cloud` | `auto`, see **Running**).
A run config never names an attachment such as the house-standards file:
the template declares it (`TEMPLATE_ATTACHMENTS` in `config.py`), so the
recorded `prompt_version` and what the agent saw cannot disagree.

## Running

### Offline — the bundled inputs, one command per cohort

Reproduce a whole cohort, all 101 tasks:

```bash
python -m coding_agent.run_sweep --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-max.yaml
```

See exactly what that would do, without starting anything:

```bash
python -m coding_agent.run_sweep --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-max.yaml --dry-run
```

`run_configs/offline/` holds one config per cohort, named after the cohort
label with `/` written as `__`; the two Stage 5 prompt-ablation arms carry a
`_v14` / `_v15` suffix. Each pins `benchmark: v2`, `source: local`,
`sink: local`, the template version and the tasks 1-101, and references its
identity by label — nothing about the agent is duplicated.

`run_sweep` walks the same task list the database query returns (this task
source, never a deprecated row, ascending id, inside the config's range),
skips any task the cohort already has a non-deprecated row for, and runs
`run_task` for the rest as its own process — so an attempt behaves exactly as
it does on its own. Re-running the same command resumes: an attempt that
ended as `infra_failure` wrote no row, so it comes back round.

| flag | |
| --- | --- |
| `--task-ids 1-101` | the ids to attempt; also `3`, `1,5,9-12` |
| `--start N --end M` | the same as a range, spelled out |
| `--workers K` | attempts at once; default 1, sequential |
| `--dry-run` | resolve every task — PROMPT.md, staged files, output folder — and start nothing |
| `--list` | print the ids it would attempt and stop |
| `--redo` | attempt every task in range, banked rows included |

Reads come from `data/tasks/task_id=<N>/` and writes go to
`outputs/<agent_model_name>/`: one timestamped folder of artifacts per
attempt plus a `task_attempts.jsonl` whose rows carry exactly the columns the
database row has (`data/README.md`). Nothing under `data/` is ever written.

**One task at a time:**
```bash
python -m coding_agent.run_task --config run_configs/offline/<cohort>.yaml --task-id 11
```

### Where a run reads and writes

`source` and `sink` in a run config each take `local`, `cloud` or `auto`
(the default):

| setting | behaviour |
| --- | --- |
| `local` | the bundled `data/` tree in, `outputs/` out |
| `cloud` | the benchmark database + object store, as before |
| `auto` | `cloud` when a database URL resolves for the benchmark, `local` when none does |

`auto` never goes offline behind a configured database: a URL in
`config/config.yaml` or `DATABASE_URL` always keeps the cloud path, so a run
cannot quietly stop recording because a credential went missing. Startup
prints which side was picked and why. Only what a run actually uses is
preflighted — an offline run contacts nothing.

### Cloud (benchmark task by id)

```bash
python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11
```
Startup prints the database it will write to and where that URL came from
(never the password), the identity's pinned settings, and whether the
database has the `extra_configs` column.

**External (your own task, your own key — no SpreadsheetSmith access needed):**
```bash
python -m coding_agent.run_task --config run_configs/example_external.yaml \
    --task-dir ./my_task --results-dir ./results
```
External task folder: `task.yaml` (`task_name`, `task_source: fmwc|modeloff|wsp|v2`)
plus a `starting_files/` directory. Results (workbook, transcript, telemetry,
verdict, summary, the run config) land in the results folder.

`run_task` runs **one task**; `run_sweep` walks a range of them. The
production waves were driven by their own orchestrator scripts, which are
operational and deliberately untracked (`orchestrate_*.py` is gitignored).
Exit codes: 0 success, 2 agent_failure, 3 timeout, 4 infra_failure,
5 needs_review.

## The sandbox

One container per attempt, from a pinned image:

- only the workspace directory is mounted; nothing else of the host is visible
- env carries exactly one secret: the model API key (DB/S3 creds stay on the host)
- **Harness defaults for long thinking turns** (2026-09-11, `coding_agent/agents.py`):
  every claude run gets `API_FORCE_IDLE_TIMEOUT=0` (turns off the Bun runtime's
  hardcoded 5-minute fetch timeout, which Claude Code only disables itself when
  it talks to api.anthropic.com directly — under the traj relay it does not),
  `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000`,
  `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000`,
  `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1`, `CLAUDE_CODE_MAX_RETRIES=15`;
  every codex run gets `-c model_providers.<provider>.{stream_idle_timeout_ms=1800000,
  stream_max_retries=15, request_max_retries=15}` on the provider it uses (traj
  relay, or openai without a relay). The identity's own `env` / `extra_args`
  override them. Recorded per row as `extra_configs.harness_defaults`. Why: both
  CLIs abort a stream after ~5 min of silence and retry the same turn, and
  Claude Code then falls back to non-streaming requests capped at 64k tokens;
  Fable 5.1 at max effort thinks silently longer than that on hard tasks and
  lost 4 of 10 tasks that way on 2026-09-10 with no workbook written.
- **default-deny egress firewall** — only the vendor's API endpoints resolve;
  the agent cannot browse. If firewall setup fails, the attempt aborts
  (fail-safe) instead of running open. Integrity matters here: the V1 tasks
  come from public competitions whose solutions may exist online.
- non-root user, memory/CPU/pids caps, hard wall-clock kill (default 4h)
- `sandbox.mode: host` exists for rung-0 debugging only and is **unsandboxed** —
  never use it for recorded runs

## Prompts

`PROMPT.md` = system wrapper + task template + workspace file listing.

- **System wrapper** `system_prompt_coding_v1.txt` — minimal proctor
  instructions (workspace rules, `solution.xlsx` requirement, no internet,
  work autonomously). New for this pipeline; versioned.
- **Task templates**, chosen by `template_version` (default follows `benchmark`):

  | Template | `prompt_version` | Source | Attachments seeded into `starting_files/` |
  |---|---|---|---|
  | `v15` (Stage 5 arm) | 115 | `v10` byte-identical (the production prompt minus the house standards) | — |
  | `v14` (Stage 5 arm) | 114 | `v9` byte-identical (rubric added back, no house standards) | — |
  | `v13` (v2 default) | 113 | `v11` byte-identical (rubric-free + `HOUSE_STANDARDS.md` pointer) | `coding_agent/prompts/house_standards_v1.md` → workspace root as `HOUSE_STANDARDS.md` |
  | `v12` (superseded) | 112 | `gui-agents-master/tasks_configs/prompts_v4/` (pre-2026-09-10 cut, rubric-bearing) | `house_standards/House_Standards_v1.md` |
  | `v11` (experiment, frozen) | 111 | `v10` + a pointer to `HOUSE_STANDARDS.md` | `coding_agent/prompts/house_standards_v1.md` → workspace root as `HOUSE_STANDARDS.md` |
  | `v10` (experiment, frozen) | 110 | `v9` with every rubric passage removed | — |
  | `v9` | 109 | `…/prompts_v3/` | — |
  | `v8` | 108 | `…/prompts_v2/` | — |
  | `v7` (v1 default) | 107 | GUI wave pv9 | — |
  | `v6` | 106 | CLI-wave adaptation | — |
  | `v5` | 105 | CLI-wave byte-exact | — |

  - `v14` / `v15` (Stage 5 prompt ablation, 2026-09-23, on the production
    Claude Code / Fable 5.1 max identity; `v13` stays the master prompt):
    existing templates re-cut **byte-identical** under new numbers. `v14` is
    the `v9` text — the current prompt with the 132-check rubric added back
    and **no** house standards; `v15` is the `v10` text — the current prompt
    minus the house standards (rubric-free, no pointer). Neither stages or
    seeds anything, so their rows carry no `house_standards` provenance.
    New numbers because 109/110 already carry the 2026-09-08 experiment's
    graded rows on another identity (same reason as v13 vs 111). Generated
    by `tools/build_v14_v15_templates.py`; `v14` whole-file md5-pinned
    (`RECUT_MD5`, plus v9's rubric-section guard), `v15` pinned in
    `SCRUBBED_MD5` and guarded rubric-free.
  - `v13` (v2 default, 2026-09-10 — the 101-task rerun): the `v11` text
    **byte-identical** — no rubric material at all, plus the pointer to
    `HOUSE_STANDARDS.md` staged into the workspace root — under a new
    number so the rerun cohort never merges with the 111 experiment rows
    (the judge's latest-prompt guard and `scripts/export_good_attempts.py`
    key on `prompt_version`; same reason v12 was cut instead of reusing
    110). Same design as gui/excel 204/205 and cli v15. Recorded as
    `extra_configs.house_standards {version, file, delivered_as, sha256}`
    plus `prompt_extras`. Generated by `tools/build_v13_template.py`;
    md5-pinned (same pin as v11) and guarded rubric-free.
  - `v12` (superseded 2026-09-10; still selectable): `v9` plus the **House Standards directive** — read
    `starting_files/House_Standards_v1.md` before building, follow it where
    the case and the prompt (rubric included) do not say otherwise, note
    departures on the cover, plus QA item 9. The file is the monorepo's
    `house_standards/House_Standards_v1.md` (see its README for the
    append-only rules); the runner seeds it beside the task inputs, so it
    appears in the seeded-file manifest and the `WORKSPACE FILES` listing.
    Rubric byte-identical to v9's (same checksum). Generated by
    `tools/build_v12_template.py` — regenerate, never hand-edit. Numbered
    v12 because 110/111 were already recorded by the experiment below.
    Caveat: the sandbox image (Debian bookworm) ships LibreOffice 7.4,
    which cannot evaluate `XLOOKUP` / `XMATCH` / `LET` — functions the
    house standards recommend — so an attempt that uses them cannot
    self-check those cells in the sandbox and relies on the judge's
    `--run-calculation` (LibreOffice 25.8) for cached values.
  - `v10` / `v11` (rubric-effect experiment, 2026-09-08; frozen — 110/111
    have recorded runs): `v10` is `v9` with every rubric-derived passage
    removed (weights, conventions summary, the 132-check block, the
    rubric-driven QA list); `v11` is `v10` plus a pointer to
    `HOUSE_STANDARDS.md`, which the runner stages into the workspace root
    from `coding_agent/prompts/house_standards_v1.md` (listed under
    `WORKSPACE FILES`, hashed into the manifest, recorded as
    `extra_configs.prompt_extras`). Whole-file md5-pinned and guarded
    rubric-free; generated by `tools/build_v10_v11_templates.py`.
  - `v9`: `v8` plus the **Questions-sheet answer convention** (answers as
    live formulas under the sheet's `Answers` header). Its rubric carries
    the 2026-08 checklist revision, so it is **not** byte-identical to v8's
    (own checksum guard). Generated by `tools/build_v9_template.py` —
    regenerate, never hand-edit.
  - `v8`: mirror of the **v2 GUI prompt** with the 132-check
    rubric embedded byte-exact (checksum-guarded). Generated from
    `gui-agents-master/tasks_configs/prompts_v2/step2_build.txt` by
    `tools/build_v8_template.py` — regenerate, never hand-edit.
  - `v7` (v1 default): mirror of the **GUI wave's pv9 prompt** — the byte-exact
    pv9 rubric preamble (all 17 grading criteria with good/bad standards,
    checksum-guarded) + the pv9 three-step closing (`Summary` sheet → model →
    `Answers` sheet), with only harness-necessitated edits: workspace /
    solution.xlsx wording, and pv9's "no code interpreter" ban translated to
    its intent — code may build the workbook, but every calculated value must
    be a live Excel formula. Task-invariant (one template for fmwc/modeloff/
    wsp), exactly like the GUI wave. One addendum beyond pv9: a short Excel
    mechanical-validity section (sheet-name rules, no formulas-as-text, no
    circular refs, no undefined names) — rules the Excel UI enforced for free
    for GUI agents but nothing enforces when writing files with code.
  - `v6`: the pv1105 CLI-wave template structure adapted for coding agents
    (rubric-blind, like the CLI task templates alone).
  - `v5`: **byte-exact** copies of the pv1105 CLI-wave templates, frozen and
    checksum-guarded. They reference harness tools that don't exist here —
    kept only for strict prompt-comparability experiments.
- `prompt_version` recorded per attempt = system version × 100 + template
  version (v1 wrapper + v13 → **113**; + v15 → 115; + v14 → 114; + v12 → 112; + v11 → 111; + v10 → 110; + v9 → 109; + v8 → 108; + v7 → 107;
  + v6 → 106; + v5 → 105), continuing the CLI pipeline's numbering scheme
  (its wave was 1105; GUI was 9).

## Trajectory recording

Every attempt captures the agent's full decision trajectory at the API layer:
a relay inside the container (`docker/traj_relay.py`) sits between the CLI and
the vendor API and appends one record per model call to `trajectory.jsonl.gz`
(uploaded with the attempt):

- `request` — the exact model input: the CLI's internal system prompt, tool
  schemas, and the complete message/input array as sent (grows every step)
- `response` — the exact model output: text / tool calls / reasoning items,
  stored raw (SSE streams verbatim); auth headers scrubbed
- one record per step -> `(request, response)` pairs are training-ready

Claude Code is routed via `ANTHROPIC_BASE_URL`; Codex ignores base-URL env, so
it is routed via `-c model_providers.traj.*` flags (API-key billing preserved
through `env_key`). Disable per run with `record_trajectory: false`. Docker
mode only; the egress firewall still sees only the vendor API.

The relay runs from the repo: `docker/traj_relay.py` is bind-mounted read-only
over the copy baked into the image, so a relay fix never needs a new image tag
(the tag is the CLI-version pin). Rows record the mounted file's hash as
`extra_configs.relay`; rows without that key ran the image's own relay. Every
failed call is recorded with `error {phase, type, repr, bytes_relayed,
elapsed_ms}` and appended to `trajectory.jsonl.errors.log`:
`upstream_open` (the vendor connection failed before any reply — the CLI gets
a complete, connection-closing 502 and retries it; the old bare 502 on a
keep-alive socket hung Claude Code for 68 min on 2026-09-20), `upstream_read`
(the vendor cut the reply mid-stream — fatal to Claude Code while the
non-streaming fallback is disabled) or `client_write` (the CLI went away).

## Validation and failure taxonomy

Success requires **all** of: `solution.xlsx` exists · opens as a valid
workbook · sha256 differs from every seeded input (manifest check — an
untouched/renamed input can never be banked) · ran longer than the junk guard
(default 3 min).

| Verdict | Meaning | DB row? |
|---|---|---|
| `success` | valid new workbook | yes (`agent_failed=false`) |
| `timeout` | wall-clock cap hit; partial workbook kept | yes (`agent_failed=true`) |
| `agent_failure` | ran to completion, no valid new workbook | yes (`agent_failed=true`) |
| `infra_failure` | seeding/container/auth/quota problem — agent never got a fair attempt | **no** (retry; no trial burned) |
| `needs_review` | junk-fast success or ambiguous output | **no** (held locally for a human) |

## Recording (internal mode)

Both sinks record the same thing; only the destination differs.

**Offline (`sink: local`)** — a folder per attempt at
`outputs/<agent_model_name>/task_id=<N>/<YYYYmmdd_HHMMSS>/` holding the same
artifact set the cloud sink uploads (the workbook, `PROMPT.md` as sent, the
prompt files, transcript, telemetry, verdict, trajectory, run config), and one
line appended to `outputs/<agent_model_name>/task_attempts.jsonl` carrying
every column of the `task_attempts` row, with repo-relative paths and
ISO-8601 timestamps. A cohort label containing a slash nests one directory,
as the object-store prefix does. The row's `id` is the millisecond epoch of
the write, so lanes on one machine never collide and a local id is never
mistaken for a database one. Appends take an exclusive lock.

**Cloud (`sink: cloud`)** — same conventions as the CLI wave: a
`task_attempts` row (`agent_model_name`, prompt version, timing,
`agent_failed`, cost, `agent_model_type = "coding_cli"`) plus artifacts under
`s3://<bucket>/<BizbenchV1|SpreadsheetSmith>/attempts/<agent_model_name>/task_source=<src>/task_id=<id>/`.

Either way `solution.xlsx` is first in `attempt_files` (the judge grades the
first xlsx), and the same verdicts are recordable: `infra_failure` and
`needs_review` write no row at all. Artifacts per attempt: the full agent transcript (`transcript.jsonl`),
`telemetry.json` (per-turn token usage), `verdict.json`, the trajectory
capture, and `run_config.yaml` (the config the attempt ran with). Cost comes
from the CLI's own usage report (Claude Code reports `total_cost_usd`; Codex
reports tokens only, so cost is null there) — there is deliberately no
hand-maintained price table.

The row's `extra_configs` additionally records the
identity's pinned settings plus the sandbox image, so a row can be audited
without trusting the registry file; for templates that ship the house
standards it also carries `house_standards: {version, file, sha256}`, the
hash taken at run time from the file actually seeded. The attachment is
recorded with the prompt files, so `prompt_files` lists it too. The v1
table has no such column; on the cloud path it is probed at startup and
skipped.

Locally, every attempt dir (`workspaces/task{id}_{ts}_{pid}/`) also holds
`run_config.yaml` and `prompts/` (the exact system prompt + template + any
template attachment), written before the agent starts — the record
survives an upload failure.

On the cloud path, finished attempts upload one lane at a time (`flock` on
`workspaces/.s3_upload.lock`), at 250 KB/s, in a single stream
(`recorder.S3_UPLOAD_MAX_BYTES_PER_SEC`). With 12 lanes on one uplink, every
one of 36 dropped API connections on 2026-09-20 fell inside an upload window
(a 209 MB workbook caused a 6-minute storm), and a throttle spread over
boto3's default ten streams let S3 time out an idle part and lose a finished
attempt's row. A recording failure is an `infra_failure`: no row, the attempt
folder is kept.

Registered cohorts live in `coding_agent/agent_identities.yaml`; the ones
that make up the v2 coding leaderboard each have a ready-to-run config in
`run_configs/offline/`.

## Judging

Unchanged: the existing `judge/` pipeline grades these attempts exactly like
any others (V1 rubric for v1 rows; the agentic judge + rubric_9 for v2 — see
`judge/project_configs.yaml`).

## Rollout ladder (spend nothing until each rung passes)

1. **Rung 0** — one cheap task, `sandbox.mode: host`: verify the CLI bills the
   **API key** (not a logged-in subscription) and that model/effort settings
   are actually applied (check the transcript's model/usage fields). Adjust
   the identity's `extra_args`/`env` as needed — no code changes.
   Note that host mode is **not** credential-isolated: it runs the CLI with
   the whole host environment, so any database URL or cloud credential in the
   shell is inherited by the agent process. Docker mode passes exactly the
   model API key, the egress allowlist and the relay settings, and nothing
   else — no database URL, bucket name or cloud credential ever enters the
   container.
2. **Ladder** — two known tasks under a throwaway identity, full Docker path:
   verify the recorded row, the artifacts and judgeability. Discard rows after.
3. **Pilot** — small graded batch; calibrate cost/task and the wall-clock cap.
4. **Wave** — the full task set × agents via `run_sweep`, with monitoring.

## Tests

```bash
python3 tests/test_smoke.py             # config, prompts, validation verdicts, telemetry
python3 tests/test_benchmark_config.py  # v1/v2 switch + v8/v9/v12/v13 template guards + attachment/extra seeding
python3 tests/test_agent_identity.py    # identity registry rules
python3 tests/test_repo_config.py       # config/config.yaml resolution ladder
python3 tests/test_offline_parity.py    # bundled source/sink == the cloud path
python3 -m pytest tests/test_relay.py   # trajectory relay
```
All offline. No Docker, database, object store or keys required.
