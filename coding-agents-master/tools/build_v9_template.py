"""Generate coding_agent/prompts/task_template_shared_v9.txt — the benchmark-v2
Questions-sheet task template — from the v3 GUI prompt files, exactly as
tools/build_v8_template.py generated v8 from the v2 GUI files.

Sources (single source of truth, shared with the GUI pipeline):
    gui-agents-master/tasks_configs/prompts_v3/step1_analyze.txt
    gui-agents-master/tasks_configs/prompts_v3/step2_build.txt   (embeds the
        132-check rubric — copied byte-exact; carries the 2026-08 revision,
        so it deliberately differs from frozen prompts_v2 / template v8)
    gui-agents-master/tasks_configs/prompts_v3/step3_qa.txt

Harness-necessitated translations, the same rules v8 used plus one:
  * "in the open workbook" / "attached file(s)"  ->  solution.xlsx in the
    workspace root, built from starting_files/
  * the GUI's "no code interpreter" ban          ->  code may build the
    workbook, but every calculated value must be a live Excel formula
  * chat-specific flow                           ->  single-session coding-
    agent equivalents
  * the ANSWERS block's "carry the sheet over" wording  ->  start
    solution.xlsx as a copy of the starting workbook so the 'Questions'
    sheet survives.
Everything from the "== FULL RUBRIC" marker onward is copied byte-exact and
md5-guarded by prompt_builder (V9_RUBRIC_MD5).

Usage (from coding-agents-master):  python tools/build_v9_template.py
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_V3 = ROOT.parent / "gui-agents-master/tasks_configs/prompts_v3"
V7 = ROOT / "coding_agent/prompts/task_template_shared_v7.txt"
OUT = ROOT / "coding_agent/prompts/task_template_shared_v9.txt"

HARNESS_PREAMBLE = """\
You are an expert financial-modeling agent building an Excel solution for the provided case. You will work in THREE steps, completing all of them in this session without stopping.

Your only allowed method for solving this problem is building an Excel model in a workbook named solution.xlsx in the workspace root, using the starting workbook and data files provided in the starting_files/ directory. You may write and run code to construct and inspect the workbook, but you must not use code to find the final answers: the Excel file you build must be the only thing performing the calculations. Every calculated value in the workbook must be a live Excel formula; never compute a result outside Excel and paste it in as a constant.

Step 1 - Analyze & plan. Review the file(s) in starting_files/ and produce a brief written analysis on a new sheet named 'Summary', covering:
"""


def swap(text: str, old: str, new: str) -> str:
    """Exact-match replace that refuses to silently no-op or double-fire."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"expected exactly 1 occurrence, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def main() -> int:
    step1 = (PROMPTS_V3 / "step1_analyze.txt").read_text()
    step2 = (PROMPTS_V3 / "step2_build.txt").read_text()
    step3 = (PROMPTS_V3 / "step3_qa.txt").read_text()
    v7 = V7.read_text()

    # ---- Step 1: plan items byte-lifted from step1_analyze.txt (item 2
    # carries the header-anchored 'Answers'-column wording) ----
    plan_items = step1[step1.index("1. Task type") : step1.index("\n\nDo not start building")]

    # ---- Step 2: harness-translate the head; keep the rubric byte-exact ----
    marker = "== FULL RUBRIC"
    head, rubric = step2[: step2.index(marker)], step2[step2.index(marker):]

    head = swap(
        head,
        "Step 2 of 3 - Build the model. Build the complete Excel model now, "
        "in the open workbook, using live Excel formulas only (no external "
        "tools - the workbook must perform every calculation).",
        "Step 2 - Build the model. Build the complete Excel model now, in "
        "solution.xlsx, using live Excel formulas for every calculated "
        "value (your code may write the workbook, but the workbook must "
        "perform every calculation).",
    )
    # Coding harness: solution.xlsx is built in the workspace, so name the
    # route that carries the 'Questions' sheet over.
    head = swap(
        head,
        "If you build the model in a new workbook, carry the sheet over intact.",
        "solution.xlsx must contain this sheet - the simplest way is to "
        "start solution.xlsx as a copy of the starting workbook so the "
        "sheet carries over intact.",
    )
    # Chat-flow efficiency block -> coding-agent wording.
    head = swap(
        head,
        "WORKING EFFICIENTLY (manage message length and tool use)\n"
        "- Do your work directly in the workbook; keep chat narration to one "
        "or two sentences per step. The workbook is the deliverable, not a "
        "written description of it.\n"
        "- Build efficiently: write whole sheets/ranges in as few operations "
        "as possible rather than cell-by-cell, and do not re-verify or repeat "
        "work you have already completed.\n"
        "- Build methodically. If you approach a message-length or tool-use "
        "limit, stop at a clean checkpoint - you will be prompted to "
        "continue. When you continue, resume exactly where you stopped; "
        "never restart the model or repeat completed work.",
        "WORKING EFFICIENTLY\n"
        "- Do your work directly in the workbook; keep narration to one or "
        "two sentences per step. The workbook is the deliverable, not a "
        "written description of it.\n"
        "- Build efficiently: write whole sheets/ranges in as few operations "
        "as possible rather than cell-by-cell, and do not re-verify or "
        "repeat work you have already completed.",
    )

    # ---- Step 3: download wording -> workspace deliverable ----
    step3 = swap(step3, "Step 3 of 3 - QA and deliver. Before downloading, ",
                 "Step 3 - QA and deliver. Before finishing, ")
    step3 = swap(
        step3,
        "You MUST return the completed model as an actual, downloadable "
        ".xlsx file — the .xlsx workbook itself is the deliverable, NOT a "
        "text summary, description, or list of steps. Do not reply with "
        "only instructions or a plan; build the workbook and provide the "
        "finished .xlsx file for download.",
        "The deliverable is solution.xlsx in the workspace root — the "
        ".xlsx workbook itself, NOT a text summary, description, or list "
        "of steps. Finish all steps and leave the completed workbook at "
        "that path.",
    )

    # ---- Excel validity addendum: byte-same as v7's ----
    addendum_marker = "EXCEL FILE VALIDITY REQUIREMENTS:"
    addendum = v7[v7.index(addendum_marker):]

    out = (
        HARNESS_PREAMBLE
        + plan_items
        + "\n\n"
        + head
        + rubric.rstrip("\n")
        + "\n\n"
        + "--------------------------------------------------------------------------------\n\n"
        + step3.rstrip("\n")
        + "\n\n"
        + addendum
    )
    OUT.write_text(out)

    rubric_md5 = hashlib.md5(rubric.encode()).hexdigest()
    n_checks = len(re.findall(r"\n\s*Good:", rubric))
    print(f"Wrote {OUT.name}: {len(out)} chars")
    print(f"Rubric section: {len(rubric)} chars, {n_checks} Good-standards, "
          f"md5={rubric_md5}")
    print("Set V9_RUBRIC_MD5 / V9_RUBRIC_LEN in prompt_builder.py to these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
