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
    assert cfg.agent.model == "claude-fable-5" and cfg.agent.effort == "max"  # from agent_identities.yaml
    assert cfg.agent_model_name == "claudecode_anthropic/claude-fable-5-max"
    assert cfg.api_key_env == "ANTHROPIC_API_KEY"
    assert "api.anthropic.com" in cfg.allowed_domains
    assert template_name("modeloff", "v6") == "task_template_fmwc_v6.txt"
    assert template_name("wsp", "v5") == "task_template_wsp_v5.txt"
    assert template_name("fmwc", "v7") == template_name("wsp", "v7") == "task_template_shared_v7.txt"
    assert template_name("fmwc", "v9") == template_name("wsp", "v9") == "task_template_shared_v9.txt"
    assert template_name("jp", "v10") == "task_template_shared_v10.txt"
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_fmwc_v6.txt") == 106
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_wsp_v5.txt") == 105
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v7.txt") == 107
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v9.txt") == 109
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v10.txt") == 110
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


def test_scrubbed_templates_v10_v11():
    """Rubric-effect experiment: v10/v11 carry no rubric wording, v11 stages
    HOUSE_STANDARDS.md into the workspace root, both are md5-pinned and
    reproducible from the generator."""
    import hashlib
    import subprocess
    from coding_agent.prompt_builder import (SCRUBBED_FORBIDDEN, SCRUBBED_MD5, TEMPLATE_EXTRAS,
                                             prompt_extra_paths, prompt_extras_provenance)
    prompts = ROOT / "coding_agent" / "prompts"
    for name, pin in SCRUBBED_MD5.items():
        text = (prompts / name).read_text()
        assert hashlib.md5(text.encode()).hexdigest() == pin, f"{name} drifted from its pin"
        for word in SCRUBBED_FORBIDDEN:
            assert word.lower() not in text.lower(), f"{name} still says {word!r}"
        # what must survive the scrub: task mechanics, not grading
        for keep in ("Questions", "Answers", "solution.xlsx", "Summary", "live Excel formula",
                     "WORKING EFFICIENTLY", "EXCEL FILE VALIDITY REQUIREMENTS", "Step 3 - Verify and deliver"):
            assert keep in text, f"{name} lost {keep!r}"
    v10 = (prompts / "task_template_shared_v10.txt").read_text()
    v11 = (prompts / "task_template_shared_v11.txt").read_text()
    assert "HOUSE_STANDARDS.md" not in v10 and v11.count("HOUSE_STANDARDS.md") == 2
    assert v11.replace("HOUSE STANDARDS\n- The workspace root contains HOUSE_STANDARDS.md", "") != v11
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v10.txt") == 110
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v11.txt") == 111
    assert template_name("fmwc", "v11") == template_name("wsp", "v11") == "task_template_shared_v11.txt"
    assert TEMPLATE_EXTRAS == {"v11": [("house_standards_v1.md", "HOUSE_STANDARDS.md")],
                               "v13": [("house_standards_v1.md", "HOUSE_STANDARDS.md")]}
    # v13 = v11 byte-identical under a new number (the rerun default)
    v13 = (prompts / "task_template_shared_v13.txt").read_text()
    assert v13 == v11 and SCRUBBED_MD5["task_template_shared_v13.txt"] == SCRUBBED_MD5["task_template_shared_v11.txt"]
    assert parse_prompt_version("system_prompt_coding_v1.txt", "task_template_shared_v13.txt") == 113
    assert template_name("jp", "v13") == "task_template_shared_v13.txt"
    r13 = subprocess.run([sys.executable, str(ROOT / "tools" / "build_v13_template.py"), "--check"],
                         capture_output=True, text=True)
    assert r13.returncode == 0, r13.stdout + r13.stderr
    assert (prompts / "house_standards_v1.md").exists()
    # One standards text everywhere: the copy staged into v11 workspaces must be
    # byte-identical to the monorepo's canonical house_standards/ file (the
    # one every other pipeline delivers). A drift here would hand the coding
    # cohort different conventions from the rest of the study.
    canonical = ROOT.parent / "house_standards" / "House_Standards_v1.md"
    assert (prompts / "house_standards_v1.md").read_bytes() == canonical.read_bytes(), \
        "coding_agent/prompts/house_standards_v1.md drifted from house_standards/House_Standards_v1.md"
    assert "under five seconds" not in canonical.read_text()
    # generator is deterministic and the committed files match it
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_v10_v11_templates.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        spec = ExternalSource(make_external_task(tmp)).fetch(tmp / "_staging")
        for version, pv in (("v10", 110), ("v11", 111), ("v13", 113)):
            cfg = load_config(ROOT / "run_configs" / "example_external.yaml")
            cfg.template_version = version
            cfg.workspaces_dir = tmp / f"ws_{version}"
            attempt = create_attempt(cfg.workspaces_dir, spec)
            prompt, got = build_prompt(cfg, spec, attempt.workspace, attempt=attempt)
            assert got == pv
            assert "ACCURACY" not in prompt and "== FULL RUBRIC" not in prompt
            house = attempt.workspace / "HOUSE_STANDARDS.md"
            if version in ("v11", "v13"):
                assert house.exists() and house.read_text() == (prompts / "house_standards_v1.md").read_text()
                assert "- HOUSE_STANDARDS.md (" in prompt  # listed under WORKSPACE FILES
                assert "HOUSE_STANDARDS.md" in attempt.manifest  # seeded, so validation knows
                manifest_on_disk = json.loads((attempt.attempt_dir / "manifest.json").read_text())
                assert manifest_on_disk["HOUSE_STANDARDS.md"] == attempt.manifest["HOUSE_STANDARDS.md"]
                prov = prompt_extras_provenance(cfg)
                assert prov["HOUSE_STANDARDS.md"]["source"] == "house_standards_v1.md"
                assert len(prov["HOUSE_STANDARDS.md"]["sha256"]) == 64
                assert [ws for _, ws in prompt_extra_paths(cfg)] == ["HOUSE_STANDARDS.md"]
                # the row also carries house_standards, like cli/excel rows do
                hs = cfg.extra_configs()["house_standards"]
                assert hs == {"version": 1, "file": "House_Standards_v1.md", "delivered_as": "HOUSE_STANDARDS.md",
                              "sha256": hashlib.sha256((prompts / "house_standards_v1.md").read_bytes()).hexdigest()}
                # the house file is not an xlsx, so validation still needs a real workbook
                v = validate(attempt, sandbox_result(tmp, exit_code=1), junk_seconds=180)
                assert v.status == "agent_failure", v
            else:
                assert not house.exists() and "HOUSE_STANDARDS" not in prompt
                assert prompt_extras_provenance(cfg) == {}
                assert "house_standards" not in cfg.extra_configs()
    print("ok: scrubbed templates v10/v11/v13 + house-standards staging")


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
    test_scrubbed_templates_v10_v11()
    test_telemetry_parsers()
    print("ALL SMOKE TESTS PASSED")
