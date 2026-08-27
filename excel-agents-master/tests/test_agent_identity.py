"""Offline tests for the agent-identity registry and its refusal semantics."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from infra.configs.agent_identity import (
    AgentIdentityError,
    load_registry,
    resolve_agent_identity,
)

MEMBER_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "agent_identities.yaml"
    p.write_text(text)
    return p


VALID = """
- agent_model_name: claude_excel_opus_4_6
  provider: claude_excel_agent
  ui_model_label: "Opus 4.6"
  thinking_effort: null
  agent_folder: claude_excel_opus_4_6
  agent_model_type: excel
- agent_model_name: chatgpt_excel_heavy
  provider: chatgpt_excel_agent
  ui_model_label: null
  thinking_effort: "Heavy"
  agent_folder: chatgpt_excel_heavy
  agent_model_type: excel
- agent_model_name: chatgpt_excel_gpt_5_6_sol_xhigh
  provider: chatgpt_excel_agent
  ui_model_label: "GPT-5.6 Sol"
  thinking_effort: "Extra High"
  agent_folder: chatgpt_excel_gpt_5_6_sol_xhigh
  agent_model_type: excel
"""


def test_shipped_registry_loads():
    registry = load_registry()
    assert registry, "shipped agent_identities.yaml must not be empty"
    for identity in registry.values():
        assert identity.agent_model_type == "excel"
        assert identity.model_name == identity.agent_model_name  # task_io compat


def test_resolve_known_label(tmp_path):
    path = _write(tmp_path, VALID)
    cfg = SimpleNamespace(agent_model_name="chatgpt_excel_gpt_5_6_sol_xhigh")
    identity = resolve_agent_identity(cfg, path)
    assert identity.provider == "chatgpt_excel_agent"
    assert identity.ui_model_label == "GPT-5.6 Sol"
    assert identity.thinking_effort == "Extra High"
    assert identity.settings()["agent_model_type"] == "excel"


def test_legacy_chatgpt_without_model_loads_but_refuses_to_resolve(tmp_path):
    """Pre-model-picker ChatGPT entries stay loadable (append-only history)
    but cannot run: without a pinned model the panel default would decide
    the cohort."""
    path = _write(tmp_path, VALID)
    assert "chatgpt_excel_heavy" in load_registry(path)
    with pytest.raises(AgentIdentityError, match="model picker") as e:
        resolve_agent_identity(
            SimpleNamespace(agent_model_name="chatgpt_excel_heavy"), path
        )
    assert "ui_model_label" in str(e.value)  # points at the fix


def test_unknown_label_prints_stanza(tmp_path):
    path = _write(tmp_path, VALID)
    cfg = SimpleNamespace(agent_model_name="nope")
    with pytest.raises(AgentIdentityError) as e:
        resolve_agent_identity(cfg, path)
    assert "agent_model_name: nope" in str(e.value)  # paste-ready stanza


def test_missing_label_refused(tmp_path):
    path = _write(tmp_path, VALID)
    with pytest.raises(AgentIdentityError, match="agent_model_name"):
        resolve_agent_identity(SimpleNamespace(agent_model_name=None), path)


def test_pinned_key_in_config_refused(tmp_path):
    path = _write(tmp_path, VALID)
    cfg = SimpleNamespace(
        agent_model_name="claude_excel_opus_4_6", ui_model_label="Opus 4.6"
    )
    with pytest.raises(AgentIdentityError, match="may not be set"):
        resolve_agent_identity(cfg, path)


def test_pinned_key_in_provider_block_refused(tmp_path):
    path = _write(tmp_path, VALID)
    cfg = SimpleNamespace(
        agent_model_name="claude_excel_opus_4_6",
        claude_excel_agent=SimpleNamespace(model="Opus 4.6"),
    )
    with pytest.raises(AgentIdentityError, match="claude_excel_agent.model"):
        resolve_agent_identity(cfg, path)
    cfg2 = SimpleNamespace(
        agent_model_name="chatgpt_excel_heavy",
        chatgpt_excel_agent=SimpleNamespace(thinking_effort="Fast"),
    )
    with pytest.raises(AgentIdentityError, match="thinking_effort"):
        resolve_agent_identity(cfg2, path)


def test_duplicate_label_refused(tmp_path):
    path = _write(tmp_path, VALID + VALID)
    with pytest.raises(AgentIdentityError, match="labels must be unique"):
        load_registry(path)


def test_duplicate_axes_refused(tmp_path):
    dupe = VALID + """
- agent_model_name: another_label_same_axes
  provider: chatgpt_excel_agent
  ui_model_label: null
  thinking_effort: "Heavy"
  agent_folder: another_label_same_axes
  agent_model_type: excel
"""
    path = _write(tmp_path, dupe)
    with pytest.raises(AgentIdentityError, match="one combination, one cohort"):
        load_registry(path)


def test_provider_axis_validation(tmp_path):
    # Claude without a ui_model_label
    path = _write(tmp_path, """
- agent_model_name: bad_claude
  provider: claude_excel_agent
  ui_model_label: null
  thinking_effort: null
  agent_folder: bad_claude
  agent_model_type: excel
""")
    with pytest.raises(AgentIdentityError, match="ui_model_label"):
        load_registry(path)
    # ChatGPT without a thinking_effort
    path = _write(tmp_path, """
- agent_model_name: bad_chatgpt
  provider: chatgpt_excel_agent
  ui_model_label: "GPT-5.6 Sol"
  thinking_effort: null
  agent_folder: bad_chatgpt
  agent_model_type: excel
""")
    with pytest.raises(AgentIdentityError, match="thinking_effort"):
        load_registry(path)


def test_wrong_agent_model_type_refused(tmp_path):
    path = _write(tmp_path, """
- agent_model_name: bad_type
  provider: chatgpt_excel_agent
  ui_model_label: null
  thinking_effort: "Heavy"
  agent_folder: bad_type
  agent_model_type: gui
""")
    with pytest.raises(AgentIdentityError, match="must be 'excel'"):
        load_registry(path)
