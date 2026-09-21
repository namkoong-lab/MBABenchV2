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
