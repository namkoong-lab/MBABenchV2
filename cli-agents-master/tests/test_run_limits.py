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
