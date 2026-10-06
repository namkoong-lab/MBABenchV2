# V2 benchmark prompts, House Standards revision (rubric-free)

Byte-identical copies of `gui-agents-master/tasks_configs/prompts_v4/` (the
3-step House Standards set, **prompt_version 204**) and, for the single-turn
variant, `tasks_configs/prompts/v2_3.txt` (**205**, the default): the agent is
handed the house financial-modelling conventions
(`<repo>/house_standards/House_Standards_v1.md`, attached with the case files)
instead of the grading rubric. Every rubric passage of the 202/203 text was
removed and the house-standards directives added; the ANSWERS / Questions-sheet
mechanics, the no-code-interpreter rule, the Summary-sheet plan and the
download closing are kept. The gui README of the same folder has the
step-by-step delta.

| File | Step |
|---|---|
| `step1_analyze.txt` | 1 — Analyze & plan; read the attached House_Standards_v1.md first |
| `step2_build.txt` | 2 — Build; HOUSE STANDARDS block after ANSWERS, no rubric |
| `step3_qa.txt` | 3 — Verify + download: workbook loads, Questions sheets intact and answered with live formulas, house standards followed |

`tasks_configs/prompts/registry.yaml` declares the attachment on both entries,
so the version selects the text and the file together. The byte parity with the
gui copies is enforced by `tests/test_prompt_parity.py`; regenerate both from
the gui builder (`gui-agents-master/tools/build_house_standards_prompts.py`),
never hand-edit. Precedence is stated in the text: the case instructions and
the prompt govern over the house standards; departures are noted on the cover.
