"""Offline check on the ChatGPT wait loop's dead-turn exit. No browser.

Live 2026-09-21 (chat mode at Pro, ~80 min into DailyCash): the assistant turn
collapsed to a bare "Thinking failed" chip — no text, no file, no Retry button.
The loop sat on it for 30 minutes until the dead-page watchdog fired. It now
fails the attempt after a short grace so the engine retries in a fresh chat.
Chat mode only, and only when the chip is the WHOLE response.

Run from gui-agents-master:  python -m pytest tests/test_chatgpt_dead_turn.py
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.chatgpt_web_agent import ChatGPTWebAgent  # noqa: E402


class WaitPage:
    url = "https://chatgpt.com/c/abc"

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0.005)

    async def evaluate(self, *a, **kw):
        return None


def make_agent(mode, response, monkeypatch):
    agent = ChatGPTWebAgent(
        WaitPage(),
        {"chatgpt_web": {"mode": mode, "max_wait_per_prompt_seconds": 1,
                         "check_interval_seconds": 0}},
    )
    agent.TURN_FAILED_GRACE_SEC = 0
    calls = {"gen": 0}

    async def generating_once():
        calls["gen"] += 1
        return calls["gen"] == 1          # the turn starts, then goes quiet

    async def const(value):
        return value

    monkeypatch.setattr(agent, "_is_generating", generating_once)
    monkeypatch.setattr(agent, "_count_response_articles", lambda: const(1))
    monkeypatch.setattr(agent, "_count_xlsx_tiles", lambda: const(0))
    monkeypatch.setattr(agent, "_recover_stream_error", lambda: const(False))
    monkeypatch.setattr(agent, "_recover_content_load_error", lambda: const(False))
    monkeypatch.setattr(agent, "_check_usage_limit", lambda: const(None))
    monkeypatch.setattr(agent, "_extract_last_response", lambda: const(response))
    monkeypatch.setattr(agent, "_activity_fingerprint", lambda: const(""))
    return agent


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_chat_dead_turn_fails_the_attempt_fast(monkeypatch, caplog):
    agent = make_agent("chat", "Thinking failed", monkeypatch)
    with caplog.at_level("INFO"):
        assert run(agent.wait_for_response(1)) is None
    assert "Turn ended with 'Thinking failed'" in caplog.text
    assert "Response timeout" not in caplog.text      # did not wait it out


def test_work_mode_is_left_alone(monkeypatch, caplog):
    agent = make_agent("work", "Thinking failed", monkeypatch)
    with caplog.at_level("INFO"):
        run(agent.wait_for_response(1))
    assert "Turn ended with" not in caplog.text


def test_the_words_inside_a_real_answer_do_not_trip_it(monkeypatch, caplog):
    text = ("Thinking failed on the first pass, so I rebuilt the schedule. "
            "Download the finished workbook\nModel.xlsx\nSpreadsheet")
    agent = make_agent("chat", text, monkeypatch)
    with caplog.at_level("INFO"):
        run(agent.wait_for_response(1))
    assert "Turn ended with" not in caplog.text
