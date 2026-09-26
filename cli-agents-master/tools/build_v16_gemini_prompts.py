#!/usr/bin/env python3
"""Generate system_prompt_v16_gemini-3.8-flash.txt: the v16 system prompt with
its response contract changed from a JSON actions reply to native function
calls. It is a VARIANT of v16 for one model (prompt_versions.py
MODEL_SYSTEM_PROMPT_VARIANTS), not a new prompt set: the rubric-free manual,
the House Standards pointer and the task template (v9) are v16's, and an
attempt run with it records prompt_version 1609 like every other v16 row,
with the file name in extra_configs.system_prompt_file.

Why (2026-09-22): Gemini 3.8 Flash through TensorBlock Forge answers the
agent prompt with a native function call instead of the JSON text every other
model writes. Declaring the Excel tools as functions keeps those calls intact
(9187af2), but against v16's JSON contract the model issues ONE call per step
- 40 iterations, 40 cell operations - while, asked for function calls outright
with no JSON contract in the way, it batches every operation of a step (7 of 7
in a probe). Probes: models_config.GEMINI_TOOL_CALL_MODELS. Only the executor
path for that model reads native calls (task_executor._uses_gemini_tool_calls)
and only that model is ever given this file (prompt_versions.system_prompt_file).

Minimal edits, nothing else touched:
  1. Intro (lines 4 and 9): "is_complete=true with completion_summary" ->
     "the complete_task function".
  2. VALIDATION BEFORE COMPLETING / TASK COMPLETION VALIDATION: "setting
     is_complete=true" -> "calling complete_task" (three places).
  3. PHASE 2 batching line: "in the same batch" -> "in the same reply".
  4. RESPONSE FORMAT (STRICT JSON) block, up to the closing "If the task is
     complete..." line: replaced by (a) a "build, do not re-read" passage -
     in every local fresh-context run the model spent all its iterations
     re-reading sheets the grid dump already showed it (list_files x11,
     Assumptions x10 in 40 iterations) while every other model built from
     the dump - plus an "only K iterations" passage tying the ITERATION
     n/K line of every message to how much each reply must carry - and (b)
     the FUNCTION-CALL contract - one function call per
     Excel operation, every call of the step in one reply, in order,
     stopping at the first failure; complete_task(completion_summary) to
     finish; no JSON, no prose.

Deterministic; run from cli-agents-master/ and diff.
tests/test_v16_gemini_prompts.py pins the result.

    uv run python tools/build_v16_gemini_prompts.py [--check]
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "excel_cli_agent" / "prompts"
SYS_IN, SYS_OUT = PROMPTS / "system_prompt_v16.txt", PROMPTS / "system_prompt_v16_gemini-3.8-flash.txt"

JSON_CONTRACT_START = "RESPONSE FORMAT (STRICT JSON - NO COMMENTS):\n"
JSON_CONTRACT_END = "If the task is complete, set is_complete to true and provide a completion_summary.\n"

FUNCTION_CONTRACT = """THE WORKBOOK IS ALREADY IN FRONT OF YOU - BUILD, DO NOT RE-READ:
Your message shows the current contents of every worksheet in solution.xlsx (and the source workbook)
cell by cell - values and formulas - under the "=== SOLUTION FILE" / "=== WORKSHEET" headings. That IS
the model's state. Do not spend a reply on list_files, list_worksheets, get_cell_range or
get_file_metadata to look at what is already shown: those calls return nothing you do not have.

YOU HAVE ONLY K ITERATIONS - DO AS MUCH AS POSSIBLE IN EACH:
The message states "ITERATION n/K". K is the total number of replies you get for the whole task; when
it runs out the workbook is graded as it stands, finished or not. One tool call per reply therefore
means an unfinished model. Every reply must carry as much of the build as you can write - dozens of
calls are normal: create every sheet AND write its labels, inputs and formulas in the same reply;
fill a whole cashflow table in one reply; write the Summary analysis together with the sheets it
plans. Once solution.xlsx exists, the very next reply builds - the Summary analysis, then the working
sheets, cells and formulas - and each reply after it finishes as much of what remains as possible.
Read only a cell whose computed value you cannot see, and only after building.

RESPONSE FORMAT (FUNCTION CALLS - NO JSON, NO PROSE):

You act ONLY through the declared functions. Every Excel operation is one function call with the
tool's own parameters; the JSON text formats other agents use do NOT apply to you.

For QUESTIONS or direct answers (no Excel operations needed):
- Call complete_task with the complete answer in completion_summary.

For Excel OPERATIONS:
- Put EVERY call the step needs into ONE reply, in the order they must run - creating a sheet, then
  filling it, then its formulas; a whole block of set_cell_formula calls at once. One call per reply
  wastes an iteration: you have a fixed number of them.
- The calls of a reply execute SEQUENTIALLY in the order given. If ANY call fails, execution STOPS
  and the remaining calls are SKIPPED; you receive the results of all of them - successes, the
  failure, and the skipped ones - in the next step.
- Do not narrate between calls and do not write text instead of a call: a reply with no function
  call does nothing.

When the task is COMPLETE (all requirements above met):
- Call complete_task with a completion_summary of the Excel work done. Do not combine it with other
  calls: finish the operations in one reply, then complete in the next.
"""

EDITS = (
    # 1. intro
    (
        "1. Answer questions directly (use is_complete=true with completion_summary)\n",
        "1. Answer questions directly (call the complete_task function with the answer as completion_summary)\n",
    ),
    (
        "- Answer directly using is_complete=true and put the answer in completion_summary\n",
        "- Answer directly by calling complete_task with the answer as completion_summary\n",
    ),
    # 2. completion checks
    (
        "VALIDATION BEFORE COMPLETING (MANDATORY):\nBefore setting is_complete=true, check:\n",
        "VALIDATION BEFORE COMPLETING (MANDATORY):\nBefore calling complete_task, check:\n",
    ),
    (
        "REQUIRED before is_complete=true:\n",
        "REQUIRED before calling complete_task:\n",
    ),
    (
        "Before setting is_complete=true, ask yourself:\n",
        "Before calling complete_task, ask yourself:\n",
    ),
    # 3. batching wording
    (
        "Do NOT include formatting actions in the same batch as calculation actions.\n",
        "Do NOT include formatting calls in the same reply as calculation calls.\n",
    ),
)

# The JSON contract may not survive anywhere in the variant.
GONE = ("is_complete", "STRICT JSON", '"actions"', '"action"', "Return ONLY valid JSON")


def swap(text: str, old: str, new: str) -> str:
    """Exact-match replace that refuses to silently no-op or double-fire."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"expected exactly 1 occurrence, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def build_system_prompt(v16: str) -> str:
    t = v16
    for old, new in EDITS:
        t = swap(t, old, new)
    # 4. the response contract block, replaced whole
    start, end = t.index(JSON_CONTRACT_START), t.index(JSON_CONTRACT_END) + len(JSON_CONTRACT_END)
    if t.count(JSON_CONTRACT_START) != 1 or t.count(JSON_CONTRACT_END) != 1 or end <= start:
        raise SystemExit("JSON response-format block not found exactly once")
    t = t[:start] + FUNCTION_CONTRACT + t[end:]
    for needle in GONE:
        if needle in t:
            raise SystemExit(f"JSON contract survived: {needle!r}")
    return t


def main() -> int:
    check = "--check" in sys.argv
    text = build_system_prompt(SYS_IN.read_text())
    if check:
        if not SYS_OUT.exists() or SYS_OUT.read_text() != text:
            print(f"DRIFT: {SYS_OUT.name} differs from the generated text"); return 1
    else:
        SYS_OUT.write_text(text)
    print(f"{SYS_OUT.name}: {len(text):,} chars md5 {hashlib.md5(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
