"""Offline checks for the 2026-09-22 ChatGPT composer's turn markup. No browser.

Live that day (lane B): the rebuilt UI dropped every marker the wait loop knew.
The Stop control kept only an aria-label, exchanges became [data-turn-key]
wrappers with a [data-user-message-bubble] and a hidden
[data-chatgpt-agent-turn-start] anchor, and nothing carried
data-message-author-role, [data-turn] or .agent-turn. Result: "Response
generation did not start within 120s" on every task while the model was in
fact answering. The older markers must keep winning, since the other account
was still on the previous UI at the same moment.

Run from gui-agents-master:  python -m pytest tests/test_chatgpt_new_composer_turns.py
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.chatgpt_web_agent import ChatGPTWebAgent  # noqa: E402

PROMPT = "You are an expert financial-modeling agent. Build the model now."
ANSWER = "Assessed missing inputs\n\nDesigned an intake workbook"


class DomPage:
    """Evaluates the agent's real JS against a tiny in-python DOM model."""

    url = "https://chatgpt.com/c/abc"

    def __init__(self, ui, generating=False):
        self.ui, self.generating = ui, generating

    async def evaluate(self, js, *a):
        old = self.ui == "old"
        if "hasStopBtn" in js:                      # _is_generating
            return self.generating
        if "data-chatgpt-agent-turn-start" in js and "querySelectorAll" in js and "innerText" not in js:
            return 1 if (self.generating or self.ui == "new") else 0
        if "data-message-author-role" in js:        # _extract_last_response
            return f"{PROMPT}\n{ANSWER}".split(PROMPT)[-1].strip() if old else ANSWER
        return None


def agent(page):
    return ChatGPTWebAgent(page, {"chatgpt_web": {"mode": "chat"}})


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_is_generating_accepts_an_aria_only_stop_button():
    src = ChatGPTWebAgent._is_generating.__doc__ or ""
    import inspect
    js = inspect.getsource(ChatGPTWebAgent._is_generating)
    assert "hasStopAria" in js and "aria-label" in js
    # the older signals stay in the disjunction
    for older in ("hasStopBtn", "hasStop ", "hasAnswerNow", "hasThinking"):
        assert older in js


def test_turn_count_falls_back_to_the_new_anchor_only_last():
    import inspect
    js = inspect.getsource(ChatGPTWebAgent._count_response_articles)
    order = [js.index(s) for s in (
        "data-message-author-role", "[data-turn]", ".agent-turn",
        "data-chatgpt-agent-turn-start")]
    assert order == sorted(order), "older markers must be tried first"


def test_extractor_subtracts_the_user_bubble():
    import inspect
    js = inspect.getsource(ChatGPTWebAgent._extract_last_response)
    assert "data-turn-key" in js and "data-user-message-bubble" in js
    assert "indexOf(said)" in js, "the prompt must be sliced off the turn text"
    order = [js.index(s) for s in (
        "data-message-author-role", "[data-turn]", ".agent-turn", "data-turn-key")]
    assert order == sorted(order), "older strategies must be tried first"


@pytest.mark.parametrize("ui", ["old", "new"])
def test_extraction_returns_the_answer_not_the_prompt(ui):
    text = run(agent(DomPage(ui))._extract_last_response())
    assert text and PROMPT not in text and "Designed an intake workbook" in text
