"""Every checked-in run config and dispatcher template still loads.

A config that fails to load is not a slow failure — `infra/run.py` exits
before the browser opens, and a box provisioned from a broken dispatcher
template cannot run a single task. The four checks below are the ones the
runner does first, so a config that clears them will at least start:

  load_configs        — the merge, plus the unknown-key check
  resolve_prompt_files — prompt_version reaches a registered prompt set
  resolve_agent_identity — the axes name a row in the identity tables
  preflight_check     — the provider block satisfies the benchmark's contract

No DB, AWS, or browser access; the files are read off disk.

Run from gui-agents-master:  python -m pytest tests/test_checked_in_configs.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra.configs import load_configs  # noqa: E402
from infra.configs.agent_identity import resolve_agent_identity  # noqa: E402
from infra.configs.prompt_registry import resolve_prompt_files  # noqa: E402
from infra.run import (  # noqa: E402
    _RUN_CONFIG_TASK_KEYS,
    build_engine_config,
    preflight_check,
)

DEFAULT_PATH = REPO / "infra/configs/configs.default.yaml"
# A path that does not exist, so the operator's own configs.yaml (gitignored,
# machine-specific) cannot make a broken checked-in config look fine here.
NO_OVERRIDES = REPO / "infra/configs/configs.yaml.absent-on-purpose"

CONFIG_FILES = sorted(REPO.glob("infra/configs/run_configs/**/*.yaml")) + sorted(
    REPO.glob("infra/dispatcher/config_templates/*.yaml")
)


def _ids(paths):
    return [str(p.relative_to(REPO)) for p in paths]


def test_there_are_configs_to_check():
    """Guard against the glob silently matching nothing after a move."""
    assert len(CONFIG_FILES) >= 10, _ids(CONFIG_FILES)


@pytest.fixture
def fake_spec():
    return NS(
        task_name="TestTask",
        task_id=7,
        upload_files=[],
        solution_name=None,
        metadata={"task_source": "wsp", "db_task_id": 7},
        overrides={},
    )


@pytest.mark.parametrize("path", CONFIG_FILES, ids=_ids(CONFIG_FILES))
def test_config_loads_and_resolves(path: Path, fake_spec):
    data = yaml.safe_load(path.read_text()) or {}
    # Mirror --run-config routing: a task-shaped file's reserved keys are
    # consumed by the runner, not deep-merged as overrides.
    overlay = {k: v for k, v in data.items() if k not in _RUN_CONFIG_TASK_KEYS}

    cfg = load_configs(
        default_path=DEFAULT_PATH,
        override_path=NO_OVERRIDES,
        run_config_data=overlay,
    )

    prompt_files = resolve_prompt_files(cfg)
    assert prompt_files, "resolved to no prompt files"

    identity = resolve_agent_identity(cfg)
    assert identity.model_name

    engine_config = build_engine_config(cfg, fake_spec, identity.agent_folder)
    errors = preflight_check(engine_config, cfg.provider.kind, cfg.benchmark)
    assert not errors, errors
