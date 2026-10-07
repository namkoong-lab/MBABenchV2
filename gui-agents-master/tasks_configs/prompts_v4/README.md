# Benchmark v2 prompts, House Standards revision (prompt_version 204)

The agent is handed the house financial-modelling conventions
(`<repo>/house_standards/House_Standards_v1.md`, attached with the case files)
INSTEAD of the grading rubric. Generated from the frozen `prompts_v3/` (202)
sources by `tools/build_house_standards_prompts.py`, which removes every
rubric passage and adds the house-standards directives.

| File | Step | Delta vs prompts_v3 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | read the attached House_Standards_v1.md first; the plan follows its structure conventions |
| `step2_build.txt` | 2 — Build | graded-against paragraph, category weights, conventions summary and the 132-check rubric block removed; HOUSE STANDARDS block added after ANSWERS |
| `step3_qa.txt` | 3 — Verify + download | rubric-derived QA list replaced by a neutral verify-and-deliver step: workbook loads, Questions sheets intact and answered with live formulas, house standards followed |

Kept from 202: the framing, the no-code-interpreter rule, the Summary-sheet
plan, the ANSWERS / Questions-sheet mechanics, WORKING EFFICIENTLY, the
download closing. Precedence is stated in the text: the case instructions and
the prompt govern over the house standards; departures are noted on the cover.

Reached through `prompt_version: 204` in `tasks_configs/prompts/registry.yaml`,
whose entry also declares the attachment, so the version selects the text and
the file together. The single-pass variant is `tasks_configs/prompts/v2_3.txt`
(**205**, the repo default): the same removals and additions applied to
`prompts/v2_2.txt` (203).

`excel-agents-master/tasks_configs/prompts_v4/` and `prompts/v2_3.txt` hold
byte-identical copies under the same numbers (guarded by its
`tests/test_prompt_parity.py`). Regenerate with the builder (`--check`
verifies); never hand-edit.
