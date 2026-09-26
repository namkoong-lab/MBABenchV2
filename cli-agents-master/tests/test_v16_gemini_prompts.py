"""Offline checks for system_prompt_v16_gemini-3.8-flash.txt: the v16 system
prompt with a function-call response contract, used by Gemini 3.8 Flash alone
(2026-09-22). It is a variant of v16, not a prompt set: prompt_version stays
1609 and no other model may ever be handed it.

Stdlib-only. Run:  python tests/test_v16_gemini_prompts.py
"""
import difflib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pv = _load("prompt_versions", "excel_cli_agent/prompt_versions.py")
_mc = _load("models_config", "excel_cli_agent/models_config.py")
PROMPTS_DIR = _pv.PROMPTS_DIR
GEMINI = "tensorblock/gemini-3.8-flash"
VARIANT = "system_prompt_v16_gemini-3.8-flash.txt"


def main() -> int:
    v16 = _pv.PROMPT_VERSIONS["v16"]
    base, variant, tpl = PROMPTS_DIR / v16["system"], PROMPTS_DIR / VARIANT, PROMPTS_DIR / v16["fmwc"]
    assert variant.exists()

    # Only that one model, only under v16, gets the variant; v16 itself is untouched.
    assert _pv.MODEL_SYSTEM_PROMPT_VARIANTS == {("v16", GEMINI): VARIANT}
    assert _pv.system_prompt_file("v16", GEMINI) == VARIANT
    for model in (None, "", "tensorblock/grok-4.6", "google/gemini-3.8-flash", "gpt-6-astra"):
        assert _pv.system_prompt_file("v16", model) == v16["system"], model
    assert _pv.system_prompt_file("v15", GEMINI) == _pv.PROMPT_VERSIONS["v15"]["system"]
    # The runners pass require_variant for the native model: a set with no
    # variant refuses rather than handing it the JSON contract.
    try:
        _pv.system_prompt_file("v15", GEMINI, require_variant=True)
    except ValueError as e:
        assert "v16" in str(e) and GEMINI in str(e)
    else:
        raise AssertionError("v15 has no Gemini variant and must refuse")
    assert _pv.system_prompt_file("v15", "tensorblock/grok-4.6", require_variant=False) == _pv.PROMPT_VERSIONS["v15"]["system"]
    assert v16["system"] == "system_prompt_v16.txt" and _pv.DEFAULT_V2_PROMPT_VERSION == "v16"
    assert _mc.GEMINI_TOOL_CALL_MODELS == frozenset({GEMINI})
    assert _mc.uses_gemini_tool_calls(GEMINI) and not _mc.uses_gemini_tool_calls("tensorblock/gemini-3.8-pro")
    print("OK  the variant is registered for Gemini 3.8 Flash under v16 only; the executor gate names the same model")

    # Recorded as the set's version, 1609, like every other v16 row.
    assert _pv.parse_prompt_version(variant, tpl) == 1609 == _pv.parse_prompt_version(base, tpl)
    print("OK  prompt_version 1609 for the variant")

    text, base_text = variant.read_text(), base.read_text()
    for gone in ("is_complete", "STRICT JSON", '"actions"', '"action"', "Return ONLY valid JSON", "completion_summary\""):
        assert gone not in text, f"JSON contract survived: {gone!r}"
    for present in ("RESPONSE FORMAT (FUNCTION CALLS - NO JSON, NO PROSE)", "complete_task",
                    "THE WORKBOOK IS ALREADY IN FRONT OF YOU - BUILD, DO NOT RE-READ",
                    "YOU HAVE ONLY K ITERATIONS - DO AS MUCH AS POSSIBLE IN EACH", 'The message states "ITERATION n/K"',
                    "Put EVERY call the step needs into ONE reply", "execute SEQUENTIALLY",
                    "Do not combine it with other\n  calls"):
        assert present in text, present
    assert "HOUSE_STANDARDS.md" in text and text.count("HOUSE_STANDARDS.md") == base_text.count("HOUSE_STANDARDS.md")
    print("OK  function-call contract in, JSON contract out, House Standards pointer unchanged")

    # Minimal: the response-format block plus the six one-line wording edits.
    sm = difflib.SequenceMatcher(None, base_text.splitlines(), text.splitlines(), autojunk=False)
    hunks = [op for op in sm.get_opcodes() if op[0] != "equal"]
    block_start = base_text.splitlines().index("RESPONSE FORMAT (STRICT JSON - NO COMMENTS):")
    wording = [h for h in hunks if h[1] < block_start]
    assert len(wording) == 6 and all(h[0] == "replace" and h[2] - h[1] == 1 and h[4] - h[3] == 1 for h in wording), wording
    assert all(h[1] >= block_start for h in hunks[len(wording):]), hunks
    print("OK  six one-line wording edits plus the response-format block, nothing else")

    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_v16_gemini_prompts.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    print("OK  generator reproduces the file byte for byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
