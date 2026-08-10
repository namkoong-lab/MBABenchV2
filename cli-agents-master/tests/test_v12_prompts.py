"""Offline checks for the benchmark-v2 prompt set (prompt_version v12).

Stdlib-only — verifies the generated files without importing the heavy
runner stack. Run:  python tests/test_v12_prompts.py
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
STEP2 = ROOT.parent / "gui-agents-master" / "tasks_configs" / "prompts_v2" / "step2_build.txt"


def main() -> int:
    # Registry: v12 exists, both template slots share one file, pv = 1206.
    v12 = PROMPT_VERSIONS["v12"]
    assert v12["fmwc"] == v12["wsp"] == "task_template_shared_v6.txt"
    sys_path = PROMPTS_DIR / v12["system"]
    tpl_path = PROMPTS_DIR / v12["fmwc"]
    assert sys_path.exists() and tpl_path.exists()
    assert parse_prompt_version(sys_path, tpl_path) == 1206
    print("OK  v12 registered, shared template, prompt_version 1206")

    # Rubric pairing helper.
    assert rubric_for_prompt_version("v12") == "v2"
    assert rubric_for_prompt_version("v11") == "v1"
    assert rubric_for_prompt_version("v10") == "v1"
    print("OK  rubric_for_prompt_version: v12 -> v2, v10/v11 -> v1")

    # The rubric section is byte-exact against the GUI source of truth.
    sys_text = sys_path.read_text()
    rubric = sys_text[sys_text.index(RUBRIC_MARKER):]
    rubric = rubric[: rubric.index("--- AVAILABLE TOOLS ---")].rstrip("\n")
    step2 = STEP2.read_text()
    expected = step2[step2.index(RUBRIC_MARKER):].rstrip("\n")
    assert rubric == expected, "v12 rubric section drifted from prompts_v2/step2_build.txt"
    n_checks = len(re.findall(r"\n\s*Good:", rubric))
    assert n_checks == 132, f"expected 132 checks, found {n_checks}"
    print(f"OK  rubric byte-exact vs step2_build.txt ({len(rubric)} chars, 132 checks)")

    # No v1 leftovers in the system prompt.
    assert "17 criteria across 3 categories" not in sys_text
    assert not re.search(r"rubric criteri", sys_text), "v1 criterion-number labels survived"
    assert "12 weighted categories, 132 checks" in sys_text
    print("OK  no v1 rubric leftovers; v2 grading intro present")

    # Task template: harness blocks + the three-step v2 flow.
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
    ]:
        assert needle in tpl, f"template missing: {needle}"
    print("OK  shared v6 template carries harness blocks + 3-step flow")

    print("ALL V12 PROMPT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
