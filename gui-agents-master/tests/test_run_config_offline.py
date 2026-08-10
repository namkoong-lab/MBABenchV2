"""Offline pipeline checks: config merge -> engine config -> preflight,
for both benchmarks. No DB, AWS, or browser access.

Run from gui-agents-master:  .venv/bin/python tests/test_run_config_offline.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

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


def check_v1():
    # Same shape as the untracked prod_single_claude_*.yaml configs, minus
    # machine secrets — validates the merge path those configs rely on.
    cfg = load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={
            "benchmark": "v1",
            "prompts_file": ["tasks_configs/prompts_pv9/SHARED_pv9_prompt.txt"],
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
    ec = build_engine_config(cfg, fake_spec())
    assert ec["prompts_file"] == [
        "tasks_configs/prompts_pv9/SHARED_pv9_prompt.txt"
    ], ec.get("prompts_file")
    errors = preflight_check(ec, "claude", cfg.benchmark)
    assert not errors, errors
    # engine-side expansion: 1 file -> 1 prompt, correct length
    resolved = resolve_prompts(dict(ec))
    assert len(resolved["prompts"]) == 1
    assert len(resolved["prompts"][0]) > 13000, len(resolved["prompts"][0])
    print(f"OK  v1: identity={ident.model_name}, pv9 prompt "
          f"{len(resolved['prompts'][0])} chars, preflight clean")


def check_v2():
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
    assert ident.model_name == "claude_fable_5", ident
    ec = build_engine_config(cfg, fake_spec())
    errors = preflight_check(ec, "claude", cfg.benchmark)
    assert not errors, errors
    resolved = resolve_prompts(dict(ec))
    assert len(resolved["prompts"]) == 3, len(resolved["prompts"])
    assert "132 checks" in resolved["prompts"][1]
    print(f"OK  v2: identity={ident.model_name}, 3-step prompts "
          f"({[len(p) for p in resolved['prompts']]}) , preflight clean")


def check_v1_missing_axes_fails():
    """A v1 run config that forgets the axes must fail preflight, not run."""
    cfg = load_configs(
        default_path=REPO / "infra/configs/configs.default.yaml",
        override_path=REPO / "infra/configs/does_not_exist.yaml",
        run_config_data={
            "benchmark": "v1",
            "provider": {"kind": "chatgpt"},
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
    print("OK  v1 invalid work-effort rejected by preflight")


def main() -> int:
    check_v1()
    check_v2()
    check_v1_missing_axes_fails()
    print("ALL OFFLINE RUN-CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
