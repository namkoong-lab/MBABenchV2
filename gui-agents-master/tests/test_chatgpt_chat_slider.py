"""Offline checks for the ChatGPT slider-generation pickers. No browser.

Chat mode moved to the slider picker (probed live 2026-09-21): model radios
Latest / GPT-5.6 Sol / GPT-5.5, a Power ladder Instant..Pro, pill "6Pro" at
the top stop under Latest. The chat branch reuses the work-mode slider walk
with its own ladder, so these tests pin BOTH: the chat path, and that the
work-mode walk (default ladder, Ultra, the 'Default' model trap) behaves
exactly as it did before the ladder became a parameter.

Run from gui-agents-master:  python -m pytest tests/test_chatgpt_chat_slider.py
"""
import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from claude_web_agent.chatgpt_web_agent import ChatGPTWebAgent  # noqa: E402
from infra.configs.agent_identity import (  # noqa: E402
    UnknownAgentCombination,
    resolve_agent_identity,
)


# ---- a fake page: just enough DOM for the slider helpers --------------------


class FakeEl:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    async def evaluate(self, js, *a):
        return None

    async def text_content(self):
        return self.page.pill_text()

    async def click(self, **kw):
        if self.kind == "pill":
            self.page.menu_open = True


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        p = self.page
        p.presses.append(key)
        if key == "ArrowRight":
            p.idx = min(p.idx + 1, len(p.stops) - 1)
        elif key == "ArrowLeft":
            p.idx = max(p.idx - 1, 0)
        elif key == "Escape":
            p.menu_open = False


class FakePage:
    """A pill menu with a Power slider over ``stops`` and one model radio."""

    def __init__(self, stops, start, radios, pill_fmt):
        self.stops, self.idx = list(stops), list(stops).index(start)
        self.radios = dict(radios)  # first line -> checked
        self.pill_fmt = pill_fmt    # callable(stop_label) -> pill text
        self.menu_open = True
        self.presses = []
        self.keyboard = FakeKeyboard(self)

    def pill_text(self):
        return self.pill_fmt(self.stops[self.idx])

    async def evaluate(self, js, arg=None):
        if "aria-describedby" in js:
            return [self.stops[self.idx], self.idx + 1, len(self.stops)]
        if "document.activeElement ===" in js:
            return True
        if "aria-disabled" in js:
            return False
        if "menuitemradio" in js and "dispatchEvent" in js:  # radio click
            if arg not in self.radios:
                return False
            self.radios = {k: (k == arg) for k in self.radios}
            return True
        if "menuitemradio" in js:                            # radio state
            return self.radios.get(arg)
        raise AssertionError(f"unexpected evaluate: {js[:60]}")

    async def query_selector(self, sel):
        if sel == '[role="menu"][data-state="open"]':
            return FakeEl(self, "menu") if self.menu_open else None
        if "__composer-pill" in sel or sel.startswith("form button"):
            return FakeEl(self, "pill")
        if 'aria-label="Power"' in sel:
            return FakeEl(self, "power")
        if 'aria-label="Select model"' in sel:
            return FakeEl(self, "toggle")
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(*a, **kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


WORK6 = ("Light", "Medium", "High", "Extra High", "Max", "Ultra")
CHAT5 = ("Instant", "Medium", "High", "Extra High", "Pro")


def agent_on(page, **block):
    return ChatGPTWebAgent(page, {"chatgpt_web": block})


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- work mode: unchanged ---------------------------------------------------


def test_work_ladder_is_the_default_and_untouched():
    sig = inspect.signature(ChatGPTWebAgent._slider_set_effort)
    assert sig.parameters["ladder"].default is None
    assert ChatGPTWebAgent._SLIDER_EFFORT_LADDER == WORK6


def test_work_walk_reaches_ultra_without_a_ladder_argument():
    page = FakePage(WORK6, "Max", {"GPT-6 Astra": True}, lambda s: f"GPT-6 Astra{s}")
    assert run(agent_on(page, mode="work")._slider_set_effort("Ultra")) is True
    assert page.presses == ["ArrowRight"]
    assert page.pill_text() == "GPT-6 AstraUltra"


def test_work_walk_goes_left_too():
    page = FakePage(WORK6, "Ultra", {"GPT-6 Astra": True}, lambda s: s)
    assert run(agent_on(page, mode="work")._slider_set_effort("High")) is True
    assert page.presses == ["ArrowLeft"] * 3


def test_work_default_model_trap_still_refuses_ultra(caplog):
    """Under 'Default' the slider has 5 stops; Ultra must fail loudly with
    the original hint, not walk to the last stop."""
    page = FakePage(WORK6[:5], "High", {"Default": True}, lambda s: s)
    with caplog.at_level("ERROR"):
        assert run(agent_on(page, mode="work")._slider_set_effort("Ultra")) is False
    assert page.presses == []
    assert "Ultra requires an explicit model, not 'Default'" in caplog.text


def test_chat_tier_is_not_a_work_effort():
    page = FakePage(WORK6, "Max", {"GPT-6 Astra": True}, lambda s: s)
    assert run(agent_on(page, mode="work")._slider_set_effort("Pro")) is False
    assert page.presses == []


# ---- chat mode: the slider branch -------------------------------------------


@pytest.mark.parametrize("pill,ok", [
    ("6Pro", True),
    ("6 Pro", True),
    ("6pro", True),
    ("6.1Pro", False),       # "Latest" re-pointed to a newer model
    ("6 AstraPro", False),
    ("5.5Pro", False),
    ("Pro", False),          # token dropped — cannot tell which model
    ("6Extra High", False),
    ("GPT-6 AstraMax", False),
    ("", False),
    (None, False),
])
def test_chat_pill_is_matched_exactly(pill, ok):
    assert ChatGPTWebAgent._chat_pill_matches(pill, "6", "Pro") is ok


def test_chat_walk_instant_to_pro():
    page = FakePage(CHAT5, "Instant", {"Latest": True}, lambda s: s)
    a = agent_on(page, mode="chat")
    assert run(a._slider_set_effort("Pro", ladder=a._CHAT_SLIDER_LADDER)) is True
    assert page.presses == ["ArrowRight"] * 4


def test_chat_walk_already_at_pro_presses_nothing():
    page = FakePage(CHAT5, "Pro", {"Latest": True}, lambda s: s)
    a = agent_on(page, mode="chat")
    assert run(a._slider_set_effort("Pro", ladder=a._CHAT_SLIDER_LADDER)) is True
    assert page.presses == []


def test_work_effort_is_not_a_chat_tier():
    page = FakePage(CHAT5, "Instant", {"Latest": True}, lambda s: s)
    a = agent_on(page, mode="chat")
    assert run(a._slider_set_effort("Ultra", ladder=a._CHAT_SLIDER_LADDER)) is False
    assert page.presses == []


def _chat_pill(stop):
    # live 2026-09-21: "Instant" alone at the bottom stop, "6Pro" at the top
    return "Instant" if stop == "Instant" else f"6{stop}"


def test_chat_slider_end_to_end_latest_pro():
    page = FakePage(CHAT5, "Instant", {"Latest": True, "GPT-5.6 Sol": False}, _chat_pill)
    a = agent_on(page, mode="chat", model="gpt_6", intelligence="pro")
    assert run(a.ensure_model_and_intelligence()) is True
    assert page.pill_text() == "6Pro"
    assert page.radios["Latest"] is True
    assert page.menu_open is False          # nothing left open before the send
    assert page.presses[-1] == "Escape"
    assert "ArrowRight" not in page.presses[page.presses.index("Escape"):]


def test_chat_slider_selects_latest_when_another_model_is_checked():
    page = FakePage(CHAT5, "High", {"Latest": False, "GPT-5.6 Sol": True}, _chat_pill)
    a = agent_on(page, mode="chat", model="gpt_6", intelligence="pro")
    assert run(a.ensure_model_and_intelligence()) is True
    assert page.radios == {"Latest": True, "GPT-5.6 Sol": False}
    assert page.pill_text() == "6Pro"


def test_chat_slider_refuses_a_repointed_latest():
    """'Latest' now resolves to something else: the pill says so, and the
    send must not happen under the gpt_6 label."""
    page = FakePage(CHAT5, "Instant", {"Latest": True}, lambda s: f"6.1{s}")
    a = agent_on(page, mode="chat", model="gpt_6", intelligence="pro")
    assert run(a.ensure_model_and_intelligence()) is False


def test_chat_slider_refuses_a_missing_model_radio():
    page = FakePage(CHAT5, "Instant", {"GPT-5.6 Sol": True}, _chat_pill)
    a = agent_on(page, mode="chat", model="gpt_6", intelligence="pro")
    assert run(a.ensure_model_and_intelligence()) is False
    assert "ArrowRight" not in page.presses


# ---- identity ---------------------------------------------------------------


def _cfg(**block):
    return NS(benchmark="v2", provider=NS(kind="chatgpt"), chatgpt_web=NS(**block))


def test_gpt_6_chat_identity_and_its_neighbours():
    got = resolve_agent_identity(_cfg(mode="chat", model="gpt_6", intelligence="pro"))
    assert got.model_name == got.agent_folder == "chatgpt_gpt_6_pro"
    for block in (
        dict(mode="chat", model="gpt_6_astra", intelligence="pro"),  # no such radio in chat
        dict(mode="chat", model="gpt_6", intelligence=None),         # tier must be pinned
        dict(mode="chat", model="gpt_6", intelligence="xhigh"),
        dict(mode="work", model="gpt_6", effort="ultra", speed="standard"),
    ):
        with pytest.raises(UnknownAgentCombination):
            resolve_agent_identity(_cfg(**block))
    # the finished work cohort is untouched
    work = resolve_agent_identity(
        _cfg(mode="work", model="gpt_6_astra", effort="ultra", speed="standard")
    )
    assert work.model_name == "chatgpt_gpt_6_astra_work_ultra"


# ---- 2026-09-22 composer rebuild --------------------------------------------
# The composer shipped a new generation mid-run (live, lane B account): the
# hidden file input lost its id, the Chat/Work radios became aria-pressed
# buttons inside [role=group][aria-label="Composer mode"], and the picker
# button reads "Thinking effortPro" — the tier only, no model token. The menu
# itself is unchanged: a "Select model" row reading "6\nPro", the Power
# slider, and the Latest / GPT-5.6 Sol / GPT-5.5 radios.


def test_upload_selectors_cover_both_generations_and_skip_photo_inputs():
    sels = ChatGPTWebAgent.UPLOAD_INPUT_SELECTORS
    assert sels[0] == 'input#upload-files[type="file"]'            # old UI first
    assert 'input[type="file"][aria-label="Attach files"]' in sels  # 2026-09-22
    # every fallback that is not id- or aria-anchored must exclude accept=…,
    # or the photo/camera inputs would swallow the .xlsx
    for s in sels:
        if "#upload-files" not in s and "aria-label" not in s:
            assert ":not([accept])" in s, s


def test_new_composer_row_text_still_proves_the_model():
    m = ChatGPTWebAgent._chat_pill_matches
    assert m("6\nPro", "6", "Pro") is True        # menu row, new UI
    assert m("6Pro", "6", "Pro") is True          # pill, old UI
    assert m("Thinking effortPro", "6", "Pro") is False   # tier only — not proof
    assert m("6.1\nPro", "6", "Pro") is False
    assert m("GPT-5.6 Sol\nPro", "6", "Pro") is False


def test_pill_lookup_knows_the_new_button():
    assert 'button[aria-label="Select ChatGPT model"]' in inspect.getsource(
        ChatGPTWebAgent._get_pill
    )


def test_mode_lookup_accepts_aria_pressed_group():
    src = inspect.getsource(ChatGPTWebAgent.ensure_mode)
    assert "composer mode" in src.lower()
    assert "aria-pressed" in src
    assert 'button[role="radio"]' in src          # old UI still first
