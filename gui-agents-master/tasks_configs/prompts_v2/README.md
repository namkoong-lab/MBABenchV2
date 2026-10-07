# Benchmark v2 prompts, rubric-v9 3-step set (prompt_version 200)

Reached through `prompt_version: 200` in `tasks_configs/prompts/registry.yaml`.
All three files are sent verbatim to every provider, one chat turn each.

| File | Step |
|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan (Summary sheet) |
| `step2_build.txt` | 2 — Build (embeds the full 132-check rubric) |
| `step3_qa.txt` | 3 — QA + download |

The single-pass variant is `tasks_configs/prompts/v2_1.txt` (**201**): the same
deliverables and QA checklist in one turn, with the rubric body byte-identical
to `step2_build.txt` from its `== FULL RUBRIC ==` marker onward. At ~67k chars
it is one very large turn; the Claude agent's truncation-continue loop (up to 5
"Continue" sends) relies on the prompt's checkpoint wording — do not strip it.

Later sets build on these files: `prompts_v3/` (202) adds the Questions-sheet
convention; `prompts_v4/` (204) replaces the rubric with the house standards.
The repo default is **205** (`prompts/v2_3.txt`). Not to be confused with
`prompts_pv9/`, the benchmark-v1 text against a 17-check rubric.

These files are frozen; new text = new version.
