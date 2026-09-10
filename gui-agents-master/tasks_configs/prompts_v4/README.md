# V2 benchmark prompts, House Standards revision (rubric-v9)

`prompts_v3/` + one addition: the house financial-modelling conventions in
`<repo>/house_standards/House_Standards_v1.md` are attached with the case
files, and the prompts tell the agent to follow them. Nothing else changed:
the 132-check rubric body is **byte-identical** to `prompts_v2/step2_build.txt`
from the `== FULL RUBRIC ==` marker onward, so scores stay comparable and any
delta is attributable to the house standards alone.

| File | Step | Delta vs prompts_v3 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | one sentence: read the attached House_Standards_v1.md now; the plan follows its structure conventions |
| `step2_build.txt` | 2 — Build | new HOUSE STANDARDS block after ANSWERS, before the conventions; rubric untouched |
| `step3_qa.txt` | 3 — QA + download | new QA item 9 (workbook follows the standards; departures noted on the cover) |

This set is **prompt_version 204** in `tasks_configs/prompts/registry.yaml`,
whose entry also declares the attachment (`attachments:`), so the version
selects the text and the file together. The matching single-pass variant is
`tasks_configs/prompts/v2_3.txt` (**205**): v2_2 with the HOUSE STANDARDS
block before the graded-rubric sentence and the same QA item 9.

Precedence is stated in the text: the case instructions and this prompt
(including the rubric) govern over the house standards.

`excel-agents-master/tasks_configs/prompts_v4/` holds byte-identical copies
under the same number 204 (guarded by its `tests/test_prompt_parity.py`);
the cli v14 and coding template v10 sets are generated from these files by
their `tools/build_*` scripts. Do not edit any of them in place — new text =
new number, here and in every mirror.
