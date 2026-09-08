#!/usr/bin/env python3
"""Generate the rubric-scrubbed task templates v10 and v11 from v9.

Rubric-effect experiment (2026-09-08, Pat): the same Fable 5 max-effort
coding-agent cohort re-runs 30 tasks under two prompts that carry NO rubric
material, so the effect of showing the agent the grading rubric can be
measured against the existing v9 (prompt_version 109) rows.

  v10 (prompt_version 110) — v9 with every rubric-derived passage removed:
      * the "You will be graded against the rubric below ... Category
        weights" paragraph;
      * the "Build to the standard of a top professional-services firm"
        conventions summary (STRUCTURE & FLOW ... OUTPUTS & DOCUMENTATION);
      * the "== FULL RUBRIC ==" block (all 132 checks with Good/Bad
        standards);
      * Step 3's rubric-derived audit list, replaced by a neutral
        verify-and-deliver step (file loads, Questions sheets intact and
        answered with live formulas).
      Kept, because they define the task or the harness rather than the
      grading: the three-step framing, the Questions-sheet answer mechanics
      (the judge's harness answer check depends on them), the live-formula
      rule, the Summary-sheet planning step, WORKING EFFICIENTLY, and the
      EXCEL FILE VALIDITY REQUIREMENTS.
  v11 (prompt_version 111) — v10 plus a pointer to the house-standards file
      (prompts/house_standards_v1.md, staged into the workspace root as
      HOUSE_STANDARDS.md by prompt_builder.TEMPLATE_EXTRAS) and a matching
      line in the verify step.

Deterministic: run from coding-agents-master/ and diff. The generated files
are then md5-pinned in prompt_builder.SCRUBBED_MD5 so they cannot drift.

    uv run python tools/build_v10_v11_templates.py [--check]
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "coding_agent" / "prompts"
V9 = PROMPTS / "task_template_shared_v9.txt"
V10 = PROMPTS / "task_template_shared_v10.txt"
V11 = PROMPTS / "task_template_shared_v11.txt"

WEIGHTS_PARA_START = "You will be graded against the rubric below"
CONVENTIONS_START = "Build to the standard of a top professional-services firm"
WORKING_HEADER = "WORKING EFFICIENTLY\n"
RUBRIC_INTRO = "Build the model now. The full rubric you will be scored against follows.\n"
RUBRIC_MARKER = "== FULL RUBRIC"
STEP3_START = "Step 3 - QA and deliver."
VALIDITY_START = "EXCEL FILE VALIDITY REQUIREMENTS:"

STEP3_NEUTRAL = """Step 3 - Verify and deliver. Before finishing:
1. Re-open solution.xlsx and confirm it loads without errors or repair warnings and that every calculated value is stored as a live Excel formula, not a typed constant.
2. Confirm every 'Questions' sheet is intact - same name, same questions in the same rows (each such sheet, where a task has more than one) - and that every reserved answer cell in the column headed 'Answers' (row 2 down, one per question; column B in most tasks, column C in a few) contains a live formula referencing the model whose result follows the header row's answer-format instructions and the 'Unit' column where given. No reserved answer cell is empty or hard-coded.
{house_line}
Fix everything you find. The deliverable is solution.xlsx in the workspace root — the .xlsx workbook itself, NOT a text summary, description, or list of steps. Finish all steps and leave the completed workbook at that path.

"""

HOUSE_STANDARDS_BLOCK = """HOUSE STANDARDS
- The workspace root contains HOUSE_STANDARDS.md, our firm's financial-modelling house standards. Read it before you build and conform to it throughout. Where the case's own instructions conflict with it, the case instructions govern.

"""
HOUSE_STEP3_LINE = "3. Confirm the workbook conforms to HOUSE_STANDARDS.md in the workspace root."


def _cut(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + text[j:]


def build(v9: str) -> tuple[str, str]:
    t = v9
    # 1. drop the weights paragraph (ends at the blank line before ANSWERS)
    t = _cut(t, WEIGHTS_PARA_START, "\nANSWERS (the 'Questions' sheet)")
    t = t.replace("\n\n\nANSWERS", "\n\nANSWERS", 1)
    # 2. drop the conventions summary, keep WORKING EFFICIENTLY
    t = _cut(t, CONVENTIONS_START, WORKING_HEADER)
    # 3. drop the rubric intro sentence and the whole rubric block up to Step 3
    t = _cut(t, RUBRIC_INTRO, STEP3_START)
    t = t.replace(STEP3_START, "Build the model now.\n\n" + STEP3_START, 1)
    # 4. replace Step 3 with the neutral verify step
    i = t.index(STEP3_START)
    j = t.index(VALIDITY_START)
    v10 = t[:i] + STEP3_NEUTRAL.format(house_line="").replace("\n\nFix", "\nFix") + t[j:]
    # v11: house-standards pointer before WORKING EFFICIENTLY + step-3 line
    v11 = v10.replace(WORKING_HEADER, HOUSE_STANDARDS_BLOCK + WORKING_HEADER, 1)
    v11 = v11.replace("No reserved answer cell is empty or hard-coded.\nFix",
                      "No reserved answer cell is empty or hard-coded.\n" + HOUSE_STEP3_LINE + "\nFix", 1)
    # the validity block's chart note referenced "the grading" - neutral wording instead
    v10 = v10.replace("Charts are optional; the grading does not require them.", "Charts are optional; they are not required.", 1)
    v11 = v11.replace("Charts are optional; the grading does not require them.", "Charts are optional; they are not required.", 1)
    assert RUBRIC_MARKER not in v10 and "Good:" not in v10 and "rubric" not in v10.lower() and "grad" not in v10.lower()
    assert "HOUSE_STANDARDS.md" not in v10 and v11.count("HOUSE_STANDARDS.md") == 2
    return v10, v11


def main() -> int:
    check = "--check" in sys.argv
    v10, v11 = build(V9.read_text())
    for path, text in ((V10, v10), (V11, v11)):
        if check:
            if not path.exists() or path.read_text() != text:
                print(f"DRIFT: {path.name} differs from the generated text"); return 1
        else:
            path.write_text(text)
        print(f"{path.name}: {len(text):,} chars md5 {hashlib.md5(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
