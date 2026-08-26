"""Offline tests: config loading/guards, engine-config assembly, and the
source-level regression guards for the bugs this port fixed."""

from pathlib import Path

import pytest
from excel_agent.engine import (
    AGENT_CORES,
    EXIT_AGENT_FAILURE,
    EXIT_CONFIG_ERROR,
    EXIT_INFRA_FAILURE,
    EXIT_SUCCESS,
    _validate_config,
)
from infra.configs import ConfigError, load_configs
from infra.configs.prompt_registry import PromptVersionError, resolve_prompt_files
from infra.run import build_engine_config, preflight_check
from task_io.base import TaskSpec
from task_io.registry import build_source

MEMBER_ROOT = Path(__file__).resolve().parents[1]


# ---- config loader ----------------------------------------------------------


def test_defaults_load_clean(tmp_path):
    cfg = load_configs(override_path=tmp_path / "absent.yaml")
    assert cfg.benchmark == "v2"
    assert cfg.browser.cdp_port == 9222
    assert cfg.source.schema == "mbabenchv2"
    assert cfg.prompt_version == 200


def test_unknown_key_is_a_typo_error(tmp_path):
    bad = tmp_path / "configs.yaml"
    bad.write_text("browsr:\n  cdp_port: 9223\n")
    with pytest.raises(ConfigError, match="unknown key 'browsr'"):
        load_configs(override_path=bad)


def test_provider_model_key_is_not_in_schema(tmp_path):
    """The schema itself refuses model-selecting keys in provider blocks —
    the registry is the only source for them."""
    bad = tmp_path / "configs.yaml"
    bad.write_text('claude_excel_agent:\n  ui_model_label: "Opus 4.6"\n')
    with pytest.raises(ConfigError, match="ui_model_label"):
        load_configs(override_path=bad)


def test_benchmark_schema_mismatch_refused(tmp_path):
    """A v1 run must not read through the mbabenchv2 schema (and vice
    versa) — the mismatch is refused at build time, before credentials."""
    override = tmp_path / "configs.yaml"
    override.write_text("benchmark: v1\n")
    cfg = load_configs(override_path=override)
    assert cfg.source.schema == "mbabenchv2"  # default schema now contradicts
    with pytest.raises(ValueError, match="contradicts benchmark"):
        build_source(cfg)


def test_prompt_version_dual_knob_mismatch_refused(tmp_path):
    cfg = load_configs(override_path=tmp_path / "absent.yaml")
    cfg.agent.prompt_version = 0  # disagrees with top-level 200
    with pytest.raises(PromptVersionError, match="mismatch records the row"):
        resolve_prompt_files(cfg)


# ---- engine-config assembly -------------------------------------------------


def _identity():
    from infra.configs.agent_identity import AgentIdentity

    return AgentIdentity(
        agent_model_name="claude_excel_opus_4_6",
        provider="claude_excel_agent",
        ui_model_label="Opus 4.6",
        thinking_effort=None,
        agent_folder="claude_excel_opus_4_6",
        agent_model_type="excel",
    )


def _spec(tmp_path) -> TaskSpec:
    wb = tmp_path / "ApfelInc model.xlsx"
    wb.write_bytes(b"x")
    pdf = tmp_path / "case.pdf"
    pdf.write_bytes(b"y")
    return TaskSpec(
        task_id="7",
        task_name="ApfelInc",
        upload_files=[wb, pdf],
        metadata={"task_source": "jp", "db_task_id": 7},
    )


def test_engine_config_splits_workbook_from_panel_files(tmp_path):
    cfg = load_configs(override_path=tmp_path / "absent.yaml")
    engine_config = build_engine_config(
        cfg, _spec(tmp_path), _identity(), ["step one text"]
    )
    # The workbook is opened on OneDrive by NAME, not uploaded to the panel.
    assert engine_config["template_file"] == "ApfelInc model.xlsx"
    assert [Path(p).name for p in engine_config["upload_files"]] == ["case.pdf"]
    assert engine_config["agent_type"] == "claude_excel_agent"
    # The identity's pin reaches the provider block the core reads.
    assert engine_config["claude_excel_agent"]["ui_model_label"] == "Opus 4.6"
    assert engine_config["prompts"] == ["step one text"]
    assert engine_config["file_path"] == ["My files", "mbabench_tasks"]
    assert preflight_check(engine_config) == []


def test_preflight_catches_missing_upload_file(tmp_path):
    cfg = load_configs(override_path=tmp_path / "absent.yaml")
    spec = _spec(tmp_path)
    engine_config = build_engine_config(cfg, spec, _identity(), ["text"])
    engine_config["upload_files"] = [str(tmp_path / "missing.pdf")]
    errors = preflight_check(engine_config)
    assert any("not found" in e for e in errors)


# ---- engine contract --------------------------------------------------------


def test_engine_exit_codes():
    assert (EXIT_SUCCESS, EXIT_AGENT_FAILURE, EXIT_CONFIG_ERROR, EXIT_INFRA_FAILURE) == (
        0, 1, 2, 3,
    )
    assert set(AGENT_CORES) == {"claude_excel_agent", "chatgpt_excel_agent"}


def test_engine_config_validation():
    errors = _validate_config({})
    assert errors  # everything missing
    ok = {
        "agent_type": "claude_excel_agent",
        "task_name": "T",
        "prompts": ["p"],
        "file_path": ["My files"],
        "task_source": "jp",
    }
    assert _validate_config(ok) == []
    bad_agent = dict(ok, agent_type="tabai")
    assert any("agent_type" in e for e in _validate_config(bad_agent))


# ---- regression guards on the ported sources --------------------------------
# These pin the specific bugs the port fixed. If one fires, the bug came back.


def _src(rel: str) -> str:
    return (MEMBER_ROOT / rel).read_text()


def test_engine_never_gates_template_on_attempt_number():
    # The predecessor's `workbook_file and attempt_number == 0` ran every
    # RETRY on a blank workbook and recorded it as SUCCESS.
    assert "and attempt_number == 0" not in _src("excel_agent/engine.py")


def test_navigation_has_no_substring_matching():
    nav = _src("excel_agent/core/navigation.py")
    assert "target_norm in _normalize_name" not in nav  # round1 ⊄ round10
    # No :has-text() selectors (substring; div form clicked the whole list
    # container) and no *= attribute substring selectors on names.
    assert ':has-text("' not in nav
    assert '*="{' not in nav


def test_no_global_browser_kills():
    bm = _src("excel_agent/core/browser_manager.py")
    assert "kill_all_browser_processes" not in bm
    assert '"pkill", "-f", "chrome"' not in bm.lower()
    assert "--user-data-dir=" in bm  # kills are profile-scoped


def test_upload_fallbacks_are_frame_scoped():
    for rel in (
        "excel_agent/core/claude_core.py",
        "excel_agent/core/chatgpt_core.py",
    ):
        src = _src(rel)
        assert "_set_files_on_hidden_input" in src
        # The page-wide search list must not appear in the fallback.
        assert "list(self.page.frames) + [self.page]:\n            try:\n                inputs" not in src


def test_frame_scans_exclude_host_frames():
    base = _src("excel_agent/core/ai_agent_base.py")
    assert "def is_host_frame" in base
    for rel in (
        "excel_agent/core/claude_core.py",
        "excel_agent/core/chatgpt_core.py",
    ):
        assert "_is_host_frame(f)" in _src(rel)
