"""Offline tests for the agent identity registry (no DB, S3, or API)."""

from pathlib import Path

import pytest
import yaml

from excel_cli_agent import agent_identity as ai

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
TEMPLATE = EXAMPLES_DIR / "batch_config_template_auto.yaml"

HAIKU = "openpyxl_anthropic/claude-haiku-4-5-think16k"

FULL = ("model: m, reasoning_effort: null, thinking_budget_tokens: null, "
        "max_completion_tokens: 1000, base_url: u, fresh_context_mode: true, "
        "enhanced_excel_context: true, recent_history_count: 3")


def test_seed_registry_loads_and_is_collision_free():
    registry = ai.load_registry()
    assert len(registry) >= 5
    assert len({i.axes for i in registry.values()}) == len(registry)


def test_known_label_resolves_with_all_pinned_settings():
    identity = ai.resolve_agent_identity({"agent_model_name": HAIKU})
    assert identity.model == "claude-haiku-4-5"
    assert identity.thinking_budget_tokens == 16000
    assert identity.max_completion_tokens == 32000
    assert identity.reasoning_effort is None
    assert identity.base_url == "https://api.anthropic.com"
    assert identity.fresh_context_mode is True
    assert identity.enhanced_excel_context is True
    assert identity.recent_history_count == 3
    assert set(identity.settings()) == set(ai.PINNED_KEY_NAMES)
    assert identity.extra_configs() == identity.settings()


@pytest.mark.parametrize("key,value", [
    ("model", "claude-haiku-4-5"),           # even repeating the value
    ("thinking_budget_tokens", 16000),
    ("max_completion_tokens", 32000),
    ("base_url", "https://api.anthropic.com"),
    ("fresh_context_mode", True),
    ("enhanced_excel_context", False),
    ("recent_history_count", 5),
    ("reasoning_effort", "high"),
])
def test_pinned_key_in_config_refused(key, value):
    with pytest.raises(ai.AgentIdentityError, match="may not be set in a batch config"):
        ai.resolve_agent_identity({"agent_model_name": HAIKU, key: value})


def test_missing_label_refused():
    with pytest.raises(ai.AgentIdentityError, match="Missing required field"):
        ai.resolve_agent_identity({"model": "claude-haiku-4-5"} | {})
    # (a config with only model/no label used to be the norm; it must not
    # silently resolve via the settings any more)


def test_unregistered_label_refuses_with_stanza():
    with pytest.raises(ai.AgentIdentityError) as exc:
        ai.resolve_agent_model_name({"agent_model_name": "openpyxl_x/new-thing"})
    msg = str(exc.value)
    assert "- agent_model_name: openpyxl_x/new-thing" in msg
    for key in ai.PINNED_KEY_NAMES:
        assert f"  {key}:" in msg


def test_duplicate_axes_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(f"- {{agent_model_name: a, {FULL}}}\n- {{agent_model_name: b, {FULL}}}\n")
    with pytest.raises(ai.AgentIdentityError, match="one combination, one cohort"):
        ai.load_registry(reg)


def test_duplicate_label_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    other = FULL.replace("model: m", "model: n")
    reg.write_text(f"- {{agent_model_name: a, {FULL}}}\n- {{agent_model_name: a, {other}}}\n")
    with pytest.raises(ai.AgentIdentityError, match="labels must be unique"):
        ai.load_registry(reg)


@pytest.mark.parametrize("key", ai.PINNED_KEY_NAMES)
def test_missing_pinned_field_rejected(tmp_path, key):
    entry = {k: v for k, v in yaml.safe_load(f"{{{FULL}}}").items() if k != key}
    entry["agent_model_name"] = "a"
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump([entry]))
    with pytest.raises(ai.AgentIdentityError, match=f"missing '{key}'"):
        ai.load_registry(reg)


def test_wrong_type_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(f"- {{agent_model_name: a, {FULL.replace('recent_history_count: 3', 'recent_history_count: true')}}}\n")
    with pytest.raises(ai.AgentIdentityError, match="must be int"):
        ai.load_registry(reg)
    reg.write_text(f"- {{agent_model_name: a, {FULL.replace('max_completion_tokens: 1000', 'max_completion_tokens: null')}}}\n")
    with pytest.raises(ai.AgentIdentityError, match="may not be null"):
        ai.load_registry(reg)


def test_every_auto_and_local_mode_example_resolves():
    """Each shipped auto-mode and local-mode config (and the template) names
    a registered label, sets no pinned key, and no longer carries
    agent_folder. Examples live in examples/{local,v1,v2}/."""
    paths = sorted(EXAMPLES_DIR.rglob("*.yaml")) + [TEMPLATE]
    assert any(p.parent != EXAMPLES_DIR for p in paths), "no example configs found"
    for cfg_path in paths:
        cfg = yaml.safe_load(cfg_path.read_text())
        assert "agent_folder" not in cfg, f"{cfg_path.name} still sets agent_folder"
        if not (cfg.get("auto_mode") or cfg.get("local_mode")):
            continue
        leaked = [k for k in ai.PINNED_KEY_NAMES if k in cfg]
        assert not leaked, f"{cfg_path.name} sets pinned key(s) {leaked}"
        assert ai.resolve_agent_model_name(cfg) == cfg["agent_model_name"], cfg_path.name
