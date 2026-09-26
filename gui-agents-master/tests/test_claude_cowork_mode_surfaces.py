"""ensure_mode across the two claude.ai UI generations. No browser.

2026-09-23: the four Opus 5.5 lanes run on four accounts, two of which
still show the Chat/Cowork toggle and two of which have dropped it (that
UI is always cowork). A cowork run has to pass on both, without letting a
genuinely chat-only surface — or a page that simply hasn't rendered —
pass as cowork.

Run from gui-agents-master:  python -m pytest tests/test_claude_cowork_mode_surfaces.py
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.claude_web_agent import ClaudeWebAgent  # noqa: E402


class FakeRadio:
    def __init__(self, checked=True):
        self._checked = "true" if checked else "false"

    async def get_attribute(self, name):
        return self._checked if name == "aria-checked" else None


def agent(mode, radios, model_button=True):
    """A ClaudeWebAgent with only what ensure_mode touches."""
    a = object.__new__(ClaudeWebAgent)
    a.agent_config = {"mode": mode}
    a.approval_calls = 0

    async def find(label):
        return radios.get(label)

    async def get_model_button():
        return object() if model_button else None

    async def approval():
        a.approval_calls += 1
        return True

    a._find_mode_radio = find
    a._get_model_button = get_model_button
    a.ensure_cowork_approval = approval
    return a


@pytest.mark.asyncio
async def test_cowork_toggle_present_and_selected():
    a = agent("cowork", {"Cowork": FakeRadio(checked=True)})
    assert await a.ensure_mode() is True
    assert a.approval_calls == 1


@pytest.mark.asyncio
async def test_cowork_passes_when_the_ui_has_no_toggle_at_all():
    """The newer UI: no mode radios, page rendered. Always cowork."""
    a = agent("cowork", {})
    assert await a.ensure_mode() is True
    assert a.approval_calls == 1


@pytest.mark.asyncio
async def test_cowork_fails_on_a_chat_only_surface():
    """A Chat radio with no Cowork one is an account without the feature."""
    a = agent("cowork", {"Chat": FakeRadio(checked=True)})
    assert await a.ensure_mode() is False
    assert a.approval_calls == 0


@pytest.mark.asyncio
async def test_cowork_fails_when_the_page_has_not_rendered():
    """No radios AND no model button — 'no toggle' proves nothing yet."""
    a = agent("cowork", {}, model_button=False)
    assert await a.ensure_mode() is False
    assert a.approval_calls == 0


@pytest.mark.asyncio
async def test_chat_still_passes_on_a_toggle_less_surface():
    a = agent("chat", {})
    assert await a.ensure_mode() is True
    assert a.approval_calls == 0


# --- approval menu rows, both UI generations -----------------------------
# Live rows captured from the cowork-only UI, 2026-09-23 (lane C).
NEW_UI_ROWS = [
    "ManualClaude asks before using new tools or opening new site",
    "AutoClaude runs on its own and pauses to ask if anything loo",
]
OLD_UI_ROWS = ["Manually approve", "Automatically approve", "Skip all approvals"]

match = ClaudeWebAgent._approval_option_matches
FULL = ClaudeWebAgent.COWORK_APPROVAL_LABELS
SHORT = ClaudeWebAgent.COWORK_APPROVAL_SHORT


@pytest.mark.parametrize("rows", [NEW_UI_ROWS, OLD_UI_ROWS])
def test_auto_matches_exactly_one_row(rows):
    hits = [r for r in rows if match(FULL["auto"], SHORT["auto"], r)]
    assert hits == [rows[1]]


@pytest.mark.parametrize("rows", [NEW_UI_ROWS, OLD_UI_ROWS])
def test_manual_matches_exactly_one_row(rows):
    hits = [r for r in rows if match(FULL["manual"], SHORT["manual"], r)]
    assert hits == [rows[0]]


def test_skip_finds_nothing_on_the_cowork_only_ui():
    """That UI offers no Skip row — better to fail loudly than pick Auto."""
    assert not [r for r in NEW_UI_ROWS if match(FULL["skip"], SHORT["skip"], r)]
    assert [r for r in OLD_UI_ROWS if match(FULL["skip"], SHORT["skip"], r)]


def test_a_description_mentioning_the_other_mode_does_not_match():
    assert not match(FULL["auto"], SHORT["auto"],
                     "ManualClaude asks before running automatically")
    assert not match(FULL["auto"], SHORT["auto"], "")
