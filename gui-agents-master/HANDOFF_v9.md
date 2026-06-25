# MBABenchV2 / prompt_version 9 — Handoff Notes for Thomson

Draft consolidation of the non-obvious changes, gotchas, and open items from the
V2 GUI-agent enablement work. These have operational consequences for running the
agents and the judge correctly — they are **not** all visible from the diff alone.

Branch: `MBABenchV2_pshea`. Relevant commits: `c89ae76`, `0b75342`, `dc98348`,
`c2d4aff`, `17a406c`, `93dad9d`.

---

## 1. Must-know before grading or running

### Judge: grade v9 with `--agentic` — the standard path is NOT v9-compatible
Only the **agentic** judge carries the 12-category / 132-check v9 rubric (it's
data-driven via `check_order`). The standard/non-agentic path is hardcoded to 3
categories (`judge_template_7_0.yaml` + `judge.py` ~L883–885 = Accuracy / Formula /
Formatting). This is deliberate (route v9 through agentic; keep standard for legacy).
A guard at `judge.py` ~L884 now raises a clear `ValueError` if a non-agentic run gets
a non-3-category rubric (instead of a confusing `KeyError` on "Formula"). Legacy
3-category grading still works by pointing `project_configs.yaml`
rubric/weights/check_order back to `rubric_8` / `rubric_6_weights`.

### `configs.default.yaml` prompts block is still a placeholder, not the v9 prompts
The real 3-step v9 prompts (with the full 132-check rubric) live in the committed EC2
templates (`infra/dispatcher/config_templates/claude_opus_4_8.yaml` etc.). But
`prompts.value` in `configs.default.yaml` is still the old stub ("Analyze the attached
files…"). **Anyone setting up a local run from scratch inherits the stub and records
wrong-prompt attempts at `prompt_version=9`.** Fix before wider rollout: copy the
`prompts:` block from `claude_opus_4_8.yaml` into `configs.default.yaml`. Not a secret —
the prompts are already in the repo; purely a convenience/safety update.

---

## 2. Code changes with operational consequences

### ChatGPT model selection: data-testid → visible-text (commit `17a406c`)
OpenAI replaced the named-model switcher (GPT-5.x rows with `model-switcher-*`
data-testids) with an **"Intelligence" picker** whose rows carry **no data-testid**.
The old `MODEL_TESTIDS` lookup always missed → every run silently fell through to the
project's **default** model (Pro Extended, the slowest), making a single prompt take
10–50 min and confusing completion detection.

Rewrite (`chatgpt_web_agent.py` `ensure_model_selected`): map config values/aliases to
exact on-screen labels and click the row by `role` + exact text. Current labels
(verified live via CDP):

| config value | picker label | role |
|---|---|---|
| `instant` | Instant | menuitemradio |
| `medium` | Medium | menuitemradio |
| `high` | High | menuitemradio |
| `extra high` | Extra High | menuitemradio |
| `pro` | Pro Extended | menuitemradio |
| `gpt-5.5` | GPT-5.5 | menuitem |

If OpenAI relabels the picker again, the **only** thing to update is the `MODEL_LABELS`
map. Set the ChatGPT **project default** to something cheap (Instant) so a missed
selection doesn't strand a run on Pro Extended.

### Extended-thinking config is now NON-FATAL (commit `c89ae76`)
Claude.ai's ET UI is model-specific: Opus shows a real toggleable "Extended thinking"
switch (detected fine); Haiku 4.5 shows "Extended / Always uses deep reasoning"
(always-on, no toggle), which the "think"-based switch detection can't find. Previously
a detection miss made `ensure_model_config` return False → "Failed to configure model –
aborting" → whole run died (blocked the cheap Haiku test; Opus unaffected). Fix: if the
model is selected but ET can't be configured, log a warning and continue. If a future
model needs a real ET toggle the detection misses, update `_find_extended_thinking_item`
(text match requires "think").

### S3 attempt path is now task-name-based (commit `0b75342`)
Layout changed from `{prefix}/{agent_folder}/task_source={src}/task_id={id}/…` to
`{prefix}/{agent_folder}/{task_name}/…`
(e.g. `MBABenchV2/attempts/chatgpt_instant/ApfelInc/…`). `task_source` / `db_task_id`
still go into the `task_attempts` row, just not the S3 key. **Anything that parsed the
old Hive-style path must be updated.** One stale V8 test row still has dangling old-path
URIs (harmless for v9 dedup, which keys on `prompt_version=9`).

### `task_attempts` schema reminder
Columns: `id, task_id, agent_model_name, agent_model_type, attempt_files(jsonb),
prompt_files(jsonb), start_time, end_time, time_taken_min, cost, prompt_version,
agent_failed, agent_failed_reason, deprecated, created_at`. **No `status` column** —
success = `agent_failed=False`. Column is `task_id`, **not** `db_task_id`. Table is
unqualified `task_attempts` (search_path), not `mbabenchv2.task_attempts`.

---

## 3. Environment / infra

### EC2 needs IAM permissions not yet on Patrick's AWS user
`aws_bootstrap.sh` and `spinup.sh` need at least: `ec2:RunInstances`,
`ec2:DescribeInstances`, `ec2:DescribeKeyPairs`, `ec2:CreateKeyPair`,
`ec2:DescribeSecurityGroups`, `ec2:CreateSecurityGroup`,
`ec2:AuthorizeSecurityGroupIngress`, `iam:PassRole`. The `patrickshea` user currently
has S3 only. The `claude_opus_4_8.yaml` config template is in place — EC2 is code-ready,
just needs IAM. Workflow once unblocked: (1) `./infra/dispatcher/aws_bootstrap.sh` (once
per region), (2) `./infra/dispatcher/spinup.sh --alias claude-1 --config-template
infra/dispatcher/config_templates/claude_opus_4_8.yaml`.

### Long laptop runs need `caffeinate` (macOS)
These runs drive a real browser for many minutes/task. If the Mac sleeps it suspends
Chrome and drops Wi-Fi mid-generation → `Target page … has been closed` +
`net::ERR_INTERNET_DISCONNECTED` → wasted retry that restarts the whole 3-prompt
sequence (telltale: a multi-minute gap between 30s poll lines). Wrap runs:
`caffeinate -dimsu <cmd>`. EC2 runs are unaffected. Now documented in the README.

### `configs.yaml` is gitignored and must never be committed
Contains the Neon V2 DB URL, AWS keys, and the Claude project_id. Correctly listed in
`.gitignore` (verified). Stage explicit paths when committing — never `git add -A`.

---

## 4. Validation status (ladder test)

| Rung | Provider | Sink | Result |
|---|---|---|---|
| 1 | Claude (haiku) | local | ✅ pass (prior session) |
| 2 | Claude | S3 + DB | ✅ pass (prior session) |
| 1 | ChatGPT (instant) | local | ✅ pass — 831s, pv=9, 3 valid xlsx |
| 2 | ChatGPT (instant) | S3 + DB | ✅ pass — `task_attempts` id=3, pv=9, agent_failed=False, 15.4min |
| 3 | Judge | — | ⏳ blocked on OpenRouter API key |
| — | EC2 single task | — | ⏳ blocked on IAM permissions |

**Open items for Thomson:** (a) grant EC2 IAM or run bootstrap; (b) provide OpenRouter
key for the judge; (c) decide whether to bake v9 prompts into `configs.default.yaml`
before wider rollout.
