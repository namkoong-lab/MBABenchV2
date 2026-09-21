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


def test_forge_grok_maps_to_its_openrouter_slug():
    c = mc._candidate_slugs("tensorblock/grok-4.6")
    assert c[0] == "tensorblock/grok-4.6"
    assert "grok-4.6" in c and "x-ai/grok-4.6" in c
    # the cohorts already running resolve exactly as before
    assert mc._candidate_slugs("gpt-6-astra") == ["gpt-6-astra", "openai/gpt-6-astra"]
    assert mc._candidate_slugs("claude-fable-5-1") == [
        "claude-fable-5-1", "claude-fable-5.1",
        "anthropic/claude-fable-5-1", "anthropic/claude-fable-5.1"]


def test_forge_grok_prices_and_sizes_from_the_live_feed(monkeypatch):
    feed = {"x-ai/grok-4.6": {"input": 2.0, "output": 6.0, "context": 500_000}}
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: feed)
    assert mc.resolve_pricing("tensorblock/grok-4.6") == feed["x-ai/grok-4.6"]
    assert mc.resolve_context_window("tensorblock/grok-4.6") == 500_000


def test_forge_grok_prices_and_sizes_without_the_live_feed(monkeypatch):
    """A failed OpenRouter fetch must not record the Grok cohort at $0 or
    floor its workbook context (128k default - 128k output cap = 10k)."""
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: None)
    assert mc.resolve_pricing("tensorblock/grok-4.6") == {"input": 2.00, "output": 6.00}
    assert mc.calculate_cost("tensorblock/grok-4.6", 1_000_000, 100_000) > 0
    window = mc.resolve_context_window("tensorblock/grok-4.6")
    assert window == 500_000
    assert window - 128_000 - 3_000 > 300_000


def test_forge_kimi_maps_to_its_lower_case_openrouter_slug():
    c = mc._candidate_slugs("tensorblock/Kimi-K3")
    assert c[0] == "tensorblock/Kimi-K3"
    assert "Kimi-K3" in c and "moonshotai/kimi-k3" in c


def test_forge_kimi_prices_and_sizes_with_and_without_the_live_feed(monkeypatch):
    """2026-09-21: Forge's usage log bills Kimi at $3.30/$16.50 on every call -
    1.94x the OpenRouter list the first two rows were recorded at. The billed
    rate outranks the live feed; the window still comes from it."""
    feed = {"moonshotai/kimi-k3": {"input": 1.7, "output": 8.5, "context": 1_048_576}}
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: feed)
    assert mc.resolve_pricing("tensorblock/Kimi-K3") == {"input": 3.30, "output": 16.50}
    assert mc.resolve_context_window("tensorblock/Kimi-K3") == 1_048_576
    monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, **k: None)
    assert mc.resolve_pricing("tensorblock/Kimi-K3") == {"input": 3.30, "output": 16.50}
    assert mc.resolve_context_window("tensorblock/Kimi-K3") - 128_000 - 3_000 > 800_000


def test_forge_gemini_prices_and_sizes_with_and_without_the_live_feed(monkeypatch):
    """OpenRouter lists it as google/gemini-3.8-flash, which the bare id never
    maps to: without the static entries every row would cost $0 and see a
    10k-token workbook context. Forge bills Google's $0.75/$3.75."""
    feed = {"google/gemini-3.8-flash": {"input": 0.75, "output": 3.75, "context": 1_048_576}}
    for live in (feed, None):
        monkeypatch.setattr(mc, "_fetch_live_pricing", lambda *a, live=live, **k: live)
        assert mc.resolve_pricing("tensorblock/gemini-3.8-flash") == {"input": 0.75, "output": 3.75}
        assert mc.calculate_cost("tensorblock/gemini-3.8-flash", 1_000_000, 100_000) == 1.125
        assert mc.resolve_context_window("tensorblock/gemini-3.8-flash") - 128_000 - 3_000 > 800_000
