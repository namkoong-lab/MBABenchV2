# Benchmark v2 prompts, Questions-sheet revision (prompt_version 202)

`prompts_v2/` + one addition: every starting workbook carries a `Questions`
sheet (questions in column A from A2, reserved answer cells in the column
headed 'Answers' — B in most tasks, C in a few — units in the 'Unit' column,
answer-format instructions in the header row). The agent must preserve that
sheet and fill the 'Answers' column with live formulas referencing the model.
The 132-check rubric body is byte-identical to `prompts_v2/step2_build.txt`
from the `== FULL RUBRIC ==` marker onward.

| File | Step | Delta vs prompts_v2 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | item 2 points the plan at the Questions sheet |
| `step2_build.txt` | 2 — Build | ANSWERS block before the conventions; rubric untouched |
| `step3_qa.txt` | 3 — QA + download | QA item 8 (Questions sheet intact + answered) |

Reached through `prompt_version: 202` in `tasks_configs/prompts/registry.yaml`.
The single-pass variant is `tasks_configs/prompts/v2_2.txt` (**203**).

`excel-agents-master/tasks_configs/prompts_v3/` holds byte-identical copies
under the same number (guarded by its `tests/test_prompt_parity.py`). Do not
edit either in place — new text = new number, here and in every mirror.
