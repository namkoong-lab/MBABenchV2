# coding-agents-master

Run MBABench financial-modeling tasks through **vendor coding agents** — Claude
Code (Anthropic) and Codex (OpenAI) — each attempt in its own locked-down
container, producing one Excel workbook per task for the MBABench judge.

Two modes:

- **external** — your own task folder, your own API key, Docker. No MBABench
  database or S3. Results land in a local folder. Start here.
- **internal** — benchmark task by id from the MBABench database and S3;
  records a `task_attempts` row and uploads artifacts. Needs
  `<MBABenchV2>/config/config.yaml`.

`benchmark: v1|v2` selects the experiment: v2 = the MBABenchV2 task set, the
v13 template (rubric-free, Questions-sheet answer convention, House Standards
pointer), graded by the agentic judge; v1 = the BizbenchV1 206-task set and
the v7 template. In internal mode it also picks the database URL and the S3
root. Internal runs require the key; external runs default to v1, so set
`benchmark: v2` explicitly for a v2 experiment.

There is **no Gemini CLI agent** in this lane: `coding_agent/agents.py`
builds commands for `claude` and `codex` only. Other models run through Codex
via a gateway (see Identities).

## How it differs from the CLI pipeline

The CLI pipeline *is* the agent: it serializes the workbook to text, asks the
model for one edit at a time, and applies edits with its own code. Here the
vendor ships the whole agent — Claude Code / Codex read files themselves,
write and run their own code, and iterate. This pipeline is only the proctor:
seed a workspace, start the agent in a sandbox, validate what came back,
record it.

```
task store -> workspace prep -> sandboxed agent attempt -> validation -> record -> judge
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
tools/                   Template generators (build_v*_template.py), validate_trajectory.py
tests/                   Offline tests (no Docker/DB/keys needed)
```

## Setup

1. **Docker** (Docker Desktop on macOS) — the sandbox runtime.
2. Python deps: from the MBABenchV2 root, `./setup.sh` (the uv workspace
   install, which also makes the shared `config` module importable).
3. Build the sandbox image (pin CLI versions):

```bash
cd docker && docker build -t mbabench-coding-agent:v2 \
  --build-arg CLAUDE_CODE_VERSION=2.1.251 --build-arg CODEX_VERSION=0.150.1 .
```

   The tag is recorded per attempt (`extra_configs.sandbox_image`) as the
   CLI-version pin — use a new tag whenever a rebuild changes the contents.
   All tags build from the same `Dockerfile`:

   | tag | `CLAUDE_CODE_VERSION` | `CODEX_VERSION` | use |
   |---|---|---|---|
   | `mbabench-coding-agent:v2` | 2.1.251 | 0.150.1 | run-config default |
   | `mbabench-coding-agent:v3` | 2.1.251 | 0.155.1 | Codex with native GPT-6 Astra metadata; required by the Forge identities (`docker/codex_model_catalog.json` is tied to this Codex version) |
   | `mbabench-coding-agent:v4` | 2.1.280 | 0.150.1 | Claude Code with a native Claude Opus 5.5 entry (2.1.251 silently maps any `claude-opus-5*` id to Opus 5: Opus 5 prompt bundle, prices and thinking rules) |

   `INCLUDE_LIBREOFFICE=false` skips LibreOffice Calc in the image (the agents
   use it to recalculate their own workbooks). The image's LibreOffice (7.4)
   cannot evaluate `XLOOKUP` / `XMATCH` / `LET`; the judge's
   `--run-calculation` recomputes those cells.

4. Secrets — never in run configs, never written into workspaces:
   - **Agent API key** (both modes): `ANTHROPIC_API_KEY` (claude) /
     `OPENAI_API_KEY` (codex) from the environment or a `.env` next to
     `coding_agent/`, falling back to `keys.anthropic_api_key` /
     `keys.openai_api_key` in `config/config.yaml`. Forge identities use
     `keys.forge_api_key` / its env var only and never fall back to the vendor
     key.
   - **DB URLs + AWS creds** (internal mode only):
     `<MBABenchV2>/config/config.yaml` (`database.v1_url` / `database.v2_url`,
     `aws.access_key_id` / `aws.secret_access_key`, `aws.s3_bucket`). The run
     config's `benchmark` picks the URL. On a standalone checkout (no `config`
     module) `DATABASE_URL` and boto3's default chain are the fallback, and
     the URL is checked against the benchmark.

## Run configs

A run config names its cohort with **one** key, `agent_model_name`, and says
nothing else about the agent:

```yaml
mode: external
benchmark: v2
agent_model_name: claudecode_anthropic/claude-fable-5-max
```

The entry for that label in `coding_agent/agent_identities.yaml` pins `cli`,
`model`, `effort`, `extra_args` and `env`. The runner refuses to start if the
config carries an `agent:` block (or the old `identity:` / `internal:` keys),
if the label is unregistered (it prints the stanza to add), or if two
registry entries share a label or a `(cli, model, effort)` combination. To
run different settings, add a new entry with a new label — never edit an
existing one: the label is what the judge and the result tables group rows
by.

Every other key is a run setting with a default:

| key | default | meaning |
|---|---|---|
| `template_version` | v7 for v1, v13 for v2 | task template (see Prompts) |
| `sandbox.mode` | `docker` | `host` runs the CLI directly on your machine: **unsandboxed**, no trajectory capture, refused by Forge identities. Debugging only. |
| `sandbox.image` | `mbabench-coding-agent:v2` | must match the identity's image (see Identities) |
| `sandbox.cpus` / `sandbox.memory` | 4 / `8g` | container caps |
| `limits.wall_clock_seconds` | 14400 | hard kill (4h) |
| `limits.junk_seconds` | 180 | a faster "success" goes to `needs_review` |
| `limits.exclude_provider_waits` | false | when true, time the relay spends waiting out provider 429s does not count against the wall clock |
| `record_trajectory` | true | per-step API capture (Docker mode only) |
| `workspaces_dir` | `coding-agents-master/workspaces` | local attempt dirs |

A run config never names an attachment such as the house-standards file: the
template declares it, so the recorded `prompt_version` and what the agent saw
cannot disagree.

## Running

One invocation runs **one task**. Exit codes: 0 success, 2 agent_failure,
3 timeout, 4 infra_failure, 5 needs_review.

### External mode (your task, your key)

Task folder:

```
my_task/
  task.yaml            task_name: <name>; task_source: fmwc|modeloff|wsp|jp
  starting_files/      the input .xlsx / .pdf files (at least one)
```

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY for a codex identity
python -m coding_agent.run_task --config run_configs/example_external.yaml \
    --task-dir ./my_task --results-dir ./results
```

`run_configs/example_external.yaml` is the one template: set `benchmark`,
`agent_model_name` and, if the identity needs it, `sandbox.image`.

### Internal mode (benchmark task by id)

```bash
python -m coding_agent.run_task --config run_configs/example_v2_claude.yaml --task-id 11
```

Startup prints the database it will write to and where that URL came from
(never the password), the identity's pinned settings, and whether the DB has
the `extra_configs` column. Batch sweeps are driven by a separate orchestrator
that calls this in a loop; orchestrators are operational scripts and
untracked (`orchestrate_*.py` is gitignored).

## Outputs

**Every attempt** writes `workspaces/<label>_<ts>_<pid>/` (label = `task<id>`
internal, `<task_name>` external) holding `workspace/` (the agent's working
directory, `PROMPT.md`, `starting_files/`, `solution.xlsx`), `run_config.yaml`
and `prompts/` (the exact system prompt, template and any attachment), written
before the agent starts, plus `transcript.jsonl` (the full agent transcript),
`telemetry.json` (per-turn token usage), `verdict.json`, `manifest.json`
(seeded-file sha256s), `trajectory.jsonl.gz` and `summary.json`.

**External mode** copies the above (minus the workspace) and `solution.xlsx`
to `<results-dir>/<task_name>_<ts>_<pid>/`.

**Internal mode** writes a `task_attempts` row (`agent_model_name`,
`prompt_version`, timing, `agent_failed`, cost, `agent_model_type =
"coding_cli"`) and uploads the artifacts to
`s3://<bucket>/<BizbenchV1|MBABenchV2>/attempts/<agent_model_name>/task_source=<src>/task_id=<id>/`,
with `solution.xlsx` first in `attempt_files` (the judge grades the first
xlsx). On MBABenchV2 the row's `extra_configs` (JSONB) also records the
identity's pinned settings, the sandbox image, the harness defaults, the
relay hash and — for templates that ship the house standards —
`house_standards: {version, file, delivered_as, sha256}` plus
`prompt_extras`. BizbenchV1 has no such column; it is probed at startup and
skipped. Uploads take one lane at a time (`flock` on
`workspaces/.s3_upload.lock`) in a single stream at
`recorder.S3_UPLOAD_MAX_BYTES_PER_SEC`. A recording failure is an
`infra_failure`: no row, the attempt folder is kept.

Cost comes from the CLI's own usage report (Claude Code reports
`total_cost_usd`; Codex reports tokens only, so cost is null there) — there is
no hand-maintained price table.

## Validation and failure taxonomy

Success requires **all** of: `solution.xlsx` exists · opens as a valid
workbook · sha256 differs from every seeded input (manifest check — an
untouched/renamed input can never be banked) · ran longer than the junk guard.

| Verdict | Meaning | DB row (internal)? |
|---|---|---|
| `success` | valid new workbook | yes (`agent_failed=false`) |
| `timeout` | wall-clock cap hit; partial workbook kept | yes (`agent_failed=true`) |
| `agent_failure` | ran to completion, no valid new workbook | yes (`agent_failed=true`) |
| `infra_failure` | seeding/container/auth/quota problem — agent never got a fair attempt | **no** (retry; no trial burned) |
| `needs_review` | junk-fast success or ambiguous output | **no** (held locally for a human) |

## The sandbox

One container per attempt, from a pinned image:

- only the workspace directory is mounted; nothing else of the host is visible
- env carries exactly one secret: the model API key (DB/S3 creds stay on the host)
- **default-deny egress firewall** — only the vendor's API endpoint resolves;
  the agent cannot browse. If firewall setup fails, the attempt aborts
  instead of running open.
- non-root user, memory/CPU/pids caps, hard wall-clock kill (default 4h)
- **harness defaults for long thinking turns** (`coding_agent/agents.py`):
  every claude run gets `API_FORCE_IDLE_TIMEOUT=0`,
  `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000`,
  `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000`,
  `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1`, `CLAUDE_CODE_MAX_RETRIES=15`;
  every codex run gets `-c model_providers.<provider>.{stream_idle_timeout_ms=1800000,
  stream_max_retries=15, request_max_retries=15}`. The identity's own `env` /
  `extra_args` override them. Recorded per row as
  `extra_configs.harness_defaults`.
- `sandbox.mode: host` runs the CLI on the host: no container, no firewall,
  no caps, no trajectory capture; Forge identities refuse it. Never use it
  for recorded runs.

## Trajectory recording

Every Docker attempt captures the agent's full decision trajectory at the API
layer: a relay inside the container (`docker/traj_relay.py`) sits between the
CLI and the vendor API and appends one record per model call to
`trajectory.jsonl.gz`:

- `request` — the exact model input: the CLI's internal system prompt, tool
  schemas, and the complete message/input array as sent (grows every step)
- `response` — the exact model output: text / tool calls / reasoning items,
  stored raw (SSE streams verbatim); auth headers scrubbed

Claude Code is routed via `ANTHROPIC_BASE_URL`; Codex ignores base-URL env, so
it is routed via `-c model_providers.traj.*` flags (API-key billing preserved
through `env_key`). Disable per run with `record_trajectory: false`. The
egress firewall still sees only the vendor API.

The relay is bind-mounted read-only from the repo over the copy baked into
the image, so a relay fix never needs a new image tag. Rows record the mounted
file's hash as `extra_configs.relay`. Every failed call is recorded with
`error {phase, type, repr, bytes_relayed, elapsed_ms}` and appended to
`trajectory.jsonl.errors.log`: `upstream_open` (the vendor connection failed
before any reply — the CLI gets a connection-closing 502 and retries),
`upstream_read` (the vendor cut the reply mid-stream) or `client_write` (the
CLI went away). An identity whose `env.TRAJ_UPSTREAM` names a gateway sends
the relay there instead of the vendor API.

## Prompts

`PROMPT.md` = system wrapper + task template + workspace file listing.

- **System wrapper** `system_prompt_coding_v1.txt` — minimal proctor
  instructions (workspace rules, `solution.xlsx` requirement, no internet,
  work autonomously).
- **Task templates**, chosen by `template_version` (default follows
  `benchmark`). Templates are generated by `tools/build_*_template.py` and
  md5-pinned — regenerate, never hand-edit.

  | Template | `prompt_version` | Content | Attachment staged into the workspace |
  |---|---|---|---|
  | `v13` (v2 default) | 113 | rubric-free + Questions-sheet answer convention + pointer to `HOUSE_STANDARDS.md` (= `v11` text) | `coding_agent/prompts/house_standards_v1.md` → `HOUSE_STANDARDS.md` |
  | `v15` | 115 | `v10` text (no rubric, no house standards) | — |
  | `v14` | 114 | `v9` text (rubric, no house standards) | — |
  | `v12` | 112 | `v9` + House Standards directive (rubric-bearing) | `house_standards/House_Standards_v1.md` → `starting_files/` |
  | `v11` | 111 | `v10` + pointer to `HOUSE_STANDARDS.md` | `coding_agent/prompts/house_standards_v1.md` → `HOUSE_STANDARDS.md` |
  | `v10` | 110 | `v9` with every rubric passage removed | — |
  | `v9` | 109 | `v8` + Questions-sheet answer convention | — |
  | `v8` | 108 | v2 GUI prompt with the 132-check rubric embedded | — |
  | `v7` (v1 default) | 107 | GUI pv9 prompt mirror (17 grading criteria) + Excel mechanical-validity section | — |
  | `v6` | 106 | CLI-wave template adapted for coding agents (rubric-blind) | — |
  | `v5` | 105 | byte-exact CLI-wave templates (reference harness tools that do not exist here) | — |

  Each `prompt_version` number is frozen to its text: a new cut of the same
  text under a new number keeps its rows from merging with an earlier cohort
  (the judge and `scripts/export_good_attempts.py` key on `prompt_version`).
  Staged attachments are listed under `WORKSPACE FILES` in `PROMPT.md`,
  hashed into the manifest and recorded in `extra_configs`.
- `prompt_version` = system version × 100 + template version (v1 wrapper +
  v13 → 113).

## Identities

`coding_agent/agent_identities.yaml` is the registry. Each family needs a
specific sandbox image; set `sandbox.image` in the run config to match.

| family | cli | runs through | image |
|---|---|---|---|
| `claudecode_anthropic/*` except Opus 5.5 | claude | Anthropic API | `:v2` (default) |
| `claudecode_anthropic/claude-opus-5-5-*` | claude | Anthropic API | `:v4` (Claude Code 2.1.280; on `:v2` the model is silently run as Opus 5) |
| `codex_openai/*` | codex | OpenAI API | `:v2` (`:v3` for native GPT-6 Astra metadata) |
| `codex_tensorblock/*` | codex | TensorBlock Forge gateway (`env.TRAJ_UPSTREAM`), billed to the Forge key | `:v3` (Codex 0.155.1; the mounted model catalog is tied to it). Docker + `record_trajectory` required. |

Registered labels:

| label | cli | model | effort |
|---|---|---|---|
| `claudecode_anthropic/claude-fable-5-1-max`, `-high`, `-low` | claude | claude-fable-5-1 | max / high / low |
| `claudecode_anthropic/claude-opus-5-max` | claude | claude-opus-5 | max |
| `claudecode_anthropic/claude-opus-5-5-max`, `-high`, `-low` | claude | claude-opus-5-5 | max / high / low |
| `claudecode_anthropic/claude-fable-5-max` | claude | claude-fable-5 | max |
| `claudecode_anthropic/claude-haiku-4-5` | claude | claude-haiku-4-5-20251001 | - |
| `codex_openai/gpt-6-astra-xhigh` | codex | gpt-6-astra | xhigh |
| `codex_openai/gpt-5.6-sol-xhigh` | codex | gpt-5.6-sol | xhigh |
| `codex_tensorblock/grok-4.6-xhigh`, `kimi-k3-max`, `gemini-3.8-flash-high`, `qwen3.8-max-xhigh`, `glm-5.3-max` | codex via Forge | the named model | as labelled |
| `codex_tensorblock/claude-fable-5-1-max`, `claude-opus-5-max` | codex via Forge | Claude models through Codex | max |

Before a paid run, do one cheap task first and check the transcript's model
and usage fields: the CLI must bill the API key (not a logged-in
subscription) and the pinned model/effort must appear. Fix with the
identity's `extra_args` / `env`, not code.

## Judging

The `judge/` pipeline grades these attempts like any others (V1 rubric for v1
rows; the agentic judge + rubric_9 for v2 — see `judge/project_configs.yaml`).

## Tests

```bash
cd coding-agents-master && uv run pytest tests     # or run any file directly: uv run python tests/test_smoke.py
```

Offline, no Docker, DB, S3 or keys: config and prompt assembly, validation
verdicts and telemetry (`test_smoke`), the v1/v2 switch and the template
checksum guards plus attachment seeding (`test_benchmark_config`), the identity
registry rules (`test_agent_identity`), the `config/config.yaml` resolution
ladder (`test_repo_config`) and the trajectory relay (`test_relay`).
