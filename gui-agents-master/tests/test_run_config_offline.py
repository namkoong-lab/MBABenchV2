"""Offline pipeline checks: config merge -> engine config -> preflight,
for both benchmarks. No DB, AWS, or browser access.

Run from gui-agents-master:  python -m pytest tests/test_run_config_offline.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra.configs import load_configs  # noqa: E402
from infra.configs.agent_identity import resolve_agent_identity  # noqa: E402
from infra.run import build_engine_config, preflight_check  # noqa: E402
from claude_web_agent.claude_web_engine import resolve_prompts  # noqa: E402


def fake_spec(name="TestTask"):
    return NS(
        task_name=name,
        task_id=7,
        upload_files=[],
        solution_name=None,
        metadata={"task_source": "wsp", "db_task_id": 7},
        overrides={},
    )


def test_v1():
    # Same shape as the untracked prod_single_claude_*.yaml configs, minus
    # machine secrets — validates the merge path those configs rely on.
    cfg = load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={
            "benchmark": "v1",
            # No prompts_file: 9 selects the pv9 payload via the registry.
            "prompt_version": 9,
            "source": {"kind": "postgres_s3", "schema": "bizbench"},
            "sink": {"kind": "postgres_s3", "schema": "bizbench"},
            "provider": {"kind": "claude"},
            "claude_web": {
                "mode": "cowork",
                "cowork_approval": "auto",
                "model": "fable_5",
                "effort": "max",
                "project_id": "test-project",
            },
        },
    )
    assert cfg.benchmark == "v1", cfg.benchmark
    assert cfg.source.schema == "bizbench"
    ident = resolve_agent_identity(cfg)
    assert ident.model_name == "claude_web_cowork_fable5_max", ident
    ec = build_engine_config(cfg, fake_spec(), ident.agent_folder)
    # The identity, not the provider lane, names the solution file.
    assert ec["claude_web"]["session"]["agent_name"] == ident.agent_folder, ec
    assert ec["prompts_file"] == [
        "tasks_configs/prompts_pv9/SHARED_pv9_prompt.txt"
    ], ec.get("prompts_file")
    errors = preflight_check(ec, "claude", cfg.benchmark)
    assert not errors, errors
    # engine-side expansion: 1 file -> 1 prompt, correct length
    resolved = resolve_prompts(dict(ec))
    assert len(resolved["prompts"]) == 1
    assert len(resolved["prompts"][0]) > 13000, len(resolved["prompts"][0])


def test_v2():
    cfg = load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={
            "benchmark": "v2",
            "provider": {"kind": "claude"},
            "claude_web": {"model": "fable_5", "project_id": "test-project"},
            "source": {"kind": "yaml"},
            "sink": {"kind": "local"},
        },
    )
    assert cfg.benchmark == "v2"
    ident = resolve_agent_identity(cfg)
    assert ident.model_name == "claude_fable_5_max", ident
    ec = build_engine_config(cfg, fake_spec(), ident.agent_folder)
    assert ec["claude_web"]["session"]["agent_name"] == "claude_fable_5_max", ec
    errors = preflight_check(ec, "claude", cfg.benchmark)
    assert not errors, errors
    resolved = resolve_prompts(dict(ec))
    assert len(resolved["prompts"]) == 3, len(resolved["prompts"])
    assert "132 checks" in resolved["prompts"][1]


def test_v1_missing_axes_fails():
    """A v1 run config that forgets the axes must fail preflight, not run."""
    cfg = load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={
            "benchmark": "v1",
            "provider": {"kind": "chatgpt"},
            "prompt_version": 9,  # pv9 single prompt
            "chatgpt_web": {
                "project_id": "x",
                "mode": "work",
                "model": "gpt_5_6_sol",
                "effort": "nonsense",  # invalid work effort
            },
            "source": {"kind": "yaml"},
            "sink": {"kind": "local"},
        },
    )
    ec = build_engine_config(cfg, fake_spec())
    errors = preflight_check(ec, "chatgpt", cfg.benchmark)
    assert any("effort" in e for e in errors), errors


# --- credential resolution (task_io/registry.py) ----------------------------
#
# The database url is keyed by benchmark in the monorepo config, so a run
# config cannot name one experiment and write to the other's DB. These checks
# run against a temp config dir — never the operator's real credentials.
#
# They need the monorepo `config` module on the path, which comes from the
# workspace install at the repo root. A worker box installs only
# gui-agents-master's dependencies and reads credentials from
# /etc/gui-agents/secrets.env instead, so `_repo_value` returning None there
# is the designed behavior rather than a failure — skip instead of failing.

needs_repo_config = pytest.mark.skipif(
    find_spec("config") is None,
    reason="monorepo `config` module not installed (run pytest from the "
    "workspace environment to exercise credential resolution)",
)

TEMP_CONFIG = {
    "database": {
        "v1_url": "postgresql://u:p@host/BizbenchV1?sslmode=require",
        "v2_url": "postgresql://u:p@host/MBABenchV2?sslmode=require",
    },
    "aws": {
        "s3_bucket": "mbabench",
        "access_key_id": "AKIAFAKE",
        "secret_access_key": "secretfake",
    },
}


@contextmanager
def temp_repo_config(data: dict | None):
    """Point the monorepo-config lookup at a throwaway dir (or a missing one)."""
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td) / "config"
        if data is not None:
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config_default.yaml").write_text("{}\n")
            (cfg_dir / "config.yaml").write_text(yaml.safe_dump(data))
        prev = os.environ.get("MBABENCH_CONFIG_DIR")
        os.environ["MBABENCH_CONFIG_DIR"] = str(cfg_dir)
        try:
            yield cfg_dir
        finally:
            if prev is None:
                os.environ.pop("MBABENCH_CONFIG_DIR", None)
            else:
                os.environ["MBABENCH_CONFIG_DIR"] = prev


def _cfg(benchmark: str, **extra):
    return load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={"benchmark": benchmark, **extra},
    )


@needs_repo_config
def test_db_url_follows_benchmark():
    from task_io.registry import _resolve_db_url, describe_database_target

    with temp_repo_config(TEMP_CONFIG):
        assert "BizbenchV1" in _resolve_db_url(_cfg("v1"))
        assert "MBABenchV2" in _resolve_db_url(_cfg("v2"))
        # The log line names the DB and never leaks the password.
        desc = describe_database_target(_cfg("v2"))
        assert desc.startswith("MBABenchV2 "), desc
        assert "p@host" not in desc and ":p" not in desc, desc


@needs_repo_config
def test_explicit_url_wins():
    """A box's pushed configs.yaml must still be able to override."""
    from task_io.registry import _resolve_db_url

    with temp_repo_config(TEMP_CONFIG):
        cfg = _cfg("v2", database={"url": "postgresql://u:p@box/Explicit"})
        assert "Explicit" in _resolve_db_url(cfg), _resolve_db_url(cfg)


@needs_repo_config
def test_env_fallback_and_no_file_creation():
    """The box path: no monorepo config at all -> env var, nothing written."""
    from task_io.registry import _resolve_db_url

    with temp_repo_config(None) as cfg_dir:
        os.environ["MBABENCHV2JUDGE_KEYS_DATABASE_URL"] = "postgresql://u:p@box/BoxDB"
        try:
            assert "BoxDB" in _resolve_db_url(_cfg("v2"))
        finally:
            os.environ.pop("MBABENCHV2JUDGE_KEYS_DATABASE_URL", None)
        # Reading a config must never write one — Config.load defaults to
        # create_missing=True, which would seed a file on a worker box.
        assert not cfg_dir.exists(), f"reading a config created {cfg_dir}"


@needs_repo_config
def test_aws_creds_resolve():
    from task_io.registry import _repo_value

    with temp_repo_config(TEMP_CONFIG):
        assert _repo_value("aws", "access_key_id") == "AKIAFAKE"
        assert _repo_value("aws", "secret_access_key") == "secretfake"
        # session_token has no monorepo equivalent — env-only by design.
        assert _repo_value("aws", "session_token") is None


@needs_repo_config
def test_null_creds_fall_through():
    """`null` placeholders must behave like absent keys, not empty strings."""
    from task_io.registry import _repo_value

    blank = {"database": TEMP_CONFIG["database"], "aws": {"access_key_id": None}}
    with temp_repo_config(blank):
        assert _repo_value("aws", "access_key_id") is None
