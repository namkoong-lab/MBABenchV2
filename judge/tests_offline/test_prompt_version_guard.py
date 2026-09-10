"""Offline: the latest-prompt guard (2026-09-10) — DB drivers refuse to spend
on superseded prompt generations for v2 unless told otherwise.

Run from judge/:  uv run pytest tests_offline/test_prompt_version_guard.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.misc_utils import (  # noqa: E402
    LATEST_PROMPT_VERSION_BY_TYPE,
    apply_latest_prompt_guard,
    latest_prompt_versions,
)


def test_table_covers_every_v2_pipeline():
    assert set(LATEST_PROMPT_VERSION_BY_TYPE["v2"]) == {"gui", "excel", "api", "coding_cli"}
    assert latest_prompt_versions("v1") is None, "v1 is closed; no guard"


def test_guard_keeps_latest_drops_older_passes_unknown_types():
    rows = [
        {"attempt_id": 1, "agent_model_type": "gui", "prompt_version": 205},
        {"attempt_id": 2, "agent_model_type": "gui", "prompt_version": 203},
        {"attempt_id": 3, "agent_model_type": "gui", "prompt_version": 1},
        {"attempt_id": 4, "agent_model_type": "api", "prompt_version": 1408},
        {"attempt_id": 5, "agent_model_type": "api", "prompt_version": 1307},
        {"attempt_id": 6, "agent_model_type": "coding_cli", "prompt_version": 112},
        {"attempt_id": 7, "agent_model_type": "coding_cli", "prompt_version": 111},
        {"attempt_id": 8, "agent_model_type": "excel", "prompt_version": 205},
        {"attempt_id": 9, "agent_model_type": "human", "prompt_version": None},
    ]
    kept, dropped = apply_latest_prompt_guard(rows, latest_prompt_versions("v2"))
    assert [r["attempt_id"] for r in kept] == [1, 4, 6, 8, 9]
    assert [r["attempt_id"] for r in dropped] == [2, 3, 5, 7]


def test_guard_is_a_no_op_without_a_table():
    rows = [{"attempt_id": 1, "agent_model_type": "gui", "prompt_version": 9}]
    kept, dropped = apply_latest_prompt_guard(rows, None)
    assert kept == rows and dropped == []


def test_drivers_wire_the_guard():
    judge = Path(__file__).resolve().parents[1]
    orch = (judge / "main_scripts" / "grade_with_orchestration.py").read_text()
    gdb = (judge / "main_scripts" / "grade_from_db.py").read_text()
    for src in (orch, gdb):
        assert "apply_latest_prompt_guard(" in src and "--all-prompt-versions" in src
    manifest = (judge.parent / "scripts" / "export_good_attempts.py").read_text()
    for t, pv in LATEST_PROMPT_VERSION_BY_TYPE["v2"].items():
        assert f'"{t}": {pv}' in manifest, f"manifest LATEST_PV disagrees with the judge table for {t}"
