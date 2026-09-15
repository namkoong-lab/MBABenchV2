"""TensorBlock Forge model ids ("tensorblock/<bare id>") price and size like
the bare id: the prefix is stripped before the usual lookups."""
from excel_cli_agent import models_config as mc


def test_forge_prefix_maps_to_bare_and_openrouter_slugs():
    c = mc._candidate_slugs("tensorblock/gpt-5.6-sol")
    assert c[0] == "tensorblock/gpt-5.6-sol"
    assert "gpt-5.6-sol" in c and "openai/gpt-5.6-sol" in c


def test_forge_prefix_uses_direct_pricing_and_context(monkeypatch):
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: None)
    assert mc.resolve_pricing("tensorblock/claude-fable-5") == mc.DIRECT_API_PRICING["claude-fable-5"]
    assert mc.resolve_context_window("tensorblock/gpt-5.6-sol") == mc.MODEL_CONTEXT_WINDOWS["gpt-5.6-sol"]
