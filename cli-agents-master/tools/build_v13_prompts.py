"""Generate the benchmark-v2 Questions-sheet prompt set (prompt_version v13)
from the v3 GUI prompts, exactly as tools/build_v12_prompts.py generated v12
from the v2 GUI prompts.

Outputs (excel_cli_agent/prompts/):
    system_prompt_v13.txt        system_prompt_v11 body with the v1 17-check
                                 rubric block replaced by the v3 grading intro
                                 (which adds the ANSWERS / 'Questions'-sheet
                                 convention), conventions, and the 132-check
                                 rubric (copied byte-exact from
                                 prompts_v3/step2_build.txt), plus the same
                                 body fix-ups v12 applied.
    task_template_shared_v7.txt  one shared task template (fmwc + wsp slots)
                                 carrying v5's harness-critical blocks and the
                                 v3 three-step flow from step1_analyze.txt /
                                 step3_qa.txt (whose QA list now ends with the
                                 Questions-sheet item 8).

Sources (single source of truth, shared with the GUI pipeline):
    gui-agents-master/tasks_configs/prompts_v3/step1_analyze.txt
    gui-agents-master/tasks_configs/prompts_v3/step2_build.txt
    gui-agents-master/tasks_configs/prompts_v3/step3_qa.txt

Harness-necessitated translations on top of v12's rules:
  * "prefer XLOOKUP / IFS / LET"  ->  LET dropped (not in the MCP formula
    validator's whitelist)
  * chat-flow wording             ->  single-session CLI-loop equivalents
  * the ANSWERS block's "carry the sheet over" wording  ->  names copy_file
    as the way to start solution.xlsx from the starting workbook so the
    'Questions' sheet survives.
  * the v11 body's "create separate answer sheets per question" deliverable-
    structure advice  ->  answer in the 'Questions' sheet's reserved cells
    (it contradicted the ANSWERS convention this version adds).
Everything from the "== FULL RUBRIC" marker onward is copied byte-exact
(and is byte-identical to v12's rubric — the v3 revision does not touch the
rubric); tests/test_v13_prompts.py asserts that equality plus the 132-check
count.

Usage (from cli-agents-master):  python tools/build_v13_prompts.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "excel_cli_agent" / "prompts"
PROMPTS_V3 = ROOT.parent / "gui-agents-master" / "tasks_configs" / "prompts_v3"
SYS_OUT = PROMPTS / "system_prompt_v13.txt"
TPL_OUT = PROMPTS / "task_template_shared_v7.txt"

RUBRIC_MARKER = "== FULL RUBRIC"


def swap(text: str, old: str, new: str) -> str:
    """Exact-match replace that refuses to silently no-op or double-fire."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"expected exactly 1 occurrence, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def build_system_prompt() -> str:
    v11 = (PROMPTS / "system_prompt_v11.txt").read_text()
    step2 = (PROMPTS_V3 / "step2_build.txt").read_text()

    rubric = step2[step2.index(RUBRIC_MARKER):].rstrip("\n")
    head = step2[: step2.index(RUBRIC_MARKER)]

    # Grading intro + ANSWERS block + conventions: from the weights sentence
    # up to (not including) the chat-specific WORKING EFFICIENTLY block.
    grading_head = head[head.index("You will be graded") : head.index("WORKING EFFICIENTLY")].rstrip("\n")
    grading_head = swap(
        grading_head,
        "prefer XLOOKUP / IFS / LET.",
        "prefer XLOOKUP / IFS (LET is not supported by the formula tools).",
    )
    # CLI harness: the deliverable is solution.xlsx built by the tools, so
    # name the tool that carries the 'Questions' sheet over.
    grading_head = swap(
        grading_head,
        "If you build the model in a new workbook, carry the sheet over intact.",
        "In solution.xlsx this sheet must be present and answered - the "
        "simplest way is to create solution.xlsx by copying the starting "
        "workbook (copy_file), which carries the sheet over intact.",
    )

    rubric_block = (
        "--- EVALUATION RUBRIC ---\n\n"
        + grading_head
        + "\n\n"
        + rubric
        + "\n\n"
    )

    start = v11.index("--- EVALUATION RUBRIC ---")
    end = v11.index("--- AVAILABLE TOOLS ---")
    out = v11[:start] + rubric_block + v11[end:]

    # v1 criterion-number labels -> v2 category labels (same set as v12).
    for old, new in [
        ("WORKSHEET STRUCTURE [rubric criterion 10 -- HOW]:",
         "WORKSHEET STRUCTURE [rubric: Structure -- HOW]:"),
        ("HARDCODED VALUE PREVENTION [rubric criterion 7 -- HOW]:",
         "HARDCODED VALUE PREVENTION [rubric: Assumptions & Formulas -- HOW]:"),
        ("NUMBER SIGN CONSISTENCY [rubric criterion 4 -- HOW]:",
         "NUMBER SIGN CONSISTENCY [rubric: Potential Dangers -- HOW]:"),
        ("HELPER COLUMNS FOR COMPLEX FORMULAS [rubric criterion 5 -- HOW]:",
         "HELPER COLUMNS FOR COMPLEX FORMULAS [rubric: Formulas -- HOW]:"),
        ("IFERROR ON ALL DIVISIONS AND LOOKUPS [rubric criterion 6 -- HOW]:",
         "IFERROR ON ALL DIVISIONS AND LOOKUPS [rubric: Error Checks -- HOW]:"),
        ("ABSOLUTE REFERENCES FOR CONSTANTS [rubric criterion 9 -- HOW]:",
         "ABSOLUTE REFERENCES FOR CONSTANTS [rubric: Formulas -- HOW]:"),
        ("RANGE HYGIENE - NO FULL-COLUMN REFERENCES [rubric criterion 8 -- HOW]:",
         "RANGE HYGIENE - NO FULL-COLUMN REFERENCES [rubric: Formulas & Flexibility -- HOW]:"),
        ("Formatting tool commands [rubric criteria 12-17 -- HOW]:",
         "Formatting tool commands [rubric: Formatting -- HOW]:"),
    ]:
        out = swap(out, old, new)

    # The v2 rubric's "Avoid volatile functions" check names OFFSET; the v1
    # guidance recommended it for dynamic ranges.
    out = swap(
        out,
        "For time-series or expandable data, prefer dynamic ranges using OFFSET or INDEX:\n"
        "- =SUM(OFFSET(B2,0,0,COUNTA(B:B)-1,1))  -- sums exactly as many data rows as exist\n"
        "- =SUM(B2:INDEX(B:B,COUNTA(B:B)))         -- dynamic end using INDEX\n"
        "Use these when the model may expand (e.g., adding periods). Use explicit bounds (B2:B100) for fixed-size models.\n"
        "NOTE: Excel Tables and structured references are not supported by the MCP tools. Use OFFSET/INDEX for dynamic ranges instead.",
        "For time-series or expandable data, prefer a non-volatile INDEX-based dynamic end:\n"
        "- =SUM(B2:INDEX(B2:B500,COUNTA(B2:B500)))  -- sums exactly as many data rows as exist\n"
        "Use this when the model may expand (e.g., adding periods). Use explicit bounds (B2:B100) for fixed-size models.\n"
        "The rubric penalizes volatile functions (INDIRECT, OFFSET, TODAY, NOW, RAND) -- avoid them.\n"
        "NOTE: Excel Tables and structured references are not supported by the MCP tools. Use INDEX-based dynamic ranges instead.",
    )

    # v2 font-color convention: red = external links (which the rubric also
    # forbids), not orange = checks.
    out = swap(
        out,
        'Color Standards (font color, NOT cell fill):\n'
        '- Input cells (hardcoded values): font: {"color": "0000FF"}\n'
        '- Formula cells: Black font (default, no action needed)\n'
        '- Cross-sheet link formulas: font: {"color": "008000"}\n'
        '- Check/validation formulas: font: {"color": "FF8C00"}\n'
        '- Header rows: font: {"color": "FFFFFF", "bold": true}, fill: {"color": "002060"}',
        'Color Standards (font color, NOT cell fill) -- rubric convention: blue=inputs, black=formulas, green=cross-sheet links, red=external links:\n'
        '- Input cells (hardcoded values): font: {"color": "0000FF"}\n'
        '- Formula cells: Black font (default, no action needed)\n'
        '- Cross-sheet link formulas: font: {"color": "008000"}\n'
        '- External links are forbidden (rubric: Potential Dangers) -- there should be none to color red\n'
        '- Header rows: font: {"color": "FFFFFF", "bold": true}, fill: {"color": "002060"}',
    )

    # The v11 body's deliverable-structure advice told the model to create
    # separate per-question answer sheets — the opposite of the ANSWERS
    # convention above. Rewrite it to defer to the 'Questions' sheet.
    out = swap(
        out,
        '2. MATCH EXPECTED DELIVERABLE STRUCTURE:\n'
        '   - Read PDF questions/materials to understand expected format\n'
        '   - If questions are "Q1, Q2, Q3, Q4", create SEPARATE sheets for each\n'
        '   - If case expects Cover sheet, create it\n'
        '   - Do not assume format - follow case requirements exactly\n'
        '\n'
        '   Example: If questions are Q1-Q4, create separate answer sheets for each question\n'
        '   rather than putting all answers in a single sheet.',
        '2. MATCH EXPECTED DELIVERABLE STRUCTURE:\n'
        '   - Read PDF questions/materials to understand expected format\n'
        "   - Answer the case's questions in the starting workbook's 'Questions' sheet, "
        "in its reserved 'Answers'-column cells - see the ANSWERS convention above; "
        'do NOT create separate per-question answer sheets\n'
        '   - If case expects Cover sheet, create it\n'
        '   - Do not assume format - follow case requirements exactly\n'
        '\n'
        "   Example: If questions are Q1-Q4, their answers belong in the 'Questions' "
        'sheet\'s reserved answer cells (one row per question), each entered as a live '
        'formula referencing the model.',
    )

    if re.search(r"rubric criteri", out):
        raise SystemExit("v1 criterion-number references survived the translation")
    return out


def build_task_template() -> str:
    fmwc_v5 = (PROMPTS / "task_template_fmwc_v5.txt").read_text()
    step1 = (PROMPTS_V3 / "step1_analyze.txt").read_text()
    step3 = (PROMPTS_V3 / "step3_qa.txt").read_text()

    # v5's harness-critical blocks (filename requirement, circular-reference
    # prevention, formula tool usage) survive verbatim bar the tab example,
    # which now also names the copy_file route that preserves 'Questions'.
    harness = fmwc_v5[: fmwc_v5.index("STEP 1 - READ ALL ATTACHED FILES:")]
    harness = swap(
        harness,
        'Example: create_file(filename="solution.xlsx", worksheets=["Assumptions", "Workings", "Q1", ...])',
        'Example: create_file(filename="solution.xlsx", worksheets=["Cover", "Contents", "Assumptions", "Calculations", "Summary", "Error Checks", ...])\n'
        "Since the deliverable must preserve the starting workbook's 'Questions' sheet, prefer copy_file(source, \"solution.xlsx\") on the starting workbook and build in the copy.",
    )

    # Step 1's analysis points, byte-lifted from step1_analyze.txt (item 2
    # now points the plan at the 'Questions' sheet).
    plan_start = step1.index("1. Task type")
    plan_end = step1.index("\n\nDo not start building")
    plan_items = step1[plan_start:plan_end]

    # Step 3's QA items, byte-lifted from step3_qa.txt (now eight items —
    # item 8 is the Questions-sheet check).
    qa_start = step3.index("1. Zero visible Excel errors")
    qa_end = step3.index("\n\nFix everything")
    qa_items = step3[qa_start:qa_end]

    return (
        harness
        + "You are an expert financial-modeling agent building an Excel solution for the attached case. Work in THREE steps, completing all of them in this session.\n"
        + "\n"
        + "Your only allowed method for solving this is the Excel workbook itself: build every calculation as a live Excel formula in solution.xlsx. Never compute a result outside Excel and enter it as a constant - the workbook must perform all calculations.\n"
        + "\n"
        + "STEP 1 - ANALYZE & PLAN (do not build yet):\n"
        + "Read ALL files in the ATTACHED PDFs and ATTACHED EXCEL CONTEXT sections above, then produce a brief written analysis on a new sheet named 'Summary', covering:\n"
        + plan_items
        + "\n\n"
        + "STEP 2 - BUILD THE MODEL:\n"
        + "Build the complete Excel model in solution.xlsx, using live Excel formulas for every calculated value. You will be graded against the EVALUATION RUBRIC in your system instructions (12 weighted categories, 132 checks) - build so that every check meets its \"Good\" standard and avoids its \"Bad\" standard.\n"
        + "The deliverable must also preserve and answer the starting workbook's 'Questions' sheet - see the ANSWERS convention in your system instructions.\n"
        + "If the starting .xlsx file already contains sheets that need modification to satisfy the rubric, modify those original sheets directly - every sheet in the final workbook will be graded, including sheets that came with the starting file.\n"
        + "\n"
        + "STEP 3 - QA AND DELIVER:\n"
        + "Audit the workbook against the rubric's Error Checks and Potential Dangers categories and fix every issue:\n"
        + qa_items
        + "\n\n"
        + "Fix everything you find and confirm the master \"Model OK\" flag is green. The deliverable is the solution.xlsx workbook itself - it must contain the completed model, not a text description of it.\n"
    )


def main() -> int:
    sys_prompt = build_system_prompt()
    template = build_task_template()
    SYS_OUT.write_text(sys_prompt)
    TPL_OUT.write_text(template)

    rubric = sys_prompt[sys_prompt.index(RUBRIC_MARKER):]
    rubric = rubric[: rubric.index("--- AVAILABLE TOOLS ---")].rstrip("\n")
    n_checks = len(re.findall(r"\n\s*Good:", rubric))
    print(f"Wrote {SYS_OUT.name}: {len(sys_prompt)} chars "
          f"(rubric section {len(rubric)} chars, {n_checks} Good-standards)")
    print(f"Wrote {TPL_OUT.name}: {len(template)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
