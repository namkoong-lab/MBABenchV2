"""Offline checks for the benchmark-v2 House-Standards prompt set (v14).

Stdlib-only — verifies the generated files without importing the heavy
runner stack. Run:  python tests/test_v14_prompts.py
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
GUI = ROOT.parent / "gui-agents-master" / "tasks_configs"
STEP2_V4 = GUI / "prompts_v4" / "step2_build.txt"
STEP2_V3 = GUI / "prompts_v3" / "step2_build.txt"
STEP3_V4 = GUI / "prompts_v4" / "step3_qa.txt"
STANDARDS = ROOT.parent / "house_standards" / "House_Standards_v1.md"


def main() -> int:
    # Registry: v14 exists, both template slots share one file, pv = 1408,
    # and the version (not a batch config) declares the attachment.
    v14 = PROMPT_VERSIONS["v14"]
    assert v14["fmwc"] == v14["wsp"] == "task_template_shared_v8.txt"
    sys_path = PROMPTS_DIR / v14["system"]
    tpl_path = PROMPTS_DIR / v14["fmwc"]
    assert sys_path.exists() and tpl_path.exists()
    assert parse_prompt_version(sys_path, tpl_path) == 1408
    assert _pv.attachments_for("v14") == ["house_standards/House_Standards_v1.md"]
    assert _pv.attachments_for("v13") == []
    assert _pv.DEFAULT_V2_PROMPT_VERSION == "v15"  # v14 superseded by the scrubbed v15
    assert STANDARDS.is_file(), "house_standards/House_Standards_v1.md missing from the monorepo"
    print("OK  v14 registered, shared template, prompt_version 1408, attachment declared")

    # Rubric pairing helper.
    assert rubric_for_prompt_version("v14") == "v2"
    assert rubric_for_prompt_version("v13") == "v2"
    assert rubric_for_prompt_version("v11") == "v1"
    print("OK  rubric_for_prompt_version: v13/v14 -> v2, v11 -> v1")

    # The rubric section is byte-exact against the GUI v4 source of truth,
    # which is itself byte-identical to v3 from the marker on — so v14
    # scores stay comparable with v13 and any delta is the standards alone.
    sys_text = sys_path.read_text()
    rubric = sys_text[sys_text.index(RUBRIC_MARKER):]
    rubric = rubric[: rubric.index("--- AVAILABLE TOOLS ---")].rstrip("\n")
    step2 = STEP2_V4.read_text()
    expected = step2[step2.index(RUBRIC_MARKER):].rstrip("\n")
    assert rubric == expected, "v14 rubric section drifted from prompts_v4/step2_build.txt"
    step2_v3 = STEP2_V3.read_text()
    assert expected == step2_v3[step2_v3.index(RUBRIC_MARKER):].rstrip("\n"), (
        "prompts_v4 rubric differs from prompts_v3 — the house-standards "
        "revision was supposed to leave the rubric body untouched"
    )
    n_checks = len(re.findall(r"\n\s*Good:", rubric))
    assert n_checks == 132, f"expected 132 checks, found {n_checks}"
    print(f"OK  rubric byte-exact vs prompts_v4 (== prompts_v3); {len(rubric)} chars, 132 checks")

    # The house-standards directive, in the CLI phrasing: the file is
    # provided with the task files and embedded in the context (the agent
    # has no .md reader), with the GUI's precedence rule kept verbatim.
    assert "HOUSE STANDARDS\n- " in sys_text
    assert "provided with the task files as House_Standards_v1.md" in sys_text
    assert "full text is included below in your context under the HOUSE STANDARDS header" in sys_text
    assert "is attached with the case files" not in sys_text, "chat-upload wording survived the CLI translation"
    assert ("Where the case instructions or this prompt (including the rubric) conflict with the "
            "house standards, the case instructions and this prompt govern.") in sys_text
    assert "Note any deliberate departure from the house standards on the cover, with the reason." in sys_text
    print("OK  HOUSE STANDARDS directive present with CLI provided-and-embedded phrasing")

    # LET is whitelisted from v14 on, so the GUI's formula guidance is kept
    # verbatim instead of the v12/v13 "LET is not supported" rewrite.
    assert "prefer XLOOKUP / IFS / LET." in sys_text
    assert "LET is not supported" not in sys_text
    print("OK  GUI LET guidance kept verbatim (validator whitelists LET/XMATCH)")

    # Everything v13 carried is still there.
    assert "ANSWERS (the 'Questions' sheet)" in sys_text
    assert "copying the starting workbook (copy_file)" in sys_text
    assert "If you build the model in a new workbook" not in sys_text
    assert "do NOT create separate per-question answer sheets" in sys_text
    assert "17 criteria across 3 categories" not in sys_text
    assert not re.search(r"rubric criteri", sys_text), "v1 criterion-number labels survived"
    assert "12 weighted categories, 132 checks" in sys_text
    print("OK  v13 content (ANSWERS convention, no v1 leftovers) inherited")

    # Task template: v7's blocks + step1's read-the-standards sentence +
    # step3's QA item 9 (byte-lifted from the GUI source).
    tpl = tpl_path.read_text()
    step3 = STEP3_V4.read_text()
    item9 = step3[step3.index("9. The workbook follows House_Standards_v1.md"):step3.index("\n\nFix everything")]
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
        "provided with the task files as House_Standards_v1.md",
        "let your build plan (item 4) follow its structure conventions",
        item9,
    ]:
        assert needle in tpl, f"template missing: {needle}"
    assert "is attached with the case files" not in tpl
    print("OK  shared v8 template carries v7 blocks + standards sentence + QA item 9")

    # Frozen predecessors untouched by the v14 build (append-only registry).
    v13 = PROMPT_VERSIONS["v13"]
    assert v13["system"] == "system_prompt_v13.txt" and v13["fmwc"] == "task_template_shared_v7.txt"
    assert "attachments" not in v13
    print("OK  v13 registry entry unchanged")

    print("ALL V14 PROMPT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
