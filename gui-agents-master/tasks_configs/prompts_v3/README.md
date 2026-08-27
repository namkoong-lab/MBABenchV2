# V2 benchmark prompts, Questions-sheet revision (rubric-v9)

`prompts_v2/` + one addition: every starting workbook carries a `Questions`
sheet (questions in column A from A2, reserved answer cells in column B from
B2, units in column C where given, answer-format instructions in the header
row), and the agent must preserve that sheet and fill column B with live
formulas referencing the model. Nothing else changed: the 132-check rubric
body is **byte-identical** to `prompts_v2/step2_build.txt` from the
`== FULL RUBRIC ==` marker onward, so scores stay comparable and any delta
is attributable to the answer-placement instruction alone.

| File | Step | Delta vs prompts_v2 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | item 2 now points the plan at the Questions sheet |
| `step2_build.txt` | 2 — Build | new ANSWERS block before the conventions; rubric untouched |
| `step3_qa.txt` | 3 — QA + download | new QA item 8 (Questions sheet intact + answered) |

This set is **prompt_version 202** in `tasks_configs/prompts/registry.yaml`.
The matching single-pass variant is `tasks_configs/prompts/v2_2.txt`
(**203**), which is v2_1 with the created-'Answers'-sheet deliverable
replaced by the Questions-sheet convention and the same QA item 8.

`excel-agents-master/tasks_configs/prompts_v3/` holds byte-identical copies
under the same number 202 (guarded by its `tests/test_prompt_parity.py`);
`cli-agents-master/tools/build_v13_prompts.py` and
`coding-agents-master/tools/build_v9_template.py` generate those pipelines'
v13 / v9 sets from these files. Do not edit any of the four in place — new
text = new number, here and in every mirror.
