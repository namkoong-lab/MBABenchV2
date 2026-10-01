# V2 benchmark prompts, House Standards revision (rubric-free)

The House Standards prompt set: the agent is handed the house
financial-modelling conventions (`<repo>/house_standards/House_Standards_v1.md`,
attached with the case files) INSTEAD of the grading rubric. Generated from
the frozen `prompts_v3/` (202) sources by
`gui-agents-master/tools/build_house_standards_prompts.py`, which removes every
rubric passage and adds the house-standards directives.

| File | Step | Delta vs prompts_v3 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | one sentence added: read the attached House_Standards_v1.md now; the plan follows its structure conventions |
| `step2_build.txt` | 2 — Build | graded-against paragraph + category weights, the professional-services conventions summary and the 132-check rubric block removed; HOUSE STANDARDS block added after ANSWERS |
| `step3_qa.txt` | 3 — Verify + download | the rubric-derived QA audit list replaced by a neutral verify-and-deliver step: workbook loads, Questions sheets intact and answered with live formulas, house standards followed |

This set is **prompt_version 204** in `tasks_configs/prompts/registry.yaml`,
whose entry also declares the attachment (`attachments:`), so the version
selects the text and the file together. The matching single-pass variant is
`tasks_configs/prompts/v2_3.txt` (**205**, the repo default): the same
removals and additions applied to `prompts/v2_2.txt` (203).

Precedence is stated in the text: the case instructions and this prompt
govern over the house standards; departures are noted on the cover.

These files and `prompts/v2_3.txt` are byte-identical copies of the
`gui-agents-master/tasks_configs/` originals under the same numbers (guarded
by `tests/test_prompt_parity.py`): regenerate the originals with the builder
(`--check` verifies) and re-copy, never hand-edit. The cli v14 (later
scrubbed to v15) and coding template v12 sets were generated from the
earlier (rubric-inclusive) prompts_v4 text, so their builders no longer
reproduce them from these files. New text = new number, here and in every
mirror.
