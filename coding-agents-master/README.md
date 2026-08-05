# coding-agents-master

Run MBABench financial-modeling tasks through **vendor coding agents** — Claude
Code (Anthropic) and Codex (OpenAI) — each attempt in its own locked-down
container, producing one Excel workbook per task for the standard MBABench judge.

> **Scope note:** this folder lives in the MBABenchV2 repo but targets the
> **MBABench V1 benchmark**: the V1 206-task set, the V1 task prompts, the V1
> database (`BizbenchV1` on Neon) and S3 layout, and the V1 judge/rubric.
> It is the third agent surface alongside the V1 GUI pipeline (vendor chat
> products) and V1 CLI pipeline (raw model APIs in our own harness).

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
  config.py              YAML run config + env secrets (fail-fast validation)
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
tests/test_smoke.py      Offline smoke tests (no Docker/DB/keys needed)
```

## Setup

1. **Docker** (Docker Desktop on macOS) — the sandbox runtime.
2. Python deps: `pip install -r requirements.txt` (or `pip install -e .`).
3. Build the sandbox image (pin CLI versions for a wave):

```bash
cd docker && docker build -t mbabench-coding-agent:v1 \
  --build-arg CLAUDE_CODE_VERSION=latest --build-arg CODEX_VERSION=latest .
```

4. Secrets — environment only (or a `.env` next to `coding_agent/`; never
   committed, never written into workspaces):
   - `ANTHROPIC_API_KEY` (claude) / `OPENAI_API_KEY` (codex)
   - internal mode additionally: `DATABASE_URL` + AWS credentials (env or `~/.aws`)

## Running

**Internal (benchmark task by id):**
```bash
python -m coding_agent.run_task --config run_configs/example_fable.yaml --task-id 83
```

**External (your own task, your own key — no MBABench access needed):**
```bash
python -m coding_agent.run_task --config run_configs/example_external.yaml \
    --task-dir ./my_task --results-dir ./results
```
External task folder: `task.yaml` (`task_name`, `task_source: fmwc|modeloff|wsp`)
plus a `starting_files/` directory. Results (workbook, transcript, telemetry,
verdict, summary) land in the results folder.

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
- **Task templates** — two variants, chosen by `template_version`:
  - `v6` (default): the pv1105 CLI-wave template structure adapted for coding
    agents (same output requirements, worksheet structure, formula rules;
    openpyxl-harness tool references removed).
  - `v5`: **byte-exact** copies of the pv1105 CLI-wave templates, frozen and
    checksum-guarded. They reference harness tools that don't exist here —
    kept only for strict prompt-comparability experiments.
- `prompt_version` recorded per attempt = system version × 100 + template
  version (v1 wrapper + v6 template → **106**; + v5 → 105), continuing the CLI
  pipeline's numbering scheme (its wave was 1105; GUI was 9).

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

Same conventions as the CLI wave — `task_attempts` row (identity, prompt
version, timing, `agent_failed`, cost) + S3 artifacts under
`s3://mbabench/BizbenchV1/attempts/<identity>/task_source=<src>/task_id=<id>/`,
with `solution.xlsx` first in `attempt_files` (the judge grades the first
xlsx). New artifacts per attempt: the full agent transcript
(`transcript.jsonl`), `telemetry.json` (per-turn token usage), and
`verdict.json`. Cost comes from the CLI's own usage report (Claude Code
reports `total_cost_usd`; Codex reports tokens only, so cost is null there) —
there is deliberately no hand-maintained price table.

First-wave identities:
`claudecode_anthropic/claude-fable-5-max` · `codex_openai/gpt-5.6-sol-xhigh`
(`agent_model_type = "coding_cli"`).

## Judging

Unchanged: the existing `judge/` pipeline, V1 rubric and weights, grades these
attempts exactly like any others.

## Rollout ladder (spend nothing until each rung passes)

1. **Rung 0** — one cheap task, `sandbox.mode: host`: verify the CLI bills the
   **API key** (not a logged-in subscription) and that model/effort settings
   are actually applied (check the transcript's model/usage fields). Adjust
   `agent.extra_args`/`agent.env` as needed — no code changes.
2. **Ladder** — two known tasks under throwaway identities, full Docker path:
   verify DB row, S3 artifacts, judgeability. Discard rows after.
3. **Pilot** — small graded batch; calibrate cost/task and the wall-clock cap.
4. **Wave** — full 206 × 2 agents via the orchestrator, with monitoring.

## Tests

```bash
python3 tests/test_smoke.py
```
Offline: config loading, prompt assembly + version math, all validation
verdicts, telemetry parsers. No Docker, DB, S3, or keys required.
