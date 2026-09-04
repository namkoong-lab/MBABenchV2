"""Offline tests for the native Anthropic adapter (utils/anthropic_native.py).

Run from judge/:  python tests_offline/test_anthropic_native.py
No network. Fake response objects verify the request translation (system
hoisting + cache markers, tool schemas, tool_result-first user merging,
thinking-block replay from the side-table, eviction, effort mapping) and the
shim's usage accounting (prompt_tokens = uncached + cache read + cache write;
cached tokens surfaced for cost). Also checks calculate_cost's cache terms.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

from utils import anthropic_native as AN  # noqa: E402
from utils.llm_utils import calculate_cost, usage_breakdown  # noqa: E402
from openai.types.chat import ChatCompletionMessage  # noqa: E402
from openai.types.chat.chat_completion_message_function_tool_call import (  # noqa: E402
    ChatCompletionMessageFunctionToolCall, Function,
)

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)
    else:
        print("OK ", msg)


TOOLS = [{
    "type": "function",
    "function": {"name": "read_file", "description": "d",
                 "parameters": {"type": "object", "properties": {"source": {"type": "string"}}}},
}, {
    "type": "function",
    "function": {"name": "record_check", "description": "r", "parameters": {"type": "object", "properties": {}}},
}]

# --- routing + effort ------------------------------------------------------
check(AN.wants_native_anthropic("anthropic") and not AN.wants_native_anthropic("openai"), "only provider=anthropic routes native")
check(AN.map_effort(None) is None, "null effort -> omit output_config")
check(AN.map_effort("max") == "max" and AN.map_effort("xhigh") == "xhigh", "real tiers pass through")
check(AN.map_effort("none") == "low" and AN.map_effort("minimal") == "low", "none/minimal -> low (floor)")
try:
    AN.map_effort("ultra")
    check(False, "unknown effort raises")
except ValueError:
    check(True, "unknown effort raises")

# --- tools --------------------------------------------------------------------
t = AN.convert_tools(TOOLS)
check(t[0] == {"name": "read_file", "description": "d",
               "input_schema": {"type": "object", "properties": {"source": {"type": "string"}}}},
      "chat tool -> anthropic tool (input_schema)")

# --- messages -----------------------------------------------------------------
state = AN.NativeState()
tc1 = ChatCompletionMessageFunctionToolCall(id="toolu_1", type="function",
                                            function=Function(name="read_file", arguments='{"source":"attempt"}'))
asst1 = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tc1])
state.by_first_call_id["toolu_1"] = [
    {"type": "thinking", "thinking": "", "signature": "sig1"},
    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"source": "attempt"}},
]
messages = [
    {"role": "system", "content": "SYS A"},
    {"role": "system", "content": "SYS B"},
    {"role": "user", "content": [{"type": "text", "text": "seed"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                                 {"type": "image_url", "image_url": {"url": "data:image/tiff;base64,QUJD"}}]},
    asst1,
    {"role": "tool", "tool_call_id": "toolu_1", "content": "Error: nope"},
    {"role": "user", "content": "[context: 10K / 850K tokens (1%). 1 rounds elapsed.]"},
    {"role": "assistant", "content": "I am done."},
    {"role": "user", "content": "nudge"},
]
system_text, native = AN.convert_messages(messages, state)
check(system_text == "SYS A\nSYS B", "system messages hoisted and joined")
check([m["role"] for m in native] == ["user", "assistant", "user", "assistant", "user"], "strict alternation kept")
u0 = native[0]["content"]
check(u0[0] == {"type": "text", "text": "seed"} and u0[1]["type"] == "image" and u0[1]["source"]["media_type"] == "image/png"
      and len(u0) == 2, "seed text + png kept, tiff dropped")
check(native[1]["content"] == state.by_first_call_id["toolu_1"], "assistant tool turn replayed verbatim (thinking included)")
u2 = native[2]["content"]
check(u2[0]["type"] == "tool_result" and u2[0]["tool_use_id"] == "toolu_1" and u2[0]["is_error"] is True
      and u2[1]["type"] == "text" and "context:" in u2[1]["text"], "tool_result leads the merged user message, pressure note follows")
check(native[3]["content"] == [{"type": "text", "text": "I am done."}], "text-only assistant turn replays as text")
check(native[-1]["role"] == "user", "request ends on a user turn")

# strip_thinking recovery
state.strip_thinking = True
_, native2 = AN.convert_messages(messages, state)
check(all(b["type"] != "thinking" for b in native2[1]["content"]), "strip_thinking drops thinking blocks from replay")
state.strip_thinking = False

# eviction: an evicted assistant turn simply isn't converted
evicted = [messages[0], messages[2], {"role": "user", "content": "[evicted rounds 1-1]"}, messages[6], messages[7]]
_, native3 = AN.convert_messages(evicted, state)
check([m["role"] for m in native3] == ["user", "assistant", "user"], "evicted rounds vanish; stub merges into the user turn")
check(any(b.get("text", "").startswith("[evicted") for b in native3[0]["content"]), "evict stub text present")

# empty user content -> placeholder, not a 400
_, native4 = AN.convert_messages([{"role": "user", "content": ""}], AN.NativeState())
check(native4 == [{"role": "user", "content": [{"type": "text", "text": "(continue)"}]}], "empty user message -> placeholder")

# --- request build: cache markers + effort + thinking ------------------------
req = AN.build_request(model="claude-sonnet-5", messages=messages, chat_tools=TOOLS,
                       reasoning_effort="low", state=state)
check(req["system"][0]["cache_control"] == {"type": "ephemeral"}, "explicit breakpoint on the frozen system prefix")
check(req["cache_control"] == {"type": "ephemeral"}, "top-level automatic cache_control for the tail")
check(req["output_config"] == {"effort": "low"}, "effort goes in output_config")
check(req["thinking"] == {"type": "adaptive"}, "adaptive thinking declared")
check(req["max_tokens"] == AN.DEFAULT_MAX_TOKENS and len(req["tools"]) == 2, "max_tokens + tools present")
req_null = AN.build_request(model="claude-opus-5", messages=messages, chat_tools=TOOLS,
                            reasoning_effort=None, state=state)
check("output_config" not in req_null, "null effort omits output_config")

# --- shim from a fake response ----------------------------------------------
def _blk(**kw):
    d = dict(kw)
    return NS(**d, model_dump=lambda exclude_none=True, _d=d: dict(_d))

resp = NS(
    content=[
        _blk(type="thinking", thinking="", signature="sig2"),
        _blk(type="text", text="Reading now."),
        _blk(type="tool_use", id="toolu_2", name="read_file", input={"source": "solution", "filename": "Q_full.csv"}),
        _blk(type="tool_use", id="toolu_3", name="record_check", input={"check": "1", "decision": "pass", "summary": "s"}),
    ],
    usage=NS(input_tokens=1200, cache_read_input_tokens=90000, cache_creation_input_tokens=3000, output_tokens=400),
    stop_reason="tool_use",
    model_dump=lambda **k: {"raw": True},
)
st2 = AN.NativeState()
shim = AN._shim_from_response(resp, st2)
msg = shim.choices[0].message
check(msg.role == "assistant" and msg.content == "Reading now.", "shim text content")
check([tc.function.name for tc in msg.tool_calls] == ["read_file", "record_check"], "two tool calls in order")
check(json.loads(msg.tool_calls[0].function.arguments) == {"source": "solution", "filename": "Q_full.csv"}, "tool args round-trip as JSON")
check(msg.tool_calls[0].id == "toolu_2" and st2.by_first_call_id["toolu_2"][0]["type"] == "thinking",
      "native blocks stored under the first tool-call id, thinking first")
ub = usage_breakdown(shim.usage)
check(ub["prompt_tokens"] == 94200 and ub["cached_tokens"] == 90000 and ub["cache_write_tokens"] == 3000
      and ub["completion_tokens"] == 400, f"usage: total input = uncached+read+write, cache detail surfaced ({ub})")
check(shim.model_dump()["adapter"] == "anthropic_native" and shim.model_dump()["usage"]["prompt_tokens"] == 94200,
      "shim model_dump for the trajectory recorder")

# refusal surfaces as an error the loop can retry/abort on
refusal = NS(content=[], usage=None, stop_reason="refusal", stop_details=NS(category="cyber"), model_dump=lambda **k: {})
class _FakeClient:
    class messages:
        @staticmethod
        def create(**kwargs):
            return refusal
try:
    AN.create(_FakeClient(), model="m", messages=messages, chat_tools=TOOLS, reasoning_effort=None, state=AN.NativeState())
    check(False, "refusal raises")
except RuntimeError as e:
    check("refusal" in str(e), "refusal raises RuntimeError")

# prefix-binding 400 -> strip thinking and retry once
calls = {"n": 0}
class _BindClient:
    class messages:
        @staticmethod
        def create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("400 Invalid `signature` in `thinking` block. The block is bound to a different conversation.")
            return resp
st3 = AN.NativeState()
st3.by_first_call_id["toolu_1"] = state.by_first_call_id["toolu_1"]
out = AN.create(_BindClient(), model="m", messages=messages, chat_tools=TOOLS, reasoning_effort="high", state=st3)
check(calls["n"] == 2 and st3.strip_thinking and out.choices[0].message.content == "Reading now.",
      "binding mismatch: stripped thinking and retried once")

# --- cost accounting with cache terms ----------------------------------------
c = calculate_cost("anthropic/claude-sonnet-5", 94200, 400, cached_tokens=90000, cache_write_tokens=3000, provider="anthropic")
expected_prompt = (1200 * 2.0 + 90000 * 2.0 * 0.10 + 3000 * 2.0 * 1.25) / 1_000_000
check(abs(c["prompt_cost"] - expected_prompt) < 1e-12, "anthropic: uncached full, reads 10%, writes 125%")
check(abs(c["cache_savings"] - (94200 * 2.0 / 1e6 - expected_prompt)) < 1e-12, "cache_savings = full-price minus billed")
c2 = calculate_cost("openai/gpt-5.6-sol", 100000, 1000, cached_tokens=92000, provider="openai")
check(abs(c2["prompt_cost"] - (8000 * 5.0 + 92000 * 5.0 * 0.25) / 1e6) < 1e-12, "openai: cached input at 25%")
c3 = calculate_cost("google/gemini-3.7-flash", 1000, 10, cached_tokens=500, provider="openrouter")
check(abs(c3["prompt_cost"] - 1000 * 0.75 / 1e6) < 1e-12, "unknown provider cache rate: no discount assumed")
c4 = calculate_cost("openai/gpt-5.6-sol", 100000, 1000)
check(abs(c4["prompt_cost"] - 100000 * 5.0 / 1e6) < 1e-12 and c4["cached_tokens"] == 0, "legacy call shape unchanged")

# --- native client timeouts ---------------------------------------------------
# Streaming requests: `read` is the longest silence between bytes, and a
# dead read must fail in minutes, not the 30-44 minutes seen on 2026-09-03.
from utils import llm_utils as LU
_t = LU.native_anthropic_timeout()
check(_t.read == 600.0, f"native read timeout is 10 minutes (got {_t.read})")
check(_t.connect == 30.0 and _t.pool == 30.0, "connect/pool timeouts are short")
check(_t.write == 120.0, "write timeout is 2 minutes")
check(_t.read < 1800.0, "read timeout is below the old 30-minute blanket value")

# --- content-silence watchdog ------------------------------------------------
# A stream that emits one event and then blocks forever (the API keeps it
# alive with pings the SDK never surfaces) must be aborted after the silence
# limit and surface as ContentSilenceTimeout for the caller's retry path.
import threading as _th, time as _tm
from types import SimpleNamespace

class _BlockingStream:
    """One event, then block until close() is called from another thread."""
    def __init__(self):
        self._closed = _th.Event(); self.closed_calls = 0
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def __iter__(self):
        yield {"type": "message_start"}
        self._closed.wait()           # the wedge: pings only, no events
        raise RuntimeError("stream closed")  # what httpx raises after close()
    def close(self):
        self.closed_calls += 1; self._closed.set()
    def get_final_message(self): raise AssertionError("should not be reached")

class _WedgedClient:
    class messages:
        last = None
        @staticmethod
        def stream(**kwargs):
            _WedgedClient.messages.last = _BlockingStream(); return _WedgedClient.messages.last

_t0 = _tm.monotonic()
try:
    AN._send(_WedgedClient(), {"model": "m"}, silence_seconds=0.3)
    check(False, "watchdog: wedged stream must not return")
except AN.ContentSilenceTimeout as e:
    _dt = _tm.monotonic() - _t0
    check(_dt < 5.0, f"watchdog aborted a wedged stream in {_dt:.2f}s (limit 0.3s)")
    check("pings excluded" in str(e), "watchdog error names the cause")
    check(_WedgedClient.messages.last.closed_calls >= 1, "watchdog closed the stream")
check(isinstance(AN.ContentSilenceTimeout("x"), TimeoutError), "ContentSilenceTimeout is a TimeoutError")

class _HealthyStream:
    """Events keep coming; the watchdog must never trip."""
    def __init__(self): self.final = SimpleNamespace(stop_reason="end_turn", content=[], usage=None)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def __iter__(self):
        for _ in range(4):
            _tm.sleep(0.1); yield {"type": "content_block_delta"}
    def close(self): pass
    def get_final_message(self): return self.final

class _HealthyClient:
    class messages:
        @staticmethod
        def stream(**kwargs): return _HealthyStream()

_msg = AN._send(_HealthyClient(), {"model": "m"}, silence_seconds=0.3)
check(getattr(_msg, "stop_reason", None) == "end_turn", "watchdog leaves a healthy slow stream alone (events every 0.1s, limit 0.3s)")
check(AN.CONTENT_SILENCE_SECONDS == 600.0, f"default content-silence limit is 10 minutes (got {AN.CONTENT_SILENCE_SECONDS})")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("ALL ANTHROPIC-NATIVE CHECKS PASSED")
