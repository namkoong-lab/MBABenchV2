#!/usr/bin/env python3
"""Generate the rubric-scrubbed House-Standards prompt set (prompt_version v15,
recorded as 1509 = system v15 + template v9) from v14.

Mirrors coding-agents-master/tools/build_v10_v11_templates.py: the v2 rerun
prompts carry NO rubric material - the agent gets the task, the 'Questions'
sheet answer mechanics, the harness/tool guidance, and a pointer to the
house standards file (HOUSE_STANDARDS.md in the workspace, full text in the
context), nothing about how it is graded.

Removed from v14 (system prompt):
  * the "--- EVALUATION RUBRIC ---" header, the "You will be graded ...
    Category weights" paragraph, the "Build to the standard of a top
    professional-services firm" conventions summary, and the whole
    "== FULL RUBRIC ==" block (132 checks with Good/Bad standards);
  * every rubric reference in the tool-guidance body ("[rubric: X -- HOW]"
    tags, "to satisfy the rubric", "will be graded", "The rubric
    penalizes", "rubric convention", "(rubric: Potential Dangers)").
Kept, because they define the task or the harness rather than the grading:
the ANSWERS convention (the judge's harness answer check depends on it),
the live-formula rule, the MODEL BUILDING / DELIVERABLE & FORMATTING tool
guidance, and every tool/response-format section.

Removed from template v8: the step-2 "graded against the EVALUATION RUBRIC"
sentence and the step-3 rubric audit list, replaced by the neutral
verify-and-deliver step used by coding template v11 (file loads, every
calculated value is a live formula, Questions sheets intact and answered,
workbook conforms to HOUSE_STANDARDS.md).

House-standards wording follows coding template v11 exactly, adapted to the
CLI harness: the file is delivered into the workspace as HOUSE_STANDARDS.md
(prompt_versions.PROMPT_VERSIONS["v15"]["attachment_names"]) and, because
the agent has no tool that reads .md, its full text is also embedded in the
context under the HOUSE STANDARDS header.

Deterministic; run from cli-agents-master/ and diff. tests/test_v15_prompts.py
pins the generated text.

    uv run python tools/build_v15_prompts.py [--check]
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "excel_cli_agent" / "prompts"
SYS_IN, TPL_IN = PROMPTS / "system_prompt_v14.txt", PROMPTS / "task_template_shared_v8.txt"
SYS_OUT, TPL_OUT = PROMPTS / "system_prompt_v15.txt", PROMPTS / "task_template_shared_v9.txt"

RUBRIC_HEADER = "--- EVALUATION RUBRIC ---\n\n"
WEIGHTS_START = "You will be graded against the rubric below"
ANSWERS_START = "ANSWERS (the 'Questions' sheet)\n"
HOUSE_START = "HOUSE STANDARDS\n"
CONVENTIONS_START = "Build to the standard of a top professional-services firm"
TOOLS_START = "--- AVAILABLE TOOLS ---"

HOUSE_BLOCK = (
    "HOUSE STANDARDS\n"
    "- The workspace contains HOUSE_STANDARDS.md, our firm's financial-modelling "
    "house standards; its full text is included below in your context under the "
    "HOUSE STANDARDS header (do not try to open it with the Excel tools). Read it "
    "before you build and conform to it throughout. Where the case's own "
    "instructions conflict with it, the case instructions govern.\n"
)

STEP3_NEUTRAL = (
    "STEP 3 - VERIFY AND DELIVER:\n"
    "Before finishing:\n"
    "1. Re-read solution.xlsx with the tools (list_worksheets, get_used_range, get_formula) and confirm it opens without errors and that every calculated value is stored as a live Excel formula, not a typed constant.\n"
    "2. Confirm every 'Questions' sheet is intact - same name, same questions in the same rows (each such sheet, where a task has more than one) - and that every reserved answer cell in the column headed 'Answers' (row 2 down, one per question; column B in most tasks, column C in a few) contains a live formula referencing the model whose result follows the header row's answer-format instructions and the 'Unit' column where given. No reserved answer cell is empty or hard-coded.\n"
    "3. Confirm the workbook conforms to HOUSE_STANDARDS.md.\n"
    "\n"
    "Fix everything you find. The deliverable is the solution.xlsx workbook itself - it must contain the completed model, not a text description of it.\n"
)


def swap(text: str, old: str, new: str) -> str:
    """Exact-match replace that refuses to silently no-op or double-fire."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"expected exactly 1 occurrence, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def _cut(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + text[j:]


def build_system_prompt(v14: str) -> str:
    t = v14
    # 1. header + weights paragraph -> a neutral header; ANSWERS block kept.
    t = _cut(t, RUBRIC_HEADER, ANSWERS_START)
    t = t.replace(ANSWERS_START, "--- TASK CONVENTIONS ---\n\n" + ANSWERS_START, 1)
    # 2. the v14 HOUSE STANDARDS block (rubric-aware wording) -> the v11 wording.
    t = _cut(t, HOUSE_START, CONVENTIONS_START)
    # 3. conventions summary + the full rubric block, up to the tools section.
    t = _cut(t, CONVENTIONS_START, TOOLS_START)
    t = t.replace(TOOLS_START, HOUSE_BLOCK + "\n" + TOOLS_START, 1)
    # 4. rubric references in the tool-guidance body.
    t = swap(
        t,
        "If the original starting .xlsx file contains sheets that need to be modified to satisfy the rubric\n",
        "If the original starting .xlsx file contains sheets that need to be modified to complete the task\n",
    )
    t = swap(
        t,
        "sheets — every sheet in the final workbook will be graded, including sheets that came with the\n",
        "sheets — every sheet in the final workbook is part of the deliverable, including sheets that came with the\n",
    )
    for tag in (
        "WORKSHEET STRUCTURE [rubric: Structure -- HOW]:",
        "HARDCODED VALUE PREVENTION [rubric: Assumptions & Formulas -- HOW]:",
        "NUMBER SIGN CONSISTENCY [rubric: Potential Dangers -- HOW]:",
        "HELPER COLUMNS FOR COMPLEX FORMULAS [rubric: Formulas -- HOW]:",
        "IFERROR ON ALL DIVISIONS AND LOOKUPS [rubric: Error Checks -- HOW]:",
        "ABSOLUTE REFERENCES FOR CONSTANTS [rubric: Formulas -- HOW]:",
        "RANGE HYGIENE - NO FULL-COLUMN REFERENCES [rubric: Formulas & Flexibility -- HOW]:",
        "Formatting tool commands [rubric: Formatting -- HOW]:",
    ):
        t = swap(t, tag, re.sub(r" \[rubric: [^\]]+\]", "", tag))
    t = swap(
        t,
        "The rubric penalizes volatile functions (INDIRECT, OFFSET, TODAY, NOW, RAND) -- avoid them.",
        "Avoid volatile functions (INDIRECT, OFFSET, TODAY, NOW, RAND).",
    )
    t = swap(
        t,
        "Color Standards (font color, NOT cell fill) -- rubric convention: blue=inputs, black=formulas, green=cross-sheet links, red=external links:",
        "Color Standards (font color, NOT cell fill) -- convention: blue=inputs, black=formulas, green=cross-sheet links, red=external links:",
    )
    t = swap(
        t,
        "- External links are forbidden (rubric: Potential Dangers) -- there should be none to color red",
        "- External links are forbidden -- there should be none to color red",
    )
    return t


def build_task_template(v8: str) -> str:
    t = v8
    # step 1: the v14 attachment sentence -> the v11-style pointer.
    t = swap(
        t,
        "The house financial-modelling conventions are provided with the task files as House_Standards_v1.md and its full text is included in your context under the HOUSE STANDARDS header. It sets our house conventions for building the model; read it now, alongside the case files, and let your build plan (item 4) follow its structure conventions.\n",
        "Read HOUSE_STANDARDS.md, our firm's financial-modelling house standards (full text in your context under the HOUSE STANDARDS header), alongside the case files, and let your build plan (item 4) conform to it.\n",
    )
    # step 2: drop the grading sentence; keep the build instruction.
    t = swap(
        t,
        "Build the complete Excel model in solution.xlsx, using live Excel formulas for every calculated value. You will be graded against the EVALUATION RUBRIC in your system instructions (12 weighted categories, 132 checks) - build so that every check meets its \"Good\" standard and avoids its \"Bad\" standard.\n",
        "Build the complete Excel model in solution.xlsx, using live Excel formulas for every calculated value. Conform to HOUSE_STANDARDS.md throughout; where the case's own instructions conflict with it, the case instructions govern.\n",
    )
    t = swap(
        t,
        "If the starting .xlsx file already contains sheets that need modification to satisfy the rubric, modify those original sheets directly - every sheet in the final workbook will be graded, including sheets that came with the starting file.\n",
        "If the starting .xlsx file already contains sheets that need modification to complete the task, modify those original sheets directly - every sheet in the final workbook is part of the deliverable, including sheets that came with the starting file.\n",
    )
    # step 3: the rubric audit list -> the neutral verify step.
    i = t.index("STEP 3 - QA AND DELIVER:")
    return t[:i] + STEP3_NEUTRAL


def check_scrubbed(text: str, name: str) -> None:
    for pat in (r"== FULL RUBRIC", r"\bGood:", r"\brubric\b", r"\bgraded\b", r"\bgrading\b", r"House_Standards_v1"):
        if re.search(pat, text, re.IGNORECASE):
            raise SystemExit(f"{name}: rubric/attachment material survived: {pat!r}")


def main() -> int:
    check = "--check" in sys.argv
    sys_prompt = build_system_prompt(SYS_IN.read_text())
    template = build_task_template(TPL_IN.read_text())
    check_scrubbed(sys_prompt, SYS_OUT.name)
    check_scrubbed(template, TPL_OUT.name)
    assert sys_prompt.count("HOUSE_STANDARDS.md") == 1 and template.count("HOUSE_STANDARDS.md") == 3
    for path, text in ((SYS_OUT, sys_prompt), (TPL_OUT, template)):
        if check:
            if not path.exists() or path.read_text() != text:
                print(f"DRIFT: {path.name} differs from the generated text"); return 1
        else:
            path.write_text(text)
        print(f"{path.name}: {len(text):,} chars md5 {hashlib.md5(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
