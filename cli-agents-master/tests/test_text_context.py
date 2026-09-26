"""Offline tests for the .md (house standards) text context (no API, no MCP).

Covers: registration from the workspace only, the HOUSE STANDARDS header and
full text in the assembled context on both sides of the budget (the text is
exempt from the shrink ladder), the byte-identical fast path when nothing is
registered, and the Excel-tool guard on .md names.

Run:  pytest tests/test_text_context.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_context_budget import make_executor, make_task, write_workbook  # noqa: E402

STANDARDS_TEXT = (
    "# Financial Modelling - House Standards\n\n"
    "**Signs.** Costs and outflows negative.\n\n"
    "**Numbers.** Zeros as dashes.\n"
)


def _with_standards(tmp_path):
    (tmp_path / "House_Standards_v1.md").write_text(STANDARDS_TEXT)
    write_workbook(tmp_path / "solution.xlsx", big_rows=20)
    ex = make_executor(tmp_path)
    added = ex.add_context_texts(["House_Standards_v1.md"])
    assert added["not_found"] == []
    assert len(added["added"]) == 1 and added["total_context_texts"] == 1
    return ex


def test_registration_only_from_workspace(tmp_path):
    ex = make_executor(tmp_path)
    res = ex.add_context_texts(["Nope.md"])
    assert res["added"] == [] and res["not_found"] == ["Nope.md"]
    # only .md is text context — a .txt is not silently accepted
    (tmp_path / "notes.txt").write_text("x")
    res = ex.add_context_texts(["notes.txt"])
    assert res["not_found"] == ["notes.txt"]


def test_full_text_in_fast_path(tmp_path):
    ex = _with_standards(tmp_path)
    task = make_task()
    assembled = ex._assemble_context(task, system_prompt="SYS")
    assert task.context_reduced is False
    assert "HOUSE STANDARDS (House_Standards_v1.md)" in assembled
    assert STANDARDS_TEXT.strip() in assembled
    # the listing in the head names the file and points at the header
    assert "=== ATTACHED TEXT FILES (1) ===" in assembled
    assert "- House_Standards_v1.md" in assembled
    # the standards precede the solution-grid tail / PDF extras
    assert assembled.index("HOUSE STANDARDS (") > assembled.index("CURRENT TASK:")


def test_full_text_survives_reduced_path(tmp_path):
    """Over budget the sheets summarize, but the standards are never cut."""
    (tmp_path / "House_Standards_v1.md").write_text(STANDARDS_TEXT)
    write_workbook(tmp_path / "solution.xlsx", big_rows=3000)
    ex = make_executor(tmp_path, context_window=12_000)
    ex.add_context_texts(["House_Standards_v1.md"])
    task = make_task()
    assembled = ex._assemble_context(task, system_prompt="SYS")
    assert task.context_reduced is True
    assert "SHEET SUMMARIZED" in assembled
    assert STANDARDS_TEXT.strip() in assembled
    assert "HOUSE STANDARDS (House_Standards_v1.md)" in assembled


def test_fast_path_unchanged_without_texts(tmp_path):
    """No registered text -> byte-identical to the historical context."""
    write_workbook(tmp_path / "solution.xlsx", big_rows=20)
    ex = make_executor(tmp_path)
    task = make_task()
    assert ex._assemble_context(task, system_prompt="SYS") == ex._get_context_prompt(task)
    assert "ATTACHED TEXT FILES" not in ex._get_context_prompt(task)


def test_excel_tool_guard_blocks_md(tmp_path):
    ex = make_executor(tmp_path)
    ex.current_execution = None
    ex.langfuse_enabled = False
    res = ex._execute_action({"tool": "get_cell_range", "parameters": {"filename": "House_Standards_v1.md"}})
    assert res["success"] is False
    assert "HOUSE STANDARDS (House_Standards_v1.md)" in res["error"]
    assert "ALREADY IN YOUR PROMPT" in res["error"]
    # the PDF branch keeps its wording
    res = ex._execute_action({"tool": "get_cell_range", "parameters": {"filename": "Case.pdf"}})
    assert res["success"] is False and "PDF: Case.pdf" in res["error"]
