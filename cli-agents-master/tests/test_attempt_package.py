"""Offline tests for the local attempt record and the v2 S3 key layout.

No S3, DB, or network — everything runs against tmp_path.
"""

import pytest

from excel_cli_agent import auto_batch_runner as abr


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(abr, "RUN_LOGS_DIR", tmp_path / "run_logs")
    cfg_file = tmp_path / "my_batch.yaml"
    cfg_file.write_text("model: claude-haiku-4-5\n")
    r = abr.AutoBatchRunner(str(cfg_file), server_path="srv", api_key="key")
    r.config = {"model": "claude-haiku-4-5", "benchmark": "v2"}
    return r


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "agent_logs" / "task_11").mkdir(parents=True)
    (ws / "CorpBondII.xlsx").write_bytes(b"start")
    (ws / "solution.xlsx").write_bytes(b"sol")
    (ws / "agent_logs" / "openai_requests.csv").write_text("csv")
    (ws / "agent_logs" / "task_11" / "transcript.md").write_text("t")
    (ws / "agent_logs" / "task_11" / "task.json").write_text("{}")
    return ws


def test_package_contains_everything_reproducible(runner, workspace):
    pkg = runner._build_attempt_package(workspace, "20260823_170000")

    assert pkg == abr.RUN_LOGS_DIR / "attempt-claude-haiku-4-5-20260823_170000"
    # workspace files (starting file + solution) at the package root
    assert (pkg / "CorpBondII.xlsx").read_bytes() == b"start"
    assert (pkg / "solution.xlsx").exists()
    # full agent_logs tree
    assert (pkg / "agent_logs" / "openai_requests.csv").exists()
    assert (pkg / "agent_logs" / "task_11" / "transcript.md").exists()
    assert (pkg / "agent_logs" / "task_11" / "task.json").exists()
    # the exact prompts this process would run with
    assert (pkg / "prompts" / abr.SYSTEM_PROMPT_PATH.name).exists()
    assert (pkg / "prompts" / abr.TASK_TEMPLATE_FMWC_PATH.name).exists()
    # the batch config the run was launched with
    assert (pkg / "config" / "my_batch.yaml").exists()


def test_model_slug_flattens_provider_prefix(runner):
    runner.config["model"] = "openai/gpt-5.2"
    assert runner._model_slug() == "openai_gpt-5.2"
    runner.config["model"] = "claude-haiku-4-5"
    assert runner._model_slug() == "claude-haiku-4-5"


def test_v2_upload_selects_solution_as_only_workbook(runner, workspace):
    pkg = runner._build_attempt_package(workspace, "20260823_170000")
    uploads = runner._v2_upload_files(pkg)
    names = [rel for _, rel in uploads]

    # solution.xlsx is the one workbook, listed first
    assert names[0] == "solution.xlsx"
    workbooks = [n for n in names if n.lower().endswith((".xlsx", ".xlsm", ".xlsb", ".xls"))]
    assert workbooks == ["solution.xlsx"]
    # the starting file stays local-only (recoverable via tasks.task_starting_files)
    assert "CorpBondII.xlsx" not in names
    # logs, prompts, and config still upload
    assert "agent_logs/openai_requests.csv" in names
    assert f"prompts/{abr.SYSTEM_PROMPT_PATH.name}" in names
    assert "config/my_batch.yaml" in names


def test_v2_upload_without_solution_carries_no_workbook(runner, workspace):
    (workspace / "solution.xlsx").unlink()
    pkg = runner._build_attempt_package(workspace, "20260823_170000")
    names = [rel for _, rel in runner._v2_upload_files(pkg)]
    assert not any(n.lower().endswith((".xlsx", ".xlsm", ".xlsb", ".xls")) for n in names)
    assert "agent_logs/task_11/transcript.md" in names


def test_v2_keys_mirror_package_layout(runner, workspace):
    pkg = runner._build_attempt_package(workspace, "20260823_170000")
    s3_base = (f"MBABenchV2/attempts/cli_agents/{runner._model_slug()}"
               f"/task_id=11/20260823_170000")
    keys = [f"{s3_base}/{rel}" for _, rel in runner._v2_upload_files(pkg)]
    root = "MBABenchV2/attempts/cli_agents/claude-haiku-4-5/task_id=11/20260823_170000"
    assert f"{root}/solution.xlsx" in keys
    assert f"{root}/agent_logs/task_11/transcript.md" in keys
    assert f"{root}/prompts/{abr.SYSTEM_PROMPT_PATH.name}" in keys
    assert f"{root}/config/my_batch.yaml" in keys
    # no key escapes the per-attempt folder
    assert all(k.startswith(f"{root}/") for k in keys)
