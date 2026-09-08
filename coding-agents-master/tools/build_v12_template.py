"""Generate coding_agent/prompts/task_template_shared_v12.txt — the benchmark-v2
House Standards task template — from the v4 GUI prompt files, exactly as
tools/build_v9_template.py generated v9 from the v3 GUI files.

Sources (single source of truth, shared with the GUI pipeline):
    gui-agents-master/tasks_configs/prompts_v4/step1_analyze.txt
    gui-agents-master/tasks_configs/prompts_v4/step2_build.txt   (embeds the
        132-check rubric — copied byte-exact; byte-identical to prompts_v3's,
        so the v12 rubric guard equals v9's and any score delta vs v9 is
        attributable to the house standards alone)
    gui-agents-master/tasks_configs/prompts_v4/step3_qa.txt

prompts_v4 = prompts_v3 + the House_Standards_v1.md directive (step 1: read it
now; step 2: a HOUSE STANDARDS block; step 3: QA item 9). The GUI "attaches"
the file with the case files; here the runner seeds it into the workspace as
starting_files/House_Standards_v1.md (config.TEMPLATE_ATTACHMENTS), so the
directive names that path instead.

Harness-necessitated translations, the same rules v9 used plus the one above:
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
md5-guarded by prompt_builder (V12_RUBRIC_MD5).

Usage (from coding-agents-master):  python tools/build_v12_template.py
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_V4 = ROOT.parent / "gui-agents-master/tasks_configs/prompts_v4"
V7 = ROOT / "coding_agent/prompts/task_template_shared_v7.txt"
OUT = ROOT / "coding_agent/prompts/task_template_shared_v12.txt"

# Where the agent finds the file (workspace-relative; the runner seeds it).
STANDARDS_PATH = "starting_files/House_Standards_v1.md"

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
    step1 = (PROMPTS_V4 / "step1_analyze.txt").read_text()
    step2 = (PROMPTS_V4 / "step2_build.txt").read_text()
    step3 = (PROMPTS_V4 / "step3_qa.txt").read_text()
    v7 = V7.read_text()

    # ---- Step 1: the house-standards sentence sits before the step-1 heading
    # in the GUI file; lift it, retarget "attached" to the workspace path, and
    # put it in the same position in the preamble. swap() makes a wording
    # change in prompts_v4 fail the build instead of silently dropping it. ----
    house_sentence = step1[step1.index("A file named House_Standards_v1.md") : step1.index("\n\nStep 1 - Analyze & plan")]
    house_sentence = swap(
        house_sentence,
        "A file named House_Standards_v1.md is attached with the case files. "
        "It sets our house conventions for building the model; read it now, "
        "alongside the case files, and let your build plan (item 4) follow "
        "its structure conventions.",
        f"The file House_Standards_v1.md is in the workspace at {STANDARDS_PATH}. "
        "It sets our house conventions for building the model; read it now, "
        "alongside the case files and before you build, and let your build "
        "plan (item 4) follow its structure conventions.",
    )
    preamble = swap(HARNESS_PREAMBLE, "Step 1 - Analyze & plan.",
                    house_sentence + "\n\nStep 1 - Analyze & plan.")

    # ---- plan items byte-lifted from step1_analyze.txt (item 2 carries the
    # header-anchored 'Answers'-column wording) ----
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
    # HOUSE STANDARDS block: same retargeting as step 1.
    head = swap(
        head,
        "- A file named House_Standards_v1.md is attached with the case files. "
        "It sets our house conventions for building the model - structure, "
        "units, signs, assumptions, formulas, presentation and delivery. "
        "Follow it throughout.",
        f"- The file House_Standards_v1.md is in the workspace at {STANDARDS_PATH}; "
        "read it before you build. It sets our house conventions for building "
        "the model - structure, units, signs, assumptions, formulas, "
        "presentation and delivery. Follow it throughout.",
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

    # QA item 9 (house standards) names the file, not an attachment — no edit.
    assert "9. The workbook follows House_Standards_v1.md" in step3

    out = (
        preamble
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
    print("Set V12_RUBRIC_MD5 / V12_RUBRIC_LEN in prompt_builder.py to these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
