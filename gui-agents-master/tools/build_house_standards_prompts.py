#!/usr/bin/env python3
"""Generate the rubric-free House Standards prompt sets — versions 204 (3-step,
prompts_v4/) and 205 (single-pass, prompts/v2_3.txt) — from the frozen 202 /
203 sources.

Patrick, 2026-09-10: the House Standards versions carry NO rubric material —
the agent gets the house standards file instead of the grading rubric — the
same design as the coding pipeline's rubric-scrubbed templates v10/v11
(coding-agents-master/tools/build_v10_v11_templates.py). 204/205 had been
cut on 2026-09-10 as "202/203 + house standards, rubric untouched"; the only
runs recorded under that text (attempts 1239, 1240) are deprecated, and the
numbers are kept.

Removed from the 202/203 text (same list as the coding v10 rules):
  * "You will be graded against the rubric below ... Category weights" paragraph
  * the "Build to the standard of a top professional-services firm"
    conventions summary (STRUCTURE & FLOW ... OUTPUTS & DOCUMENTATION)
  * the "== FULL RUBRIC ==" block (all 132 checks)
  * the rubric-derived QA audit list, replaced by a neutral verify-and-deliver
    step (workbook loads, Questions sheets intact and answered with live
    formulas, house standards followed)
  * the two rubric back-references ("built to the rubric below",
    "(including the rubric)")
Kept: the framing, the no-code-interpreter rule, the Summary-sheet plan, the
ANSWERS / Questions-sheet mechanics (the judge's harness answer check depends
on them), WORKING EFFICIENTLY, the download closing.
Added: the HOUSE STANDARDS block (read-now + follow-throughout + precedence)
and, in step 1 of the 3-step set, the read-it-now sentence.

Deterministic; every edit is an exact-once swap that exits if the anchor is
missing or ambiguous. Run from gui-agents-master/:
    uv run python tools/build_house_standards_prompts.py [--check]
--check regenerates to memory and exits 1 if any target file differs.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "tasks_configs" / "prompts"
V3 = ROOT / "tasks_configs" / "prompts_v3"
V4 = ROOT / "tasks_configs" / "prompts_v4"

HOUSE_BLOCK_SINGLE = (
    "HOUSE STANDARDS\n"
    "- A file named House_Standards_v1.md is attached with the case files. It "
    "sets our house conventions for building the model - structure, units, "
    "signs, assumptions, formulas, presentation and delivery. Read it before "
    "you build and follow it throughout.\n"
    "- Where the case instructions or this prompt conflict with the house "
    "standards, the case instructions and this prompt govern. Note any "
    "deliberate departure from the house standards on the cover, with the "
    "reason.\n"
)
# Step 2 of the 3-step set: step 1 already told the agent to read the file.
HOUSE_BLOCK_STEP2 = HOUSE_BLOCK_SINGLE.replace(
    "Read it before you build and follow it throughout.", "Follow it throughout."
)
STEP1_READ_NOW = (
    "A file named House_Standards_v1.md is attached with the case files. It "
    "sets our house conventions for building the model; read it now, alongside "
    "the case files, and let your build plan (item 4) follow its structure "
    "conventions.\n\n"
)

QUESTIONS_INTACT = (
    "The 'Questions' sheet is intact - same name, same questions in the same "
    "rows (each such sheet, where a task has more than one) - and every "
    "reserved answer cell in the column headed 'Answers' (row 2 down, one per "
    "question; column B in most tasks, column C in a few) contains a live "
    "formula referencing the model whose result follows the header row's "
    "answer-format instructions and the 'Unit' column where given. No reserved "
    "answer cell is empty or hard-coded."
)
HOUSE_FOLLOWED = (
    "The workbook follows House_Standards_v1.md wherever the case "
    "instructions and this prompt do not say otherwise, and any deliberate "
    "departure from it is noted on the cover with the reason."
)
LOADS_CLEAN = (
    "The workbook opens without errors or repair warnings, and every "
    "calculated value is a live Excel formula, not a typed constant."
)
DOWNLOAD_CLOSING = (
    "You MUST return the completed model as an actual, downloadable .xlsx "
    "file - the .xlsx workbook itself is the deliverable, NOT a text summary, "
    "description, or list of steps. Do not reply with only instructions or a "
    "plan; build the workbook and provide the finished .xlsx file for download."
)

WEIGHTS_START = "You will be graded against the rubric below"
CONVENTIONS_START = "Build to the standard of a top professional-services firm"
WORKING_HEADER = "WORKING EFFICIENTLY (manage message length and tool use)\n"
RUBRIC_INTRO = "Build the model now. The full rubric you will be scored against follows.\n"
QA_START = "QA PASS (do this yourself, in the same pass, before you deliver)\n"
EXECUTE_START = "Execute all of the above in one continuous pass."


def swap(text: str, old: str, new: str, what: str) -> str:
    n = text.count(old)
    if n != 1:
        sys.exit(f"{what}: expected exactly one occurrence of anchor, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def cut(text: str, start: str, end: str, what: str) -> str:
    """Remove text from the start anchor up to (not including) the end anchor."""
    i, j = text.find(start), text.find(end)
    if i < 0 or j < 0 or j <= i:
        sys.exit(f"{what}: anchors not found in order ({start[:40]!r} .. {end[:40]!r})")
    return text[:i] + text[j:]


def build_single_pass(src: str) -> str:
    s = src
    s = swap(
        s,
        "2. Model sheet(s) - the complete working model, built to the rubric below.\n",
        "2. Model sheet(s) - the complete working model, using live Excel formulas for every calculated value.\n",
        "single-pass item 2",
    )
    # House-standards block goes where 205 had it: after the Questions item,
    # in place of the weights paragraph + conventions summary.
    s = cut(s, WEIGHTS_START, WORKING_HEADER, "single-pass weights+conventions")
    s = swap(s, WORKING_HEADER, HOUSE_BLOCK_SINGLE + "\n" + WORKING_HEADER, "single-pass house block")
    verify = (
        "VERIFY AND DELIVER (do this yourself, in the same pass, before you deliver)\n"
        f"1. {LOADS_CLEAN}\n"
        f"2. {QUESTIONS_INTACT}\n"
        f"3. {HOUSE_FOLLOWED}\n"
        "Fix everything you find.\n\n"
    )
    s = cut(s, QA_START, EXECUTE_START, "single-pass QA list")
    s = swap(
        s,
        "never treat the interruption as a reason to skip the QA pass below.",
        "never treat the interruption as a reason to skip the verify-and-deliver step below.",
        "single-pass QA back-reference",
    )
    s = swap(s, EXECUTE_START, verify + EXECUTE_START, "single-pass verify block")
    i = s.find(RUBRIC_INTRO)
    if i < 0:
        sys.exit("single-pass: rubric intro not found")
    s = s[:i] + "Build the model now.\n"
    return s


def build_step1(src: str) -> str:
    anchor = "Step 1 - Analyze & plan."
    return swap(src, anchor, STEP1_READ_NOW + anchor, "step1 read-now")


def build_step2(src: str) -> str:
    s = src
    s = cut(s, WEIGHTS_START, "ANSWERS (the 'Questions' sheet)\n", "step2 weights paragraph")
    s = cut(s, CONVENTIONS_START, WORKING_HEADER, "step2 conventions summary")
    s = swap(s, WORKING_HEADER, HOUSE_BLOCK_STEP2 + "\n" + WORKING_HEADER, "step2 house block")
    i = s.find(RUBRIC_INTRO)
    if i < 0:
        sys.exit("step2: rubric intro not found")
    return s[:i] + "Build the model now.\n"


def build_step3() -> str:
    return (
        "Step 3 of 3 - Verify and deliver. Before downloading, check the workbook and fix everything you find:\n"
        f"1. {LOADS_CLEAN}\n"
        f"2. {QUESTIONS_INTACT}\n"
        f"3. {HOUSE_FOLLOWED}\n\n"
        f"{DOWNLOAD_CLOSING}\n"
    )


def main() -> int:
    outputs = {
        P / "v2_3.txt": build_single_pass((P / "v2_2.txt").read_text()),
        V4 / "step1_analyze.txt": build_step1((V3 / "step1_analyze.txt").read_text()),
        V4 / "step2_build.txt": build_step2((V3 / "step2_build.txt").read_text()),
        V4 / "step3_qa.txt": build_step3(),
    }
    for path, text in outputs.items():
        assert "== FULL RUBRIC" not in text and "Good:" not in text and "rubric" not in text.lower(), path
        assert "House_Standards_v1.md" in text, path
    if "--check" in sys.argv:
        bad = [p for p, t in outputs.items() if not p.exists() or p.read_text() != t]
        for p in bad:
            print(f"DIFFERS: {p}")
        return 1 if bad else 0
    for path, text in outputs.items():
        path.write_text(text)
        print(f"wrote {path.relative_to(ROOT)} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
