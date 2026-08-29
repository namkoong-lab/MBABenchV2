# coding-agents-master

Run MBABench financial-modeling tasks through **vendor coding agents** — Claude
Code (Anthropic) and Codex (OpenAI) — each attempt in its own locked-down
container, producing one Excel workbook per task for the standard MBABench judge.

> **Scope note:** this pipeline runs against either benchmark — set
> `benchmark: v1|v2` in the run config (required for internal runs; there
> is no default). v1 targets the V1
> 206-task set (`BizbenchV1` on Neon, `s3://mbabench/BizbenchV1/…`, the
> pv9-mirror v7 template, the 3-category rubric). v2 targets the
> MBABenchV2 task set (`MBABenchV2`, `s3://mbabench/MBABenchV2/…`, the
> v9 template mirroring the 132-check rubric plus the Questions-sheet
> answer convention — graded by the agentic
> judge). The benchmark key picks the database URL, the S3 root and the
> template together. It is the third agent surface alongside the GUI
> pipeline (vendor chat products) and CLI pipeline (raw model APIs in our
> own harness).

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
  config.py              YAML run config + secrets resolution (fail-fast validation)
  repo_config.py         Reads <MBABenchV2>/config/config.yaml (DB URLs, AWS, keys)
  agent_identity.py      agent_model_name -> pinned cli/model/effort (registry below)
  agent_identities.yaml  THE registry of cohort labels and what each one runs
  task_source.py         internal (Neon+S3, read-only on tasks) / external (local folder)
  workspace.py           Per-attempt workspace + seeded-file sha256 manifest
  prompt_builder.py      PROMPT.md assembly + prompt_version accounting
  agents.py              claude / codex headless command construction
  sandbox.py             Docker (or host, dev-only) execution, wall-clock kill
  telemetry.py           Per-turn token usage + cost from the CLI's own output
  validate.py            Success criteria + failure taxonomy (see below)
  recorder.py            DB row + S3 upload (internal) / results folder (external)
  prompts/               System wrapper + task templates (see Prompts)
docker/                  Sandbox image: pinned CLIs + default-deny egress firewall
run_configs/             Example YAML configs (prod configs are untracked)
tools/                   build_v8_template.py / build_v9_template.py (v2 template generators), validate_trajectory.py
tests/                   Offline tests (no Docker/DB/keys needed)
```

## Setup

1. **Docker** (Docker Desktop on macOS) — the sandbox runtime.
2. Python deps: `pip install -e .` — or, from the MBABenchV2 root, the
   workspace install (`setup.sh`), which also makes the shared `config`
   module importable.
3. Build the sandbox image (pin CLI versions for a wave):

```bash
cd docker && docker build -t mbabench-coding-agent:v2 \
  --build-arg CLAUDE_CODE_VERSION=2.1.251 --build-arg CODEX_VERSION=0.150.1 .
```

   The tag is recorded per attempt (`extra_configs.sandbox_image`) as the
   CLI-version pin — use a new tag whenever a rebuild changes the contents.

4. Secrets — never in run configs, never written into workspaces:
   - **DB URLs + AWS creds**: `<MBABenchV2>/config/config.yaml`
     (`database.v1_url` / `database.v2_url`, `aws.access_key_id` /
     `aws.secret_access_key`, `aws.s3_bucket`). The run config's `benchmark`
     picks the URL; nothing to swap between v1 and v2 runs. On a standalone
     checkout (no `config` module) `DATABASE_URL` and boto3's default chain
     are the fallback, and the URL is checked against the benchmark.
   - **Agent API key**: `ANTHROPIC_API_KEY` (claude) / `OPENAI_API_KEY`
     (codex) from the environment or a `.env` next to `coding_agent/`,
     falling back to `keys.anthropic_api_key` / `keys.openai_api_key` in
     `config/config.yaml`.

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
`template_version` (v7 for v1, v9 for v2), `sandbox` (mode/image/cpus/memory),
`limits` (wall clock, junk guard), `record_trajectory`, `workspaces_dir`.

## Running

**Internal (benchmark task by id):**
```bash
python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11
```
Startup prints the database it will write to and where that URL came from
(never the password), the identity's pinned settings, and whether the DB has
the `extra_configs` column.

**External (your own task, your own key — no MBABench access needed):**
```bash
python -m coding_agent.run_task --config run_configs/example_external.yaml \
    --task-dir ./my_task --results-dir ./results
```
External task folder: `task.yaml` (`task_name`, `task_source: fmwc|modeloff|wsp|jp`)
plus a `starting_files/` directory. Results (workbook, transcript, telemetry,
verdict, summary, the run config) land in the results folder.

One invocation runs **one task**. Batch sweeps are driven by a separate
orchestrator script that calls this in a loop; orchestrators are operational
scripts and deliberately untracked (`orchestrate_*.py` is gitignored). Exit
codes: 0 success, 2 agent_failure, 3 timeout, 4 infra_failure, 5 needs_review.

## The sandbox

One container per attempt, from a pinned image:

- only the workspace directory is mounted; nothing else of the host is visible
- env carries exactly one secret: the model API key (DB/S3 creds stay on the host)
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
  - `v9` (v2 default): `v8` plus the **Questions-sheet answer convention**
    (answers as live formulas under the sheet's `Answers` header); its rubric
    section is byte-identical to v8's (same checksum guard). Generated by
    `tools/build_v9_template.py` — regenerate, never hand-edit.
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
  version (v1 wrapper + v9 → **109**; + v8 → 108; + v7 → 107; + v6 → 106; + v5 → 105),
  continuing the CLI pipeline's numbering scheme (its wave was 1105; GUI was 9).

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

Same conventions as the CLI wave — `task_attempts` row (`agent_model_name`,
prompt version, timing, `agent_failed`, cost, `agent_model_type =
"coding_cli"`) + S3 artifacts under
`s3://mbabench/<BizbenchV1|MBABenchV2>/attempts/<agent_model_name>/task_source=<src>/task_id=<id>/`,
with `solution.xlsx` first in `attempt_files` (the judge grades the first
xlsx). Artifacts per attempt: the full agent transcript (`transcript.jsonl`),
`telemetry.json` (per-turn token usage), `verdict.json`, the trajectory
capture, and `run_config.yaml` (the config the attempt ran with). Cost comes
from the CLI's own usage report (Claude Code reports `total_cost_usd`; Codex
reports tokens only, so cost is null there) — there is deliberately no
hand-maintained price table.

On MBABenchV2 the row's `extra_configs` (JSONB) additionally records the
identity's pinned settings plus the sandbox image, so a row can be audited
without trusting the registry file. BizbenchV1 has no such column; it is
probed at startup and skipped.

Locally, every attempt dir (`workspaces/task{id}_{ts}_{pid}/`) also holds
`run_config.yaml` and `prompts/` (the exact system prompt + template), written
before the agent starts — the record survives an upload failure.

Registered cohorts:
`claudecode_anthropic/claude-fable-5-max` · `codex_openai/gpt-5.6-sol-xhigh`
(v1 wave) · `claudecode_anthropic/claude-haiku-4-5` (pipeline shakeout only).

## Judging

Unchanged: the existing `judge/` pipeline grades these attempts exactly like
any others (V1 rubric for v1 rows; the agentic judge + rubric_9 for v2 — see
`judge/project_configs.yaml`).

## Rollout ladder (spend nothing until each rung passes)

1. **Rung 0** — one cheap task, `sandbox.mode: host`: verify the CLI bills the
   **API key** (not a logged-in subscription) and that model/effort settings
   are actually applied (check the transcript's model/usage fields). Adjust
   the identity's `extra_args`/`env` as needed — no code changes.
2. **Ladder** — two known tasks under a throwaway identity, full Docker path:
   verify DB row, S3 artifacts, judgeability. Discard rows after.
3. **Pilot** — small graded batch; calibrate cost/task and the wall-clock cap.
4. **Wave** — the full task set × agents via the orchestrator, with monitoring.

## Tests

```bash
python3 tests/test_smoke.py             # config, prompts, validation verdicts, telemetry
python3 tests/test_benchmark_config.py  # v1/v2 switch + v8/v9 template guards
python3 tests/test_agent_identity.py    # identity registry rules
python3 tests/test_repo_config.py       # config/config.yaml resolution ladder
python3 -m pytest tests/test_relay.py   # trajectory relay
```
Offline. No Docker, DB, S3, or keys required.
