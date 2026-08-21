# V2 benchmark prompts (rubric-v9)

Single source of truth for the MBABenchV2 (benchmark: v2) prompt sequence.
Every run reaches these files the same way: the run config sets
`prompt_version`, and the registry in `tasks_configs/prompts/registry.yaml`
maps it to the files below. That holds for local runs and for the EC2
dispatcher templates in `infra/dispatcher/config_templates/`, which set
`prompt_version: 200` and carry no prompt text of their own.

## 3-step set (the default)

| File | Step | Used by |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan (Summary sheet) | all providers |
| `step2_build.txt` | 2 — Build (embeds the full 132-check rubric) | all providers |
| `step3_qa.txt` | 3 — QA + download | all providers |

All three files are sent verbatim to every provider.

This set is **prompt_version 200** in `tasks_configs/prompts/registry.yaml`.

## Single-pass variant

The single-pass v2 prompt lives at `tasks_configs/prompts/v2_1.txt` and is
**prompt_version 201**. It folds the same three deliverables (Summary →
model → Answers) plus the step-3 QA checklist into ONE chat turn, and
carries the **identical 132-check rubric body — byte-for-byte the same text
as `step2_build.txt` from its `== FULL RUBRIC ==` marker onward**. Only the
framing around the rubric differs, so the graded standard is unchanged and
201 attempts stay comparable to 200 ones.

Caveat: at ~67k chars it is one very large turn, and the model will likely
hit its output cap mid-build. That is survivable on claude — the agent's
per-prompt truncation-continue loop (`claude_web_agent.py`, up to 5
"Continue" sends) resumes the build — but the prompt's own checkpoint
wording is what keeps a resumed turn from restarting the model. Do not
strip it. ChatGPT Pro Extended has no equivalent guarantee; 200 remains the
safer choice there.

DB rows for runs sent with these prompts carry the version that selected
them (200 or 201).
Not to be confused with `tasks_configs/prompts_pv9/` — that directory holds
the **V1 benchmark** (BizbenchV1) single-prompt payloads, which share the
"pv9" label but are different text against a different (17-check) rubric.
