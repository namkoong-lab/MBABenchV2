"""Offline checks: never send with an attachment missing. No browser.

Live 2026-09-22/23 (lane B): uploads settled and the picker verified, then one
of the two tiles quietly vanished — the workbook on one task, House_Standards
on another. The composer then refuses to submit: the Send click does nothing,
Enter does nothing, and the attempt burns both wait windows. The agent now
re-uploads what went missing, and fails the attempt if it cannot.

Run from gui-agents-master:  python -m pytest tests/test_chatgpt_attachment_guard.py
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.chatgpt_web_agent import ChatGPTWebAgent  # noqa: E402

FILES = ["/tmp/CFForecast.xlsx", "/tmp/House_Standards_v1.md"]


class Locator:
    def __init__(self, page, stem):
        self.page, self.stem = page, stem

    async def count(self):
        return 1 if self.stem in self.page.present else 0

    @property
    def first(self):
        return self

    async def wait_for(self, **kw):
        if self.stem not in self.page.present:
            raise AssertionError("never attached")


class Page:
    url = "https://chatgpt.com/"

    def __init__(self, present, attach_on_retry=True):
        self.present = set(present)
        self.attach_on_retry = attach_on_retry
        self.sets = []

    def locator(self, sel):
        stem = sel.split('aria-label*="')[1].split('"')[0]
        return Locator(self, stem)

    async def evaluate(self, *a, **kw):
        return None


def agent(page, present_files):
    a = ChatGPTWebAgent(page, {"chatgpt_web": {"mode": "chat"}})
    a._expected_attachments = list(FILES)
    return a


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_nothing_missing_is_a_no_op(monkeypatch):
    page = Page({"CFForecast", "House_Standards_v1"})
    a = agent(page, FILES)
    called = []
    monkeypatch.setattr(a, "_direct_upload_input", lambda: _async(called.append("looked")))
    assert run(a._missing_attachments()) == []
    assert run(a._restore_attachments()) is True
    assert called == [], "must not touch the input when both tiles are there"


def _async(value=None):
    async def inner():
        return value
    return inner()


def test_a_vanished_tile_is_named_and_re_uploaded(monkeypatch, caplog):
    page = Page({"CFForecast"})           # the .md disappeared
    a = agent(page, FILES)

    async def fake_input():
        return "INPUT"

    async def fake_set(inp, path):
        page.sets.append(path)
        page.present.add(Path(path).stem)   # the re-upload lands

    async def settled(n, **kw):
        return True

    monkeypatch.setattr(a, "_direct_upload_input", fake_input)
    monkeypatch.setattr(a, "_set_input_files_cdp_safe", fake_set)
    monkeypatch.setattr(a, "_wait_for_uploads_complete", settled)
    with caplog.at_level("WARNING"):
        assert run(a._restore_attachments()) is True
    assert page.sets == ["/tmp/House_Standards_v1.md"]
    assert "House_Standards_v1.md" in caplog.text


def test_a_re_upload_that_does_not_stick_fails_the_attempt(monkeypatch):
    page = Page({"House_Standards_v1"})   # the workbook disappeared
    a = agent(page, FILES)

    async def fake_input():
        return "INPUT"

    async def fake_set(inp, path):
        page.sets.append(path)            # never attaches

    monkeypatch.setattr(a, "_direct_upload_input", fake_input)
    monkeypatch.setattr(a, "_set_input_files_cdp_safe", fake_set)
    assert run(a._restore_attachments()) is False


def test_no_expectation_recorded_means_no_guard():
    page = Page(set())
    a = ChatGPTWebAgent(page, {"chatgpt_web": {"mode": "chat"}})
    assert run(a._missing_attachments()) == []
    assert run(a._restore_attachments()) is True


def test_submit_refuses_to_send_without_the_attachments():
    """The guard runs in the function that actually sends, before the click."""
    import inspect
    src = inspect.getsource(ChatGPTWebAgent._submit_prompt_once)
    assert "_restore_attachments" in src
    assert src.index("_restore_attachments") < src.index("url_before = self.page.url")


# ---- swallowed send: reload once, then give up -------------------------------
# Live 2026-09-23 (lane B): composer holding the prompt and both attachments,
# Send enabled, focus in the editor — every click and the Enter key silently
# swallowed. The identical click worked seconds after a page reload, and the
# engine's own attempt-retry did NOT clear it.


class ReloadPage(Page):
    def __init__(self):
        super().__init__({"CFForecast", "House_Standards_v1"})
        self.reloads = 0

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_timeout(self, ms):
        return None


def _agent_with(page, results):
    a = ChatGPTWebAgent(page, {"chatgpt_web": {"mode": "chat"}})
    a._expected_attachments = list(FILES)
    calls = {"n": 0}

    async def once(prompt, prompt_number=1):
        calls["n"] += 1
        return results[min(calls["n"] - 1, len(results) - 1)]

    a._submit_prompt_once = once
    a.calls = calls
    return a


def test_a_clean_send_never_reloads():
    page = ReloadPage()
    a = _agent_with(page, [True])
    assert run(a.submit_prompt("p", 1)) is True
    assert (page.reloads, a.calls["n"]) == (0, 1)


def test_a_swallowed_send_reloads_re_uploads_and_succeeds(monkeypatch, caplog):
    page = ReloadPage()
    a = _agent_with(page, [False, True])
    uploaded = []

    async def fake_upload(files):
        uploaded.extend(files)
        return True

    monkeypatch.setattr(a, "upload_files", fake_upload)
    with caplog.at_level("WARNING"):
        assert run(a.submit_prompt("p", 1)) is True
    assert page.reloads == 1 and a.calls["n"] == 2
    assert uploaded == FILES, "the attachments must be put back after a reload"
    assert "reloading the page" in caplog.text


def test_it_reloads_only_once_per_attempt(monkeypatch):
    page = ReloadPage()
    a = _agent_with(page, [False, False])

    async def fake_upload(files):
        return True

    monkeypatch.setattr(a, "upload_files", fake_upload)
    assert run(a.submit_prompt("p", 1)) is False
    assert page.reloads == 1
    # a second submit in the same attempt must not reload again
    assert run(a.submit_prompt("p", 1)) is False
    assert page.reloads == 1


def test_a_failed_re_upload_stops_the_send(monkeypatch):
    page = ReloadPage()
    a = _agent_with(page, [False, True])

    async def bad_upload(files):
        return False

    monkeypatch.setattr(a, "upload_files", bad_upload)
    assert run(a.submit_prompt("p", 1)) is False
    assert a.calls["n"] == 1, "must not send a prompt without its attachments"
