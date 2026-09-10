"""Offline checks for the rubric-scrubbed House-Standards prompt set (v15).

Stdlib-only. Run:  python tests/test_v15_prompts.py
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prompt_versions", ROOT / "excel_cli_agent" / "prompt_versions.py"
)
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)
PROMPTS_DIR = _pv.PROMPT_VERSIONS and _pv.PROMPTS_DIR

CODING_V11 = ROOT.parent / "coding-agents-master" / "coding_agent" / "prompts" / "task_template_shared_v11.txt"
SCRUB_PATTERNS = (r"== FULL RUBRIC", r"\bGood:", r"\brubric\b", r"\bgraded\b", r"\bgrading\b",
                  r"Category weights", r"professional-services firm", r"House_Standards_v1")


def main() -> int:
    v15 = _pv.PROMPT_VERSIONS["v15"]
    assert v15["fmwc"] == v15["wsp"] == "task_template_shared_v9.txt"
    sys_path, tpl_path = PROMPTS_DIR / v15["system"], PROMPTS_DIR / v15["fmwc"]
    assert sys_path.exists() and tpl_path.exists()
    assert _pv.parse_prompt_version(sys_path, tpl_path) == 1509
    assert _pv.attachments_for("v15") == ["house_standards/House_Standards_v1.md"]
    assert _pv.attachment_names_for("v15") == {"House_Standards_v1.md": "HOUSE_STANDARDS.md"}
    assert _pv.DEFAULT_V2_PROMPT_VERSION == "v15"
    assert _pv.rubric_for_prompt_version("v15") == "v2"
    print("OK  v15 registered, prompt_version 1509, HOUSE_STANDARDS.md delivered name, v2 default")

    sys_text, tpl_text = sys_path.read_text(), tpl_path.read_text()
    for text, name in ((sys_text, "system"), (tpl_text, "template")):
        for pat in SCRUB_PATTERNS:
            assert not re.search(pat, text, re.IGNORECASE), f"{name}: {pat!r} survived the scrub"
    print("OK  no rubric material, weights, conventions summary or versioned attachment name in v15")

    # What must survive: the ANSWERS mechanics (the judge's harness answer
    # check depends on them), the live-formula rule, the tool guidance, and
    # the house-standards pointer wording shared with coding template v11.
    for needle in ("ANSWERS (the 'Questions' sheet)", "copy_file", "--- AVAILABLE TOOLS ---",
                   "MODIFYING ORIGINAL STARTING SHEETS", "HOUSE STANDARDS\n- The workspace contains HOUSE_STANDARDS.md",
                   "Where the case's own instructions conflict with it, the case instructions govern."):
        assert needle in sys_text, needle
    for needle in ("STEP 1 - ANALYZE & PLAN", "STEP 2 - BUILD THE MODEL", "STEP 3 - VERIFY AND DELIVER",
                   "Read HOUSE_STANDARDS.md", "Conform to HOUSE_STANDARDS.md throughout",
                   "3. Confirm the workbook conforms to HOUSE_STANDARDS.md.",
                   "solution.xlsx", "set_cell_formula"):
        assert needle in tpl_text, needle
    assert sys_text.count("HOUSE_STANDARDS.md") == 1 and tpl_text.count("HOUSE_STANDARDS.md") == 3
    # The Questions-sheet verify line is byte-identical to coding v11's item 2.
    coding = CODING_V11.read_text()
    q_line = next(l for l in coding.splitlines() if l.startswith("2. Confirm every 'Questions' sheet is intact"))
    assert q_line in tpl_text
    print("OK  task mechanics, tool guidance and the coding-v11 house-standards wording survive")

    # v14 untouched by the scrub (append-only).
    v14_sys = (PROMPTS_DIR / "system_prompt_v14.txt").read_text()
    assert "== FULL RUBRIC" in v14_sys and len(v14_sys) > len(sys_text) * 2
    print("OK  v14 still carries the rubric; v15 is the scrubbed successor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
