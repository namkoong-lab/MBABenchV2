"""A tool's refusal must reach the model as a failure (2026-09-19).

call_tool wraps an answered MCP call as {"success": True, "result": <parsed
JSON>}; the tool's own verdict sits inside 'result'. The executor used to test
it with json.loads(str(dict)) inside a bare except, so set_cell_formula
refusals were recorded as successes: the batch did not stop and the model was
never told the cell had not been written.
"""
import json
from pathlib import Path

from excel_cli_agent.task_executor import ExcelTaskExecutor

refusal = ExcelTaskExecutor._tool_refusal

# What set_cell_formula returns when its validator rejects a formula.
TOOL_JSON = json.dumps({
    "success": False,
    "cell": "B6",
    "formula": "=ROUND(Ratios!C20,2)",
    "error": "Formula validation failed:\nUnknown function: FOO\nWarning: check the name",
    "error_type": "VALIDATION_ERROR",
}, indent=2)


def test_parsed_dict_refusal_is_a_failure():
    # the shape call_tool actually hands over: already-parsed JSON
    msg = refusal(json.loads(TOOL_JSON))
    assert msg == ("Refused at B6, nothing was written: Formula validation failed: "
                   "Unknown function: FOO Warning: check the name")


def test_text_refusals_are_failures_too():
    assert refusal(TOOL_JSON).startswith("Refused at B6")           # JSON text
    assert refusal(str(json.loads(TOOL_JSON))).startswith("Refused at B6")  # Python repr, the old blind spot
    assert refusal('{"success": false}') == "Refused, nothing was written: no reason given"


def test_successes_and_ordinary_results_are_not_failures():
    for payload in ({"success": True, "calculated_value": 15999.2}, {"cells": [[1, 2]]}, {"success": None},
                    [], [{"success": False}], "Created file solution.xlsx", "{not json}", "", None, 0,
                    '{"success": true}', "{'success': True}"):
        assert refusal(payload) is None, payload


def test_refused_step_stays_in_the_history_and_stops_the_batch():
    src = (Path(__file__).resolve().parents[1] / "excel_cli_agent" / "task_executor.py").read_text()
    assert "json.loads(result_str)" not in src, "the repr-through-json check is back"
    i = src.index("refusal = self._tool_refusal(payload)")
    branch = src[i: src.index("print(f\"✅ Success:", i)]
    # the model only learns of the refusal from RECENT ACTIONS, built from task.steps
    assert "step.error = refusal" in branch and "task.steps.append(step)" in branch
    assert "failed_action_index = action_idx" in branch and "break" in branch
    # and the reason must survive the history cut
    assert 'context += f"   Error: {step.error[:300]}\\n"' in src
