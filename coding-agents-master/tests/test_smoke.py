"""Offline smoke tests — no Docker, no DB, no S3, no API keys.

Run:  python3 tests/test_smoke.py   (or pytest tests/)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_agent.config import load_config  # noqa: E402
from coding_agent.prompt_builder import build_prompt, parse_prompt_version, template_name  # noqa: E402
from coding_agent.sandbox import SandboxResult  # noqa: E402
from coding_agent.task_source import ExternalSource  # noqa: E402
from coding_agent.telemetry import parse_transcript  # noqa: E402
from coding_agent.validate import validate  # noqa: E402
from coding_agent.workspace import create_attempt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_external_task(tmp: Path) -> Path:
    task = tmp / "my_task"
    (task / "starting_files").mkdir(parents=True)
    (task / "task.yaml").write_text("task_name: smoke\ntask_source: wsp\n")
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = "input"
    wb.save(task / "starting_files" / "input.xlsx")
    return task


def sandbox_result(tmp: Path, exit_code=0, duration=600.0, timed_out=False) -> SandboxResult:
    transcript = tmp / "transcript.jsonl"
    stderr = tmp / "agent_stderr.log"
    transcript.touch()
    stderr.touch()
    return SandboxResult(exit_code, duration, timed_out, transcript, stderr)


def test_config_and_prompt_versions():
    cfg = load_config(ROOT / "run_configs" / "example_fable.yaml")
    assert cfg.agent.cli == "claude" and cfg.mode == "internal"
    assert cfg.api_key_env == "ANTHROPIC_API_KEY"
    assert "api.anthropic.com" in cfg.allowed_domains
    assert template_name("modeloff", "v6") == "task_template_fmwc_v6.txt"
    assert template_name("wsp", "v5") == "task_template_wsp_v5.txt"
    assert template_name("fmwc", "v7") == template_name("wsp", "v7") == "task_template_shared_v7.txt"
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_fmwc_v6.txt") == 106
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_wsp_v5.txt") == 105
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v7.txt") == 107
    print("ok: config + prompt versions")


def test_workspace_prompt_and_validation():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = load_config(ROOT / "run_configs" / "example_external.yaml")
        cfg.workspaces_dir = tmp / "workspaces"
        spec = ExternalSource(make_external_task(tmp)).fetch(tmp / "_staging")
        attempt = create_attempt(cfg.workspaces_dir, spec)
        assert (attempt.workspace / "starting_files" / "input.xlsx").exists()
        assert len(attempt.manifest) == 1

        prompt, pv = build_prompt(cfg, spec, attempt.workspace)
        assert pv == 107  # default = v7 GUI-pv9 mirror, task-invariant
        assert "solution.xlsx" in prompt and "Summary" in prompt and "ACCURACY" in prompt
        assert (attempt.workspace / "PROMPT.md").exists()

        # No solution -> agent_failure
        v = validate(attempt, sandbox_result(tmp, exit_code=1), junk_seconds=180)
        assert v.status == "agent_failure", v

        # Copied input masquerading as solution -> agent_failure (hash match)
        import shutil
        shutil.copy2(attempt.workspace / "starting_files" / "input.xlsx",
                     attempt.workspace / "solution.xlsx")
        v = validate(attempt, sandbox_result(tmp), junk_seconds=180)
        assert v.status == "agent_failure" and "identical" in v.reason, v

        # Genuinely new workbook, decent duration -> success
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active["B2"] = "=SUM(A1:A10)"
        wb.save(attempt.workspace / "solution.xlsx")
        v = validate(attempt, sandbox_result(tmp), junk_seconds=180)
        assert v.status == "success", v

        # Same but too fast -> needs_review
        v = validate(attempt, sandbox_result(tmp, duration=42.0), junk_seconds=180)
        assert v.status == "needs_review", v

        # Timeout -> timeout, partial kept
        v = validate(attempt, sandbox_result(tmp, exit_code=None, timed_out=True), junk_seconds=180)
        assert v.status == "timeout" and v.solution_path is not None, v
    print("ok: workspace + prompt + validation verdicts")


def test_telemetry_parsers():
    claude = parse_transcript(FIXTURES / "claude_stream.jsonl", "claude")
    assert claude["cost_usd"] == 1.2345
    assert len(claude["turns"]) == 2
    assert claude["turns"][0]["output_tokens"] == 50
    codex = parse_transcript(FIXTURES / "codex_events.jsonl", "codex")
    assert codex["totals"].get("output_tokens") == 900
    assert codex["totals"].get("reasoning_output_tokens") == 650
    assert codex["totals"].get("cache_write_tokens") == 500
    assert codex["cost_usd"] is None
    print("ok: telemetry parsers")


if __name__ == "__main__":
    test_config_and_prompt_versions()
    test_workspace_prompt_and_validation()
    test_telemetry_parsers()
    print("ALL SMOKE TESTS PASSED")
