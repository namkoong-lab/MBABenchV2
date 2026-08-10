# V2 benchmark prompts (rubric-v9, 3-step)

Single source of truth for the MBABenchV2 (benchmark: v2) prompt sequence.
Extracted byte-exact from the committed EC2 dispatcher templates
(`infra/dispatcher/config_templates/*.yaml`) on 2026-08-10 — those templates
keep their inline copies as a frozen record of what production boxes send;
local runs should reference these files via `prompts_file` so every config
shares one copy (see `configs.default.yaml`).

| File | Step | Used by |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan (Summary sheet) | all providers |
| `step2_build.txt` | 2 — Build (embeds the full 132-check rubric) | claude, chatgpt pro |
| `step3_qa.txt` | 3 — QA + download | claude, chatgpt pro |
| `agent_step2_build.txt` | 2 — Build (ChatGPT Agent wording) | chatgpt agent_mode |
| `agent_step3_qa.txt` | 3 — QA (ChatGPT Agent wording) | chatgpt agent_mode |

Step 1 is byte-identical across all providers. The agent-mode variants differ
only in harness wording (Agent sandbox vs chat code interpreter).

DB rows for runs sent with these prompts carry `prompt_version: 9`.
Not to be confused with `tasks_configs/prompts_pv9/` — that directory holds
the **V1 benchmark** (BizbenchV1) single-prompt payloads, which share the
"pv9" label but are different text against a different (17-check) rubric.
