"""Run limits are pinned and recorded (2026-09-19, Pat).

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
    assert parsed["is_complete"] is True and left == [] and waits == [15, 30]

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

    # text wins when there is any; unparseable arguments are not guessed at
    text, _ = gemini._collect_stream_response(iter([_tool_call_chunk("list_files", "{}", content='{"is_complete": true}')]), 3600)
    assert text == '{"is_complete": true}'
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
