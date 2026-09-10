# V2 benchmark prompts, House Standards revision (rubric-free)

The House Standards prompt set: the agent is handed the house
financial-modelling conventions (`<repo>/house_standards/House_Standards_v1.md`,
attached with the case files) INSTEAD of the grading rubric. Generated from
the frozen `prompts_v3/` (202) sources by
`tools/build_house_standards_prompts.py`, which removes every rubric passage
and adds the house-standards directives — the same design as the coding
pipeline's rubric-scrubbed v10/v11 templates (Patrick, 2026-09-10).

| File | Step | Delta vs prompts_v3 |
|---|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan | one sentence added: read the attached House_Standards_v1.md now; the plan follows its structure conventions |
| `step2_build.txt` | 2 — Build | graded-against paragraph + category weights, the professional-services conventions summary and the 132-check rubric block removed; HOUSE STANDARDS block added after ANSWERS |
| `step3_qa.txt` | 3 — Verify + download | the rubric-derived QA audit list replaced by a neutral verify-and-deliver step: workbook loads, Questions sheets intact and answered with live formulas, house standards followed |

Kept from 202: the framing, the no-code-interpreter rule, the Summary-sheet
plan, the ANSWERS / Questions-sheet mechanics (the judge's harness answer
check depends on them), WORKING EFFICIENTLY, the download closing.

This set is **prompt_version 204** in `tasks_configs/prompts/registry.yaml`,
whose entry also declares the attachment (`attachments:`), so the version
selects the text and the file together. The matching single-pass variant is
`tasks_configs/prompts/v2_3.txt` (**205**, the repo default): the same
removals and additions applied to `prompts/v2_2.txt` (203).

Precedence is stated in the text: the case instructions and this prompt
govern over the house standards; departures are noted on the cover.

History: 204/205 were first cut on 2026-09-10 as "202/203 + house standards,
rubric untouched" and redefined the same day; the only runs recorded under
the earlier text (attempts 1239, 1240 at 205) are deprecated.

`excel-agents-master/tasks_configs/prompts_v4/` and `prompts/v2_3.txt` hold
byte-identical copies under the same numbers (guarded by its
`tests/test_prompt_parity.py`). Regenerate with the builder (`--check`
verifies), never hand-edit; the cli v14 (later scrubbed to v15) and coding v12 sets were generated
from the EARLIER (rubric-inclusive) prompts_v4 text and are pinned by their
own checksums — regenerating them now would change them.
