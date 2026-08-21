"""Offline checks on where an attempt's files go. No DB, AWS, or browser.

One attempt = one staging directory + one S3 prefix holding everything it
produced. Getting the custody rule wrong deletes the only copy of a
workbook, so it is pinned here.

Run from gui-agents-master:  python tests/test_attempt_files_offline.py
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra.run import _clear_staging, _staging_dir, collect_log_files  # noqa: E402
from task_io.base import AttemptResult  # noqa: E402
from task_io.sinks.local_sink import LocalAttemptSink  # noqa: E402
from task_io.sinks.postgres_s3 import (  # noqa: E402
    MBABenchV2PostgresS3AttemptSink,
    PostgresS3AttemptSink,
)
from claude_web_agent.claude_web_engine import (  # noqa: E402
    create_run_directory,
    default_run_dir,
    resolve_log_dir,
)


def populate(run_dir: Path) -> None:
    """Everything one attempt leaves behind: the engine's downloads and logs
    plus the prompts JSON infra/run.py writes."""
    create_run_directory(run_dir)
    (run_dir / "logs" / "conversations").mkdir(parents=True, exist_ok=True)
    (run_dir / "solutions" / "20260820_Task_Solution_Model.xlsx").write_text("x")
    (run_dir / "json_logs" / "completion_claude_web_1.json").write_text("{}")
    (run_dir / "json_logs" / "completion_claude_web_2.json").write_text("{}")
    (run_dir / "logs" / "claude_web_20260820_Task.log").write_text("log")
    (run_dir / "logs" / "conversations" / "conversation_x.json").write_text("{}")
    (run_dir / "prompts_Task_20260820.json").write_text("{}")


def result_for(run_dir: Path) -> AttemptResult:
    return AttemptResult(
        task_id="2",
        task_name="Task",
        agent_model_name="claude_haiku_4_5",
        prompt_version=200,
        status="success",
        solution_file=run_dir / "solutions" / "20260820_Task_Solution_Model.xlsx",
        log_files=collect_log_files(run_dir),
        started_at="",
        finished_at="",
        duration_seconds=1.0,
        prompt_files=[run_dir / "prompts_Task_20260820.json"],
    )


def check_collect_log_files(run_dir: Path) -> None:
    got = [p.name for p in collect_log_files(run_dir)]
    want = [
        "completion_claude_web_1.json",  # one per agent attempt
        "completion_claude_web_2.json",
        "conversation_x.json",  # chat transcript
        "claude_web_20260820_Task.log",  # runtime log
    ]
    assert got == want, got
    print(f"OK  collect_log_files -> {len(got)} logs, in upload order")


def check_everything_uploads(run_dir: Path) -> None:
    """The workbook, every log, and the prompts JSON all reach S3."""
    sink = MBABenchV2PostgresS3AttemptSink.__new__(MBABenchV2PostgresS3AttemptSink)
    sink.s3_prefix = "MBABenchV2/attempts"
    sink.agent_folder = "claude_haiku_4_5"
    sink.run_id = "4aa375a5"
    result = result_for(run_dir)

    attempt = MBABenchV2PostgresS3AttemptSink._attempt_files_to_upload(sink, result)
    prompts = MBABenchV2PostgresS3AttemptSink._prompt_files_to_upload(sink, result)
    assert [p.name for p in attempt] == [
        "20260820_Task_Solution_Model.xlsx",
        "completion_claude_web_1.json",
        "completion_claude_web_2.json",
        "conversation_x.json",
        "claude_web_20260820_Task.log",
    ], attempt
    assert [p.name for p in prompts] == ["prompts_Task_20260820.json"], prompts

    # All six land in the one per-attempt prefix.
    base = sink._s3_base_key(result, "20260820_120000")
    keys = [sink._file_key(base, p) for p in attempt + prompts]
    prefix = "MBABenchV2/attempts/claude_haiku_4_5/Task/20260820_120000_4aa375a5/"
    assert all(k.startswith(prefix) for k in keys), keys
    print(f"OK  {len(keys)} files upload to one attempt prefix")


def check_custody() -> None:
    """Staging is disposable only for a sink that copied the files away."""
    assert PostgresS3AttemptSink.retains_files is True
    assert LocalAttemptSink.retains_files is False

    for retains, should_exist in ((False, True), (True, False)):
        run_dir = Path(tempfile.mkdtemp()) / "attempt"
        populate(run_dir)
        _clear_staging(run_dir, NS(retains_files=retains))
        assert run_dir.exists() is should_exist, (retains, should_exist)
    print("OK  staging released after upload, kept when only paths were recorded")


def check_log_dir_follows_run_dir() -> None:
    """Pinned run_dir keeps the logs with the attempt; standalone runs use
    the configured log_directory."""
    assert resolve_log_dir({"run_dir": "/x/y"}, {}, "claude_web_logs") == Path(
        "/x/y/logs"
    )
    agent_cfg = {"logging": {"log_directory": "claude_web_logs"}}
    assert resolve_log_dir({}, agent_cfg, "claude_web_logs") == Path("claude_web_logs")
    assert str(default_run_dir(".", "claudeGUI")).endswith("_claudeGUI")
    print("OK  engine log dir follows run_dir, falls back when standalone")


def check_staging_dir_is_per_attempt() -> None:
    cfg = NS(paths=NS(scratch_dir="scratch/gui-agents"))
    from datetime import datetime

    a = _staging_dir(cfg, "Grapes of Wrath", datetime(2026, 8, 20, 12, 0, 0))
    b = _staging_dir(cfg, "Grapes of Wrath", datetime(2026, 8, 20, 12, 0, 1))
    assert a != b, "two attempts must not share a directory"
    assert a.name == "20260820_120000_Grapes_of_Wrath", a
    assert a.parent == Path("scratch/gui-agents/attempts"), a
    print(f"OK  staging dir per attempt -> {a}")


def main() -> int:
    run_dir = Path(tempfile.mkdtemp()) / "attempt"
    populate(run_dir)
    check_collect_log_files(run_dir)
    check_everything_uploads(run_dir)
    check_custody()
    check_log_dir_follows_run_dir()
    check_staging_dir_is_per_attempt()
    print("\nALL ATTEMPT-FILE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
