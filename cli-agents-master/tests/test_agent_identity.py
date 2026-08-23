"""Offline tests for the agent identity registry (no DB, S3, or API)."""

from pathlib import Path

import pytest
import yaml

from excel_cli_agent import agent_identity as ai

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def test_seed_registry_loads_and_is_collision_free():
    registry = ai.load_registry()
    assert len(registry) >= 5
    assert len(set(registry.values())) == len(registry)  # unique labels


def test_known_combo_resolves():
    identity = ai.resolve_agent_identity({
        "model": "claude-haiku-4-5",
        "thinking_budget_tokens": 16000,
        "max_completion_tokens": 32000,
    })
    assert identity.agent_model_name == "openpyxl_anthropic/claude-haiku-4-5-think16k"
    assert identity.base_url == "https://api.anthropic.com"


def test_conflicting_base_url_refused():
    with pytest.raises(ai.AgentIdentityError, match="conflicts with the registered"):
        ai.resolve_agent_identity({
            "model": "claude-haiku-4-5",
            "thinking_budget_tokens": 16000,
            "max_completion_tokens": 32000,
            "base_url": "https://openrouter.ai/api/v1",
        })
    # repeating the registered value (modulo trailing slash) is fine
    identity = ai.resolve_agent_identity({
        "model": "claude-haiku-4-5",
        "thinking_budget_tokens": 16000,
        "max_completion_tokens": 32000,
        "base_url": "https://api.anthropic.com/",
    })
    assert identity.base_url == "https://api.anthropic.com"


def test_missing_max_tokens_uses_executor_default():
    # identity must reflect what the executor actually applies
    axes = ai.axes_from_config({"model": "m"})
    assert axes == ("m", None, None, 8000)


def test_unregistered_combo_refuses_with_stanza():
    with pytest.raises(ai.AgentIdentityError) as exc:
        ai.resolve_agent_model_name({
            "model": "claude-haiku-4-5",
            "thinking_budget_tokens": 32000,   # not registered
            "max_completion_tokens": 32000,
        })
    msg = str(exc.value)
    assert "agent_model_name:" in msg          # paste-ready stanza
    assert "thinking_budget_tokens: 32000" in msg
    assert "think32k" in msg                   # suggested label
    assert "base_url:" in msg                  # stanza includes the endpoint slot


def test_duplicate_axes_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(
        "- {agent_model_name: a, model: m, max_completion_tokens: 1000, base_url: u}\n"
        "- {agent_model_name: b, model: m, max_completion_tokens: 1000, base_url: u}\n"
    )
    with pytest.raises(ai.AgentIdentityError, match="one combination, one cohort"):
        ai.load_registry(reg)


def test_duplicate_label_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(
        "- {agent_model_name: a, model: m, max_completion_tokens: 1000, base_url: u}\n"
        "- {agent_model_name: a, model: n, max_completion_tokens: 1000, base_url: u}\n"
    )
    with pytest.raises(ai.AgentIdentityError, match="labels must be unique"):
        ai.load_registry(reg)


def test_missing_base_url_rejected(tmp_path):
    reg = tmp_path / "reg.yaml"
    reg.write_text("- {agent_model_name: a, model: m, max_completion_tokens: 1000}\n")
    with pytest.raises(ai.AgentIdentityError, match="missing 'base_url'"):
        ai.load_registry(reg)


def test_every_auto_mode_example_resolves():
    """Each shipped auto-mode config must have a registry entry, and none may
    still carry the retired agent_folder key."""
    for cfg_path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        cfg = yaml.safe_load(cfg_path.read_text())
        assert "agent_folder" not in cfg, f"{cfg_path.name} still sets agent_folder"
        if not cfg.get("auto_mode"):
            continue
        label = ai.resolve_agent_model_name(cfg)
        assert label, cfg_path.name
