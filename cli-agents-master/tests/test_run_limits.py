"""Run limits are pinned and recorded (2026-09-19, maintainer).

40 iterations is the default everywhere (what every v2 API cohort ran with; the
old fallback was 30), both top effort tiers get 60 minutes per model call
(xhigh had 300 s against max's 3600), both values go on every attempt row, and
a call that overruns raises instead of returning half a response.
"""
import inspect
from pathlib import Path

import pytest

from excel_cli_agent import models_config as mc
from excel_cli_agent.batch_runner import BatchRunner
from excel_cli_agent.task_executor import ExcelTaskExecutor, StreamTimeoutError, TaskExecution

PKG = Path(__file__).resolve().parents[1] / "excel_cli_agent"


def test_forty_iterations_is_the_default_everywhere():
    assert mc.DEFAULT_MAX_ITERATIONS == 40
    assert inspect.signature(TaskExecution).parameters["max_iterations"].default == 40
    for name in ("batch_runner.py", "auto_batch_runner.py", "local_batch_runner.py"):
        assert "config.setdefault('max_iterations', DEFAULT_MAX_ITERATIONS)" in (PKG / name).read_text(), name
    assert 'default=DEFAULT_MAX_ITERATIONS, help="Maximum iterations per task"' in (PKG / "cli.py").read_text()
    assert "self.default_max_iterations: int = DEFAULT_MAX_ITERATIONS" in (PKG / "task_executor.py").read_text()


def test_both_top_tiers_get_sixty_minutes_per_call():
    assert mc.resolve_api_timeout("max") == mc.resolve_api_timeout("xhigh") == 3600
    assert mc.resolve_api_timeout("high") == 240 and mc.resolve_api_timeout(None) == 180
    assert mc.resolve_api_timeout("xhigh", 900) == 900      # an explicit run-config value still wins


def test_both_limits_go_on_the_attempt_row():
    runner = BatchRunner.__new__(BatchRunner)               # no config file, no DB
    runner.config = {"reasoning_effort": "xhigh", "max_iterations": 40}
    assert runner._run_limit_extra_configs() == {"max_iterations": 40, "api_timeout_seconds": 3600}
    runner.config = {"reasoning_effort": "max", "api_timeout_seconds": 1200}
    assert runner._run_limit_extra_configs() == {"max_iterations": 40, "api_timeout_seconds": 1200}
    auto = (PKG / "auto_batch_runner.py").read_text()
    assert "cfg.update(self._run_limit_extra_configs())" in auto


def test_an_overrun_raises_instead_of_returning_half_a_response():
    executor = ExcelTaskExecutor.__new__(ExcelTaskExecutor)

    def stream_cut_by_the_alarm():
        yield type("Chunk", (), {"choices": [], "usage": None})()
        raise StreamTimeoutError("API call exceeded 3600s hard timeout - connection likely dead")

    with pytest.raises(StreamTimeoutError):
        executor._collect_stream_response(stream_cut_by_the_alarm(), timeout_seconds=3600)

    def slow_stream():
        while True:
            yield type("Chunk", (), {"choices": [], "usage": None})()

    with pytest.raises(StreamTimeoutError):
        executor._collect_stream_response(slow_stream(), timeout_seconds=-1)   # already past the limit


def test_a_long_run_of_thinking_chunks_does_not_cut_the_answer_off():
    """Grok 4.6 via Forge streams its thinking as delta.reasoning_content: 152
    such chunks arrived before the first content chunk (probed 2026-09-20).
    They are activity, not the 100 empty chunks of a dead connection."""
    executor = ExcelTaskExecutor.__new__(ExcelTaskExecutor)

    def chunk(**delta):
        d = type("Delta", (), {"content": None, **delta})()
        return type("Chunk", (), {"choices": [type("Choice", (), {"delta": d})()], "usage": None})()

    def thinking_then_answer(field):
        for _ in range(152):
            yield chunk(**{field: "thinking..."})
        yield chunk(content='{"is_complete": true}')

    for field in ("reasoning_content", "reasoning"):
        text, _ = executor._collect_stream_response(thinking_then_answer(field), timeout_seconds=3600)
        assert text == '{"is_complete": true}', field

    def dead_connection():
        for _ in range(152):
            yield chunk()
        yield chunk(content="never reached")

    text, _ = executor._collect_stream_response(dead_connection(), timeout_seconds=3600)
    assert text == ""                                      # the breaker still works


def test_gemini_keep_alive_chunks_do_not_cut_a_long_think():
    """Gemini 3.8 Flash via Forge (probed 2026-09-21) does not stream its
    thinking: Forge sends chunks carrying only extra_content (the thought
    signature) - 83 in a row on a 44k-token think - then summary and answer."""
    executor = ExcelTaskExecutor.__new__(ExcelTaskExecutor)

    def chunk(**delta):
        d = type("Delta", (), {"content": "", **delta})()
        return type("Chunk", (), {"choices": [type("Choice", (), {"delta": d})()], "usage": None})()

    def long_think():
        for _ in range(250):
            yield chunk(role="assistant", extra_content={"google": {"thought_signature": "c2ln"}})
        yield chunk(role="assistant", reasoning_content="summary", content='{"is_complete": true}')

    text, _ = executor._collect_stream_response(long_think(), timeout_seconds=3600)
    assert text == '{"is_complete": true}'

    def dead_connection():
        for _ in range(250):
            yield chunk(role="assistant")
        yield chunk(content="never reached")

    text, _ = executor._collect_stream_response(dead_connection(), timeout_seconds=3600)
    assert text == ""                                      # the breaker still works


def test_thinking_reported_outside_completion_tokens_is_still_costed():
    """xAI's usage (Grok 4.6 via Forge, probed 2026-09-20): completion 9,
    reasoning 12,655, total = prompt + both. OpenAI's already includes it."""
    executor = ExcelTaskExecutor.__new__(ExcelTaskExecutor)

    def stream(prompt, completion, total):
        usage = type("Usage", (), {"prompt_tokens": prompt, "completion_tokens": completion,
                                   "total_tokens": total})()
        yield type("Chunk", (), {"choices": [], "usage": usage})()

    _, xai = executor._collect_stream_response(stream(703, 9, 13367), timeout_seconds=3600)
    assert xai == {"prompt_tokens": 703, "completion_tokens": 12664, "total_tokens": 13367}
    _, openai = executor._collect_stream_response(stream(5000, 46823, 51823), timeout_seconds=3600)
    assert openai == {"prompt_tokens": 5000, "completion_tokens": 46823, "total_tokens": 51823}
    _, bare = executor._collect_stream_response(stream(5000, 800, None), timeout_seconds=3600)
    assert bare["completion_tokens"] == 800


def test_forge_calls_are_cut_after_ten_silent_minutes_and_nothing_else_changes():
    """2026-09-21: Forge left Grok requests unanswered (60 min, then 2.6 h, on
    task 41). Forge tries are cut at 600 s of silence and retried in the open;
    every other endpoint keeps the one 3600 s window and the SDK defaults."""
    import httpx
    from openai import DEFAULT_MAX_RETRIES

    assert mc.resolve_stall_timeout("https://api.forge.tensorblock.co/v1") == 600
    for url in ("https://api.openai.com/v1", "https://api.anthropic.com", "https://openrouter.ai/api/v1", None):
        assert mc.resolve_stall_timeout(url) is None, url

    def executor(base_url):
        return ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k",
                                 model="m", reasoning_effort="xhigh", base_url=base_url)

    import os
    os.environ.setdefault("FORGE_API_KEY", "test-key")
    forge = executor("https://api.forge.tensorblock.co/v1")
    assert forge.api_timeout.read == 600 and forge.hard_timeout_seconds == 3600
    assert forge.openai_client.max_retries == 0
    direct = executor("https://api.openai.com/v1")
    assert direct.stall_timeout_seconds is None
    assert direct.api_timeout.read == 3600 and direct.openai_client.max_retries == DEFAULT_MAX_RETRIES

    # a Forge stall in mid-stream is raised for the retry loop, not parsed as half a response
    def stalled_stream():
        yield type("Chunk", (), {"choices": [], "usage": None})()
        raise httpx.ReadTimeout("no bytes for 600 s")

    with pytest.raises(httpx.ReadTimeout):
        forge._collect_stream_response(stalled_stream(), timeout_seconds=3600)
    text, _ = direct._collect_stream_response(stalled_stream(), timeout_seconds=3600)
    assert text == ""                                       # unchanged off Forge

    runner = BatchRunner.__new__(BatchRunner)
    runner.config = {"reasoning_effort": "xhigh", "max_iterations": 40,
                     "base_url": "https://api.forge.tensorblock.co/v1"}
    assert runner._run_limit_extra_configs() == {"max_iterations": 40, "api_timeout_seconds": 3600,
                                                 "stream_stall_seconds": 600}


def test_astra_through_forge_gets_fifteen_silent_minutes_and_nothing_else_changes():
    """2026-09-22 (maintainer): gpt-6-astra through Forge sends no byte until its thinking is
    done, so its tries are cut at 900 s of silence, not 600; every other Forge model
    (they stream their thinking) keeps 600, and the direct endpoints keep no limit."""
    import os
    forge_url = "https://api.forge.tensorblock.co/v1"
    assert mc.resolve_stall_timeout(forge_url, "tensorblock/gpt-6-astra") == 900
    for model in ("tensorblock/grok-4.6", "tensorblock/Kimi-K3", "tensorblock/gemini-3.8-flash", None):
        assert mc.resolve_stall_timeout(forge_url, model) == 600, model
    assert mc.resolve_stall_timeout("https://api.openai.com/v1", "gpt-6-astra") is None

    os.environ.setdefault("FORGE_API_KEY", "test-key")
    astra = ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k",
                              model="tensorblock/gpt-6-astra", reasoning_effort="xhigh", base_url=forge_url)
    assert astra.stall_timeout_seconds == 900 and astra.api_timeout.read == 900
    assert astra.hard_timeout_seconds == 3600 and astra.openai_client.max_retries == 0

    runner = BatchRunner.__new__(BatchRunner)
    runner.config = {"reasoning_effort": "xhigh", "max_iterations": 40, "base_url": forge_url,
                     "model": "tensorblock/gpt-6-astra"}
    assert runner._run_limit_extra_configs() == {"max_iterations": 40, "api_timeout_seconds": 3600,
                                                 "stream_stall_seconds": 900}


def test_a_forge_429_or_5xx_is_retried_in_the_open_and_nothing_else_is(monkeypatch):
    """2026-09-21: with the SDK's retries off for Forge, one 429 or 5xx ended
    the attempt as needs_clarification - under one try per task, a lost task."""
    import json
    import os

    import httpx
    import openai

    from excel_cli_agent import task_executor as te

    def status_error(cls, status, text, headers=None):
        request = httpx.Request("POST", "https://api.forge.tensorblock.co/v1/chat/completions")
        return cls(text, response=httpx.Response(status, request=request, headers=headers), body=None)

    def executor(base_url):
        ex = ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k",
                               model="m", reasoning_effort="xhigh", base_url=base_url)
        ex._get_system_prompt = lambda: "system"
        ex._assemble_context = lambda task, system_prompt: "context"
        ex._log_streaming_request = lambda *a, **k: None
        return ex

    os.environ.setdefault("FORGE_API_KEY", "test-key")
    forge = executor("https://api.forge.tensorblock.co/v1")
    direct = executor("https://api.openai.com/v1")
    busy = status_error(openai.RateLimitError, 429, "rate limit")
    bad_gateway = status_error(openai.InternalServerError, 502, "upstream connect error")

    assert [forge._forge_status_retry_wait(busy, n) for n in range(5)] == [15, 30, 60, 120, 120]
    assert forge._forge_status_retry_wait(bad_gateway, 0) == 15
    assert forge._forge_status_retry_wait(status_error(openai.RateLimitError, 429, "x", {"retry-after": "90"}), 0) == 90
    assert forge._forge_status_retry_wait(status_error(openai.RateLimitError, 429, "x", {"retry-after": "9999"}), 0) == 300
    for cls, status in ((openai.BadRequestError, 400), (openai.AuthenticationError, 401), (openai.APIStatusError, 402)):
        assert forge._forge_status_retry_wait(status_error(cls, status, "no"), 0) is None, status
    assert direct._forge_status_retry_wait(busy, 0) is None          # its SDK still retries on its own
    assert forge._forge_status_retry_wait(httpx.ReadTimeout("stall"), 0) is None

    waits = []
    monkeypatch.setattr(te.time, "sleep", waits.append)
    task = TaskExecution(task_id="t", user_prompt="p", status=te.TaskStatus.IN_PROGRESS, steps=[], start_time=0.0)

    def calls(ex, *outcomes):
        queue = list(outcomes)

        def call(request_data):
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        ex._call_api_with_hard_timeout = call
        # the unstreamed fallback must never run for these
        ex.openai_client = type("Dead", (), {"chat": property(lambda self: (_ for _ in ()).throw(AssertionError("fallback ran")))})()
        return queue

    answer = json.dumps({"reasoning": "r", "actions": [], "is_complete": True})
    left = calls(forge, busy, bad_gateway, answer)
    parsed, _ = forge._reason_once(task)
    # 2026-09-24: a 429 waits at least 60 s and does not use up a try, so the 502 after it is try 1
    assert parsed["is_complete"] is True and left == [] and waits == [60, 15]

    # 2026-09-24: eight 429s in a row (more than the six tries) still end in the answer - the key's
    # rate limit is waited out, up to an hour, before the tries are counted
    del waits[:]
    left = calls(forge, *[busy] * 8, answer)
    parsed, _ = forge._reason_once(task)
    assert parsed["is_complete"] is True and left == [] and waits == [60] * 8

    # once the hour is spent, 429s count as tries again and the call fails after six
    del waits[:]
    clock = [0.0]
    monkeypatch.setattr(te.time, "time", lambda: clock[0])
    monkeypatch.setattr(te.time, "sleep", lambda secs: (waits.append(secs), clock.__setitem__(0, clock[0] + 1000)))
    left = calls(forge, *[busy] * 12, answer)
    parsed, _ = forge._reason_once(task)
    assert "rate limit" in parsed["error"] and left == [busy, busy, answer] and waits == [60] * 4 + [15, 30, 60, 120, 120]
    monkeypatch.setattr(te.time, "sleep", waits.append)

    # six in a row: the call fails with the gateway's error - not rerun unstreamed ("upstream" contains "stream")
    del waits[:]
    left = calls(forge, *[bad_gateway] * 6, answer)
    parsed, _ = forge._reason_once(task)
    assert "upstream connect error" in parsed["error"] and left == [answer] and waits == [15, 30, 60, 120, 120]

    # a 400 (bad reasoning_effort) is not retried on Forge; off Forge a 429 is not ours to retry
    del waits[:]
    left = calls(forge, status_error(openai.BadRequestError, 400, "provider rejected the request"), answer)
    parsed, _ = forge._reason_once(task)
    assert "provider rejected" in parsed["error"] and left == [answer] and waits == []
    left = calls(direct, busy, answer)
    parsed, _ = direct._reason_once(task)
    assert "rate limit" in parsed["error"] and left == [answer] and waits == []


def _tool_call_chunk(name=None, arguments=None, index=0, content=None):
    fn = type("Fn", (), {"name": name, "arguments": arguments})()
    tc = type("ToolCall", (), {"index": index, "function": fn})()
    d = type("Delta", (), {"content": content, "role": "assistant", "tool_calls": [tc]})()
    return type("Chunk", (), {"choices": [type("Choice", (), {"delta": d, "finish_reason": None})()], "usage": None})()


def _finish_chunk(reason="stop"):
    d = type("Delta", (), {"content": None, "role": "assistant"})()
    return type("Chunk", (), {"choices": [type("Choice", (), {"delta": d, "finish_reason": reason})()], "usage": None})()


def test_a_gemini_native_tool_call_is_read_as_the_same_action():
    """Gemini 3.8 Flash via Forge (2026-09-21): with no tools declared it still
    answers about half its steps with a native call ("list_files", once
    "excel:list_files") and no text. Read as the action; other models unchanged."""
    import json
    gemini = ExcelTaskExecutor.__new__(ExcelTaskExecutor)
    gemini.model = "tensorblock/gemini-3.8-flash"

    text, _ = gemini._collect_stream_response(iter([_tool_call_chunk("excel:list_files", "{}"), _finish_chunk()]), 3600)
    assert json.loads(text) == {"is_complete": False, "actions": [{"tool": "list_files", "parameters": {}}]}
    assert gemini._last_finish_reason == "stop"

    # arguments in fragments, and a second call under the same index
    chunks = [_tool_call_chunk("get_cell_range", '{"filename": "sol'), _tool_call_chunk(None, 'ution.xlsx", "range_address": "A1:B2"}'),
              _tool_call_chunk("default_api.list_worksheets", '{"filename": "solution.xlsx"}'),
              _tool_call_chunk("mcp__excel__get_used_range", '{"filename": "solution.xlsx"}'), _finish_chunk()]
    text, _ = gemini._collect_stream_response(iter(chunks), 3600)
    assert json.loads(text)["actions"] == [
        {"tool": "get_cell_range", "parameters": {"filename": "solution.xlsx", "range_address": "A1:B2"}},
        {"tool": "list_worksheets", "parameters": {"filename": "solution.xlsx"}},
        {"tool": "get_used_range", "parameters": {"filename": "solution.xlsx"}}]

    # a JSON text reply wins; other text beside a call is kept as reasoning; unparseable arguments are not guessed at
    text, _ = gemini._collect_stream_response(iter([_tool_call_chunk("list_files", "{}", content='{"is_complete": true}')]), 3600)
    assert text == '{"is_complete": true}'
    text, _ = gemini._collect_stream_response(iter([_tool_call_chunk("list_files", "{}", content="Let me look at the files.")]), 3600)
    assert json.loads(text) == {"reasoning": "Let me look at the files.", "is_complete": False,
                                "actions": [{"tool": "list_files", "parameters": {}}]}
    text, _ = gemini._collect_stream_response(iter([_tool_call_chunk("list_files", "{not json")]), 3600)
    assert text == ""

    grok = ExcelTaskExecutor.__new__(ExcelTaskExecutor)
    grok.model = "tensorblock/grok-4.6"
    text, _ = grok._collect_stream_response(iter([_tool_call_chunk("list_files", "{}"), _finish_chunk()]), 3600)
    assert text == ""


def test_an_empty_gemini_reply_is_asked_again_and_nothing_else_changes(monkeypatch):
    import json
    import os
    from excel_cli_agent import task_executor as te

    os.environ.setdefault("FORGE_API_KEY", "test-key")

    def executor(model):
        ex = ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k", model=model,
                               reasoning_effort="high", base_url="https://api.forge.tensorblock.co/v1")
        ex._get_system_prompt = lambda: "system"
        ex._assemble_context = lambda task, system_prompt: "context"
        ex._log_streaming_request = lambda *a, **k: None
        return ex

    waits = []
    monkeypatch.setattr(te.time, "sleep", waits.append)
    task = TaskExecution(task_id="t", user_prompt="p", status=te.TaskStatus.IN_PROGRESS, steps=[], start_time=0.0)
    answer = json.dumps({"actions": [{"tool": "list_files", "parameters": {}}], "is_complete": False})

    def replies(ex, *texts):
        queue = list(texts)
        ex._call_api_with_hard_timeout = lambda request_data: (queue.pop(0), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return queue

    gemini = executor("tensorblock/gemini-3.8-flash")
    left = replies(gemini, "", "  ", answer)
    parsed, _ = gemini._reason_once(task)
    assert parsed["actions"] == [{"tool": "list_files", "parameters": {}}] and left == [] and waits == [2, 2]

    del waits[:]
    left = replies(gemini, *[""] * 6, answer)                  # six empty replies: the step is lost, as before
    parsed, _ = gemini._reason_once(task)
    assert parsed.get("actions") == [] and left == [answer] and waits == [2] * 5

    del waits[:]
    grok = executor("tensorblock/grok-4.6")                    # other models: an empty reply is not asked again
    left = replies(grok, "", answer)
    parsed, _ = grok._reason_once(task)
    assert parsed.get("actions") == [] and left == [answer] and waits == []


def test_gemini_gets_the_excel_tools_declared_and_no_other_model_does():
    import os
    os.environ.setdefault("FORGE_API_KEY", "test-key")
    schemas = [{"name": "list_files", "description": "List files.", "inputSchema": {"properties": {}, "type": "object"}},
               {"name": "get_cell_range", "description": "Read cells.", "inputSchema": {"properties": {
                   "filename": {"type": "string"}}, "required": ["filename"], "type": "object"}}]
    client = type("C", (), {"storage_path": "/tmp", "tool_schemas": schemas})()
    sent = []

    def executor(model):
        ex = ExcelTaskExecutor(excel_client=client, api_key="k", model=model, reasoning_effort="high",
                               base_url="https://api.forge.tensorblock.co/v1")
        ex._get_system_prompt = lambda: "system"
        ex._assemble_context = lambda task, system_prompt: "context"
        ex._log_streaming_request = lambda *a, **k: None
        ex._call_api_with_hard_timeout = lambda request_data: (sent.append(request_data) or '{"is_complete": true}', {})
        return ex

    from excel_cli_agent import task_executor as te
    task = TaskExecution(task_id="t", user_prompt="p", status=te.TaskStatus.IN_PROGRESS, steps=[], start_time=0.0)
    executor("tensorblock/gemini-3.8-flash")._reason_once(task)
    assert sent[-1]["tools"] == [
        {"type": "function", "function": {"name": "list_files", "description": "List files.", "parameters": schemas[0]["inputSchema"]}},
        {"type": "function", "function": {"name": "get_cell_range", "description": "Read cells.", "parameters": schemas[1]["inputSchema"]}},
        ExcelTaskExecutor.COMPLETE_TASK_FUNCTION]
    for model in ("tensorblock/grok-4.6", "tensorblock/Kimi-K3"):
        executor(model)._reason_once(task)
        assert "tools" not in sent[-1], model


def test_the_tool_server_list_is_kept_with_its_schemas(tmp_path):
    import contextlib
    import io
    from excel_cli_agent.mcp_client import ExcelMCPClient
    client = ExcelMCPClient("./excel_mcp_server/server.py", str(tmp_path))
    with contextlib.redirect_stdout(io.StringIO()):
        client.connect()
        try:
            names = [t["name"] for t in client.tool_schemas]
            assert names == client.available_tools and "get_cell_range" in names
            assert all(t.get("inputSchema", {}).get("type") == "object" for t in client.tool_schemas)
        finally:
            client.disconnect()


def test_a_forge_reply_that_breaks_off_before_any_text_is_asked_again(monkeypatch):
    """2026-09-21: "TensorBlock model provider is temporarily unavailable" ended 4 Kimi calls
    mid-stream before any answer text; each came back as an empty reply and burned a step."""
    import json
    import os

    import httpx
    import openai

    from excel_cli_agent import task_executor as te

    os.environ.setdefault("FORGE_API_KEY", "test-key")

    def executor(base_url):
        ex = ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k",
                               model="tensorblock/Kimi-K3", reasoning_effort="max", base_url=base_url)
        ex._get_system_prompt = lambda: "system"
        ex._assemble_context = lambda task, system_prompt: "context"
        ex._log_streaming_request = lambda *a, **k: None
        ex._recreate_openai_client = lambda: None
        # the unstreamed fallback must never run for these
        ex.openai_client = type("Dead", (), {"chat": property(lambda self: (_ for _ in ()).throw(AssertionError("fallback ran")))})()
        return ex

    def outage(text=None):
        if text:
            d = type("Delta", (), {"content": text, "role": "assistant"})()
            yield type("Chunk", (), {"choices": [type("Choice", (), {"delta": d, "finish_reason": None})()], "usage": None})()
        raise openai.APIError("TensorBlock model provider is temporarily unavailable. Please retry shortly.",
                              httpx.Request("POST", "https://api.forge.tensorblock.co/v1/chat/completions"), body=None)

    forge = executor("https://api.forge.tensorblock.co/v1")
    direct = executor("https://api.openai.com/v1")

    with pytest.raises(te.ForgeStreamError, match="temporarily unavailable"):
        forge._collect_stream_response(outage(), 3600)
    assert forge._collect_stream_response(outage('{"is_comp'), 3600)[0] == '{"is_comp'   # text arrived: unchanged
    assert direct._collect_stream_response(outage(), 3600)[0] == ""                        # off Forge: unchanged

    waits = []
    monkeypatch.setattr(te.time, "sleep", waits.append)
    task = TaskExecution(task_id="t", user_prompt="p", status=te.TaskStatus.IN_PROGRESS, steps=[], start_time=0.0)
    answer = json.dumps({"actions": [{"tool": "list_files", "parameters": {}}], "is_complete": False})
    broke = te.ForgeStreamError("TensorBlock model provider is temporarily unavailable. Please retry shortly.")

    def calls(*outcomes):
        queue = list(outcomes)

        def call(request_data):
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        forge._call_api_with_hard_timeout = call
        return queue

    left = calls(broke, broke, answer)
    parsed, _ = forge._reason_once(task)
    assert parsed["actions"] == [{"tool": "list_files", "parameters": {}}] and left == [] and waits == [15, 30]

    # six in a row: the step gets the empty reply it always got (no failed task), never rerun unstreamed
    del waits[:]
    left = calls(*[broke] * 6, answer)
    parsed, _ = forge._reason_once(task)
    assert parsed.get("actions") == [] and "error" not in parsed and left == [answer] and waits == [15, 30, 60, 120, 120]


def test_forge_provider_rejected_400_is_retried_and_other_400s_are_not(monkeypatch):
    """2026-09-22: Forge's generic "The configured provider rejected the request" ended three Grok
    tasks mid-run (rows 2098, 2489, 2490) on single calls that succeed when asked again."""
    import json
    import os

    import httpx
    import openai

    from excel_cli_agent import task_executor as te

    os.environ.setdefault("FORGE_API_KEY", "test-key")

    def status_error(cls, status, text):
        request = httpx.Request("POST", "https://api.forge.tensorblock.co/v1/chat/completions")
        return cls(text, response=httpx.Response(status, request=request), body=None)

    def executor(base_url):
        ex = ExcelTaskExecutor(excel_client=type("C", (), {"storage_path": "/tmp"})(), api_key="k",
                               model="tensorblock/grok-4.6", reasoning_effort="xhigh", base_url=base_url)
        ex._get_system_prompt = lambda: "system"
        ex._assemble_context = lambda task, system_prompt: "context"
        ex._log_streaming_request = lambda *a, **k: None
        ex.openai_client = type("Dead", (), {"chat": property(lambda self: (_ for _ in ()).throw(AssertionError("fallback ran")))})()
        return ex

    rejected = status_error(openai.BadRequestError, 400, "Error code: 400 - {'error': {'message': 'The configured provider "
                            "rejected the request. Please check your model name and request parameters.', 'type': 'provider_error'}}")
    other_400 = status_error(openai.BadRequestError, 400, "Error code: 400 - {'error': {'message': 'reasoning_effort must be one of low, medium, high'}}")
    forge, direct = executor("https://api.forge.tensorblock.co/v1"), executor("https://api.openai.com/v1")
    assert [forge._forge_status_retry_wait(rejected, n) for n in range(5)] == [15, 30, 60, 120, 120]
    assert forge._forge_status_retry_wait(other_400, 0) is None
    assert direct._forge_status_retry_wait(rejected, 0) is None

    waits = []
    monkeypatch.setattr(te.time, "sleep", waits.append)
    task = TaskExecution(task_id="t", user_prompt="p", status=te.TaskStatus.IN_PROGRESS, steps=[], start_time=0.0)
    answer = json.dumps({"actions": [{"tool": "list_files", "parameters": {}}], "is_complete": False})

    def calls(*outcomes):
        queue = list(outcomes)

        def call(request_data):
            o = queue.pop(0)
            if isinstance(o, Exception):
                raise o
            return o, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        forge._call_api_with_hard_timeout = call
        return queue

    left = calls(rejected, answer)
    parsed, _ = forge._reason_once(task)
    assert parsed["actions"] == [{"tool": "list_files", "parameters": {}}] and left == [] and waits == [15]

    del waits[:]
    left = calls(other_400, answer)          # a real bad request still fails at once, never unstreamed
    parsed, _ = forge._reason_once(task)
    assert "reasoning_effort" in parsed["error"] and left == [answer] and waits == []


def test_only_gemini_38_flash_declares_complete_task_and_reads_it_as_completion():
    """2026-09-22: the native-tool-call gate is the exact model id, the
    declarations end with complete_task, and a complete_task call is the
    step's is_complete=true (other calls beside it run first)."""
    import json
    from excel_cli_agent.models_config import uses_gemini_tool_calls
    assert uses_gemini_tool_calls("tensorblock/gemini-3.8-flash")
    for model in ("google/gemini-3.8-flash", "tensorblock/gemini-3-pro", "tensorblock/grok-4.6", None, ""):
        assert not uses_gemini_tool_calls(model), model

    schemas = [{"name": "list_files", "description": "List files.", "inputSchema": {"properties": {}, "type": "object"}}]
    gemini = ExcelTaskExecutor.__new__(ExcelTaskExecutor)
    gemini.model = "tensorblock/gemini-3.8-flash"
    gemini.excel_client = type("C", (), {"tool_schemas": schemas})()
    declared = gemini._gemini_tool_declarations()
    assert [d["function"]["name"] for d in declared] == ["list_files", "complete_task"]
    assert declared[-1]["function"]["parameters"]["required"] == ["completion_summary"]

    chunks = [_tool_call_chunk("complete_task", '{"completion_summary": "Model built."}'), _finish_chunk()]
    text, _ = gemini._collect_stream_response(iter(chunks), 3600)
    assert json.loads(text) == {"is_complete": True, "actions": [], "completion_summary": "Model built."}

    chunks = [_tool_call_chunk("list_files", "{}", index=0), _tool_call_chunk("complete_task", '{"completion_summary": "Done"}', index=1),
              _finish_chunk()]
    text, _ = gemini._collect_stream_response(iter(chunks), 3600)
    assert json.loads(text) == {"is_complete": True, "actions": [{"tool": "list_files", "parameters": {}}],
                                "completion_summary": "Done"}
