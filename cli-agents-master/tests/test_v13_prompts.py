"""Offline checks for the benchmark-v2 Questions-sheet prompt set (v13).

Stdlib-only — verifies the generated files without importing the heavy
runner stack. Run:  python tests/test_v13_prompts.py
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Load prompt_versions.py directly — the excel_cli_agent package __init__
# imports the full runner stack (openai, ...), which this test doesn't need.
_spec = importlib.util.spec_from_file_location(
    "prompt_versions", ROOT / "excel_cli_agent" / "prompt_versions.py"
)
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)
PROMPTS_DIR = _pv.PROMPTS_DIR
PROMPT_VERSIONS = _pv.PROMPT_VERSIONS
parse_prompt_version = _pv.parse_prompt_version
rubric_for_prompt_version = _pv.rubric_for_prompt_version

RUBRIC_MARKER = "== FULL RUBRIC"
STEP2 = ROOT.parent / "gui-agents-master" / "tasks_configs" / "prompts_v3" / "step2_build.txt"
STEP2_V2 = ROOT.parent / "gui-agents-master" / "tasks_configs" / "prompts_v2" / "step2_build.txt"


def main() -> int:
    # Registry: v13 exists, both template slots share one file, pv = 1307.
    v13 = PROMPT_VERSIONS["v13"]
    assert v13["fmwc"] == v13["wsp"] == "task_template_shared_v7.txt"
    sys_path = PROMPTS_DIR / v13["system"]
    tpl_path = PROMPTS_DIR / v13["fmwc"]
    assert sys_path.exists() and tpl_path.exists()
    assert parse_prompt_version(sys_path, tpl_path) == 1307
    print("OK  v13 registered, shared template, prompt_version 1307")

    # Rubric pairing helper.
    assert rubric_for_prompt_version("v13") == "v2"
    assert rubric_for_prompt_version("v12") == "v2"
    assert rubric_for_prompt_version("v11") == "v1"
    print("OK  rubric_for_prompt_version: v12/v13 -> v2, v11 -> v1")

    # The rubric section is byte-exact against the GUI v3 source of truth.
    # Since the 2026-08 rubric revision (in-place text update from the
    # canonical checklist xlsx) prompts_v3 deliberately DIFFERS from the
    # frozen prompts_v2 rubric — assert both directions.
    sys_text = sys_path.read_text()
    rubric = sys_text[sys_text.index(RUBRIC_MARKER):]
    rubric = rubric[: rubric.index("--- AVAILABLE TOOLS ---")].rstrip("\n")
    step2 = STEP2.read_text()
    expected = step2[step2.index(RUBRIC_MARKER):].rstrip("\n")
    assert rubric == expected, "v13 rubric section drifted from prompts_v3/step2_build.txt"
    step2_v2 = STEP2_V2.read_text()
    assert expected != step2_v2[step2_v2.index(RUBRIC_MARKER):].rstrip("\n"), (
        "prompts_v3 rubric matches frozen prompts_v2 — the 2026-08 revision "
        "text is missing (rerun judge/operation_scripts/build_rubric_9_from_xlsx.py "
        "and tools/build_v13_prompts.py)"
    )
    n_checks = len(re.findall(r"\n\s*Good:", rubric))
    assert n_checks == 132, f"expected 132 checks, found {n_checks}"
    print(f"OK  rubric byte-exact vs prompts_v3; 2026-08 revision differs from frozen prompts_v2 ({len(rubric)} chars, 132 checks)")

    # The Questions-sheet convention made it into the system prompt, with
    # the CLI copy_file translation applied.
    assert "ANSWERS (the 'Questions' sheet)" in sys_text
    assert "copying the starting workbook (copy_file)" in sys_text
    assert "If you build the model in a new workbook" not in sys_text, (
        "chat-flow carry-over wording survived the CLI translation"
    )
    print("OK  ANSWERS convention present with CLI copy_file translation")

    # The v11 body's separate-answer-sheets advice was rewritten to defer to
    # the 'Questions' sheet — nothing may contradict the ANSWERS convention.
    assert "create SEPARATE sheets for each" not in sys_text
    assert "create separate answer sheets" not in sys_text
    assert "do NOT create separate per-question answer sheets" in sys_text
    print("OK  deliverable-structure advice defers to the 'Questions' sheet")

    # No v1 leftovers in the system prompt.
    assert "17 criteria across 3 categories" not in sys_text
    assert not re.search(r"rubric criteri", sys_text), "v1 criterion-number labels survived"
    assert "12 weighted categories, 132 checks" in sys_text
    print("OK  no v1 rubric leftovers; v2 grading intro present")

    # Task template: harness blocks + the three-step flow + Questions items.
    tpl = tpl_path.read_text()
    for needle in [
        'named exactly "solution.xlsx"',
        "CRITICAL CIRCULAR REFERENCE PREVENTION:",
        "FORMULA TOOL USAGE (CRITICAL):",
        "STEP 1 - ANALYZE & PLAN",
        "STEP 2 - BUILD THE MODEL:",
        "STEP 3 - QA AND DELIVER:",
        "Error Checks and Potential Dangers",
        '"Model OK" flag',
        "The workbook's 'Questions' sheet lists these questions in column A",
        "8. The 'Questions' sheet is intact",
        "preserve and answer the starting workbook's 'Questions' sheet",
    ]:
        assert needle in tpl, f"template missing: {needle}"
    print("OK  shared v7 template carries harness blocks + 3-step flow + Questions items")

    print("ALL V13 PROMPT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
