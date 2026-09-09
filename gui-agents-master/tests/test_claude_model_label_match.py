"""Offline check on the claude.ai model-label matcher. No browser.

The dropdown grew a "Fable 5.1" entry (2026-09). The fable_5 cohort must
keep selecting "Fable 5", so the label match is whole-token: a point
release never satisfies a request for its base version, and vice versa.

Run from gui-agents-master:  python -m pytest tests/test_claude_model_label_match.py
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.claude_web_agent import ClaudeWebAgent  # noqa: E402

match = ClaudeWebAgent._model_label_matches


@pytest.mark.parametrize("text", [
    "Model: Fable 5 Max",              # button aria-label, effort suffix
    "Fable 5",                         # bare
    "fable 5 max",                     # case-insensitive
    "Fable 5Most capable for complex work",   # radio text, no separator
    "Fable 5 · Most capable",
])
def test_fable_5_matches_its_own_entries(text):
    assert match("Fable 5", text)


@pytest.mark.parametrize("text", [
    "Model: Fable 5.1 Max",
    "Fable 5.1",
    "Fable 5.1Newest and most capable",
    "Model: Opus 5 Max",
    "",
    None,
])
def test_fable_5_rejects_other_models(text):
    assert not match("Fable 5", text)


def test_fable_5_1_is_its_own_label():
    assert match("Fable 5.1", "Model: Fable 5.1 Max")
    assert not match("Fable 5.1", "Model: Fable 5 Max")
    assert not match("Fable 5.1", "Fable 5.10")
    assert not match("Fable 5", "Fable 50")


@pytest.mark.parametrize("label,text,expected", [
    ("Opus 4", "Opus 4.8", False),
    ("Opus 4.8", "Model: Opus 4.8 High", True),
    ("Haiku 4.5", "Haiku 4.5Fastest", True),
    ("Sonnet 5", "Sonnet 5.5", False),
    ("Opus 5", "Cowork Opus 5 Max", True),
])
def test_version_tokens(label, text, expected):
    assert match(label, text) is expected


def test_label_map_knows_both_fables():
    agent_labels = ClaudeWebAgent.MODEL_LABELS
    assert agent_labels["fable_5"] == "Fable 5"
    assert agent_labels["fable_5_1"] == "Fable 5.1"


def test_fable_5_1_has_no_identity():
    """Registering the label must not make it runnable: the identity table
    has no fable_5_1 entry, so a run config naming it is refused."""
    from types import SimpleNamespace as NS
    from infra.configs.agent_identity import (
        UnknownAgentCombination, resolve_agent_identity,
    )
    cfg = NS(benchmark="v2", provider=NS(kind="claude"),
             claude_web=NS(mode="chat", model="fable_5_1", effort="max"))
    with pytest.raises(UnknownAgentCombination):
        resolve_agent_identity(cfg)
