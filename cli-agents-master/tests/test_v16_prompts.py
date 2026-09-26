"""Offline checks for prompt set v16: the v15 tool manual made consistent with
the attached House Standards (2026-09-19).

Stdlib-only. Run:  python tests/test_v16_prompts.py
"""
import difflib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prompt_versions", ROOT / "excel_cli_agent" / "prompt_versions.py"
)
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)
PROMPTS_DIR = _pv.PROMPTS_DIR
STANDARDS = ROOT.parent / "house_standards" / "House_Standards_v1.md"

# What the manual used to say against the standards.
CONTRADICTIONS = ("Convention A", "Convention B", "POSITIVE numbers in their rows", "NOT cell fill",
                  "0.00%", "$#,##0_);($#,##0)", "Calibri", "\"size\": 12", "\"size\": 11")


def main() -> int:
    v15, v16 = _pv.PROMPT_VERSIONS["v15"], _pv.PROMPT_VERSIONS["v16"]
    sys_path, tpl_path = PROMPTS_DIR / v16["system"], PROMPTS_DIR / v16["fmwc"]
    assert sys_path.exists() and tpl_path.exists()
    assert v16["fmwc"] == v16["wsp"] == v15["fmwc"] == "task_template_shared_v9.txt"
    assert _pv.parse_prompt_version(sys_path, tpl_path) == 1609
    assert _pv.attachments_for("v16") == _pv.attachments_for("v15") == ["house_standards/House_Standards_v1.md"]
    assert _pv.attachment_names_for("v16") == {"House_Standards_v1.md": "HOUSE_STANDARDS.md"}
    assert _pv.DEFAULT_V2_PROMPT_VERSION == "v16"
    assert _pv.rubric_for_prompt_version("v16") == "v2"
    print("OK  v16 registered, prompt_version 1609, same template and attachment as v15, v2 default")

    text = sys_path.read_text()
    for needle in CONTRADICTIONS:
        assert needle not in text, f"contradicting passage survived: {needle!r}"
    assert ("Where the modelling or formatting guidance in these system instructions conflicts with it, "
            "the house standards govern; the tool limits described here still apply.") in text
    print("OK  no passage contradicting the House Standards; precedence sentence present")

    # The standards' values are read from the attachment, never restated in the manual.
    standards = STANDARDS.read_text()
    assert "Arial 10" in standards and "Costs and outflows negative" in standards
    for value in ("Arial", "pale blue", "outflows negative", "Zeros as dashes"):
        assert value.lower() not in text.lower(), f"manual restates a House Standards value: {value!r}"
    print("OK  House Standards values are not restated in the manual")

    # Minimal: the five standards hunks plus one tool-mechanics line, nothing else.
    v15_text = (PROMPTS_DIR / v15["system"]).read_text()
    assert CONTRADICTIONS[0] in v15_text, "v15 must stay as recorded (append-only)"
    sm = difflib.SequenceMatcher(None, v15_text.splitlines(), text.splitlines(), autojunk=False)
    hunks = [op for op in sm.get_opcodes() if op[0] != "equal"]
    assert len(hunks) == 6, hunks
    removed = sum(i2 - i1 for _, i1, i2, _, _ in hunks)
    added = sum(j2 - j1 for _, _, _, j1, j2 in hunks)
    assert (removed, added) == (23, 9), (removed, added)
    for needle in ("ANSWERS (the 'Questions' sheet)", "--- AVAILABLE TOOLS ---", "set_cell_formula",
                   "format_cells(filename: str", "number_format: \"YYYY-MM-DD\"",
                   "NEVER mix: positive CapEx on one sheet, negative on another",
                   "blue=inputs, black=formulas, green=cross-sheet links, red=external links",
                   "RESPONSE FORMAT (STRICT JSON - NO COMMENTS)"):
        assert needle in text, needle
    assert text.count("HOUSE_STANDARDS.md") == 1
    print(f"OK  6 hunks vs v15 ({removed} lines out, {added} in); tool and task mechanics intact")
    assert "with the file format's _xlfn. prefix" in text

    # The committed file is what the generator produces.
    rc = subprocess.run([sys.executable, str(ROOT / "tools" / "build_v16_prompts.py"), "--check"],
                        capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    print("OK  system_prompt_v16.txt matches tools/build_v16_prompts.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
