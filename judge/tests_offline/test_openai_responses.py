"""Offline tests for the /v1/responses adapter (utils/openai_responses.py).

Run from judge/:  python tests_offline/test_openai_responses.py
No network. A fake client verifies the request translation; fake response
objects verify the shim and the reasoning side-table, including eviction.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

from utils import openai_responses as oa  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)
    else:
        print("OK ", msg)


CHAT_TOOLS = [{
    "type": "function",
    "function": {"name": "record_check", "description": "d",
                 "parameters": {"type": "object", "properties": {}}},
}]

# ---------------------------------------------------------------------------
# Routing rule
# ---------------------------------------------------------------------------
check(oa.wants_responses_api("openai", "xhigh"), "openai+xhigh -> responses")
check(oa.wants_responses_api("openai", "low"), "openai+low -> responses")
check(not oa.wants_responses_api("openai", "none"), "openai+none stays chat")
check(not oa.wants_responses_api("openai", None), "openai+None stays chat")
check(not oa.wants_responses_api("anthropic", "max"), "anthropic never routes")
check(not oa.wants_responses_api("openrouter", "high"), "openrouter never routes")

# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------
rt = oa.convert_tools(CHAT_TOOLS)
check(rt == [{"type": "function", "name": "record_check", "description": "d",
              "parameters": {"type": "object", "properties": {}}}],
      "chat tool -> responses tool (flattened)")

# ---------------------------------------------------------------------------
# Message conversion: dicts, images, shim assistant turns, tool results
# ---------------------------------------------------------------------------
state = oa.ReasoningState()

# A fake prior assistant turn as the adapter would have built it
from openai.types.chat import ChatCompletionMessage  # noqa: E402
from openai.types.chat.chat_completion_message_function_tool_call import (  # noqa: E402
    ChatCompletionMessageFunctionToolCall, Function,
)

tc = ChatCompletionMessageFunctionToolCall(
    id="call_1", type="function",
    function=Function(name="record_check", arguments='{"check":"1"}'))
assistant = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tc])
state.by_first_call_id["call_1"] = [
    {"type": "reasoning", "id": "rs_1", "encrypted_content": "SECRET",
     "summary": []},
]

messages = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]},
    assistant,
    {"role": "tool", "tool_call_id": "call_1", "content": "Recorded."},
    {"role": "user", "content": "continue"},
]
items = oa.convert_messages(messages, state)
kinds = [i.get("type") or i.get("role") for i in items]
check(kinds == ["system", "user", "reasoning", "function_call",
                "function_call_output", "user"],
      f"conversion order (reasoning precedes its call): {kinds}")
check(items[1]["content"][1]["type"] == "input_image", "image part converted")
check(items[2]["encrypted_content"] == "SECRET", "reasoning item re-emitted")
check(items[3] == {"type": "function_call", "call_id": "call_1",
                   "name": "record_check", "arguments": '{"check":"1"}'},
      "function_call item exact")
check(items[4] == {"type": "function_call_output", "call_id": "call_1",
                   "output": "Recorded."}, "tool result item exact")

# Eviction: drop the assistant turn + tool result -> reasoning disappears too
evicted = [messages[0], messages[1], messages[4]]
items2 = oa.convert_messages(evicted, state)
check(all(i.get("type") not in ("reasoning", "function_call") for i in items2),
      "evicted turn's reasoning + calls are not emitted")

# ---------------------------------------------------------------------------
# create(): fake client -> shim shape, reasoning capture, usage mapping
# ---------------------------------------------------------------------------
class _FakeItem(SimpleNamespace):
    def model_dump(self, **kw):
        d = dict(vars(self))
        return d


class _FakeResponse:
    def __init__(self):
        self.output = [
            _FakeItem(type="reasoning", id="rs_9", encrypted_content="ENC",
                      summary=[]),
            _FakeItem(type="function_call", call_id="call_9",
                      name="record_check", arguments='{"check":"2"}'),
            _FakeItem(type="message", content=[SimpleNamespace(text="note")]),
        ]
        self.usage = SimpleNamespace(input_tokens=100, output_tokens=30,
                                     total_tokens=130)

    def model_dump(self, **kw):
        return {"fake": True}


class _FakeClient:
    def __init__(self):
        self.last_kwargs = None
        self.responses = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse()


fc = _FakeClient()
st = oa.ReasoningState()
shim = oa.create(fc, model="gpt-5.6-sol", messages=messages,
                 chat_tools=CHAT_TOOLS, reasoning_effort="xhigh", state=st)

check(fc.last_kwargs["reasoning"] == {"effort": "xhigh"}, "effort sent")
check(fc.last_kwargs["store"] is False, "store=False (stateless)")
check(fc.last_kwargs["include"] == ["reasoning.encrypted_content"],
      "encrypted reasoning requested")
msg = shim.choices[0].message
check(msg.role == "assistant" and msg.tool_calls[0].function.name == "record_check",
      "shim message carries a real tool call")
check(msg.tool_calls[0].id == "call_9", "call id mapped")
check(msg.content == "note", "assistant text mapped")
check(shim.usage.prompt_tokens == 100 and shim.usage.completion_tokens == 30
      and shim.usage.total_tokens == 130, "usage mapped to chat names")
check(st.by_first_call_id["call_9"][0]["encrypted_content"] == "ENC",
      "reasoning captured for re-emission")
check(bool(shim.choices), "shim passes the empty-choices guard")
dump = shim.model_dump()
check(dump["adapter"] == "openai_responses" and dump["usage"]["prompt_tokens"] == 100,
      "shim model_dump for the trajectory recorder")

# The shim message survives judge.py's _msg_to_dict path (model_dump exists)
check(callable(getattr(msg, "model_dump", None)), "shim msg has model_dump")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("ALL RESPONSES-ADAPTER CHECKS PASSED")
