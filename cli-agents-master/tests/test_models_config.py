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


def test_rerun_models_price_and_size_without_the_live_feed(monkeypatch):
    """2026-09-19 backstop: a failed OpenRouter fetch must not record the
    rerun cohorts at $0 or shrink their workbook context to the floor."""
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: None)
    for model in ("claude-fable-5-1", "gpt-6-astra"):
        price = mc.resolve_pricing(model)
        assert price and price["input"] > 0 and price["output"] > 0, model
        assert mc.calculate_cost(model, 1_000_000, 100_000) > 0
        window = mc.resolve_context_window(model)
        assert window >= 1_000_000, (model, window)
        # budget left for the workbook after the 128k output allowance
        assert window - 128_000 - 3_000 > 800_000
