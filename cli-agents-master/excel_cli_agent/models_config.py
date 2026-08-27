"""Centralized model configuration: pricing, defaults, and cost calculation."""
import json
import re
import urllib.request
from typing import Dict, Optional

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_COMPLETION_TOKENS = 8000

# Model pricing (per 1M tokens) - Updated January 2026
# FALLBACK ONLY: calculate_cost() prefers live prices fetched from
# OpenRouter's public model list at run start (see resolve_pricing); this
# table is used only when that fetch fails or the model isn't listed.
MODEL_PRICING = {
    # GPT-5 series (Flagship models - January 2025)
    "gpt-5.1": {"input": 1.250, "output": 10.00},
    "gpt-5-mini": {"input": 0.250, "output": 2.00},
    "gpt-5-nano": {"input": 0.050, "output": 0.400},
    "gpt-5-pro": {"input": 15.00, "output": 120.00},

    # GPT-4.1 series (Updated models with fine-tuning support)
    "gpt-4.1": {"input": 3.00, "output": 12.00},
    "gpt-4.1-mini": {"input": 0.80, "output": 3.20},
    "gpt-4.1-nano": {"input": 0.20, "output": 0.80},

    # GPT-4o family (Legacy - still available via API)
    "gpt-4o": {"input": 3.00, "output": 10.00},
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "gpt-4o-2024-11-20": {"input": 3.00, "output": 10.00},
    "gpt-4o-2024-08-06": {"input": 3.00, "output": 10.00},
    "gpt-4o-2024-05-13": {"input": 5.00, "output": 15.00},
    "gpt-4o-mini-2024-07-18": {"input": 0.150, "output": 0.600},

    # Realtime API models (Text pricing)
    "gpt-realtime": {"input": 4.00, "output": 16.00},
    "gpt-realtime-mini": {"input": 0.60, "output": 2.40},

    # GPT-4 Turbo family (Legacy)
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4-turbo-2024-04-09": {"input": 10.00, "output": 30.00},
    "gpt-4-turbo-preview": {"input": 10.00, "output": 30.00},

    # GPT-4 family (Legacy)
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-4-0613": {"input": 30.00, "output": 60.00},
    "gpt-4-32k": {"input": 60.00, "output": 120.00},

    # GPT-3.5 Turbo family (Legacy)
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "gpt-3.5-turbo-0125": {"input": 0.50, "output": 1.50},
    "gpt-3.5-turbo-1106": {"input": 1.00, "output": 2.00},

    # O1/O4 family (Reasoning models)
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o1-preview": {"input": 15.00, "output": 60.00},
    "o4-mini": {"input": 4.00, "output": 16.00},

    # OpenRouter models (third-party via OpenRouter) - Updated Jan 2026
    "moonshotai/kimi-k2-thinking": {"input": 0.57, "output": 2.42},
    "deepseek/deepseek-r1": {"input": 0.55, "output": 2.19},
    "deepseek/deepseek-v3-0324": {"input": 0.50, "output": 1.50},
    "deepseek/deepseek-v3.2": {"input": 0.25, "output": 0.38},
    "openai/gpt-5.2": {"input": 2.50, "output": 10.00},
    "openai/gpt-5.2-codex": {"input": 1.75, "output": 14.00},
    "openai/gpt-5.3-codex": {"input": 1.75, "output": 14.00},
    "anthropic/claude-opus-4.5": {"input": 5.00, "output": 25.00},
    "anthropic/claude-opus-4.6": {"input": 5.00, "output": 25.00},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},  # Anthropic direct model ID
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},  # Anthropic direct model ID
    "claude-fable-5": {"input": 25.00, "output": 50.00},  # Anthropic direct model ID; see DIRECT_API_PRICING
    "gpt-5.6-sol": {"input": 5.00, "output": 30.00},  # OpenAI direct model ID
    "google/gemini-3-pro-preview": {"input": 1.25, "output": 10.00},
    "z-ai/glm-4.7": {"input": 0.40, "output": 1.50},
    "x-ai/grok-4": {"input": 3.00, "output": 15.00},
    "mistralai/mistral-medium-3": {"input": 0.40, "output": 2.00},
    "mistralai/mistral-large-2512": {"input": 0.50, "output": 1.50},
    "qwen/qwen3-235b-a22b": {"input": 0.18, "output": 0.54},
    "qwen/qwen3-235b-a22b-2507": {"input": 0.071, "output": 0.463},
    "qwen/qwen3-235b-a22b-thinking-2507": {"input": 0.11, "output": 0.60},
    "allenai/olmo-3.1-32b-think:free": {"input": 0.0, "output": 0.0},
    "allenai/olmo-3.1-32b-think": {"input": 0.15, "output": 0.50},
    "moonshotai/kimi-k2.5": {"input": 0.60, "output": 2.50},
    "qwen/qwen3.5-397b-a17b": {"input": 0.20, "output": 0.60},
}

# Per-model defaults (only models that differ from standard)
MODEL_DEFAULTS: Dict[str, Dict] = {
    "openai/gpt-5.2": {
        "reasoning_effort": "none",
        "max_completion_tokens": 64000,
    },
    "anthropic/claude-opus-4.5": {
        "thinking_budget_tokens": 50000,
        "max_completion_tokens": 64000,
    },
    "claude-opus-4-6": {
        "thinking_budget_tokens": 50000,
        "max_completion_tokens": 64000,
    },
    # claude-fable-5: thinking always on; effort (not budget_tokens) controls it.
    # reasoning_effort maps to output_config.effort in the Anthropic direct path.
    "claude-fable-5": {
        "reasoning_effort": "max",
        "max_completion_tokens": 64000,
    },
    # gpt-5.6-sol: xhigh is the highest reasoning_effort the API accepts
    # (the ChatGPT UI "Ultra" label does not exist as an API value).
    "gpt-5.6-sol": {
        "reasoning_effort": "xhigh",
        "max_completion_tokens": 64000,
    },
    "google/gemini-3-pro-preview": {
        "reasoning_effort": "high",
        "max_completion_tokens": 64000,
    },
    "x-ai/grok-4": {
        "max_completion_tokens": 64000,
    },
    "z-ai/glm-4.7": {
        "max_completion_tokens": 64000,
    },
    "qwen/qwen3-235b-a22b-thinking-2507": {
        "max_completion_tokens": 64000,
    },
    "moonshotai/kimi-k2.5": {
        "max_completion_tokens": 64000,
    },
    "qwen/qwen3.5-397b-a17b": {
        "max_completion_tokens": 64000,
    },
}

# Timeout by reasoning effort level
TIMEOUT_BY_REASONING: Dict[Optional[str], int] = {
    "max": 3600,  # Anthropic effort-based models (e.g. claude-fable-5)
    "xhigh": 300,
    "high": 240,
    None: 180,
}


# --- Live pricing -----------------------------------------------------------
# OpenRouter publishes current per-token prices for every model it routes
# (including Anthropic/OpenAI list prices) at a public, unauthenticated
# endpoint. Fetched once per process so each run prices with current data
# instead of a hand-maintained table.
_LIVE_PRICING_URL = "https://openrouter.ai/api/v1/models"
_live_pricing: Optional[Dict[str, Dict[str, float]]] = None
_live_pricing_attempted = False
_pricing_warned: set = set()


def _fetch_live_pricing(timeout: int = 10) -> Optional[Dict[str, Dict[str, float]]]:
    """Fetch {model_id: {input, output}} in $/MTok. Cached for the process."""
    global _live_pricing, _live_pricing_attempted
    if _live_pricing_attempted:
        return _live_pricing
    _live_pricing_attempted = True
    try:
        with urllib.request.urlopen(_LIVE_PRICING_URL, timeout=timeout) as resp:
            data = json.load(resp)
        pricing = {}
        for entry in data.get("data", []):
            p = entry.get("pricing") or {}
            try:
                pricing[entry["id"]] = {
                    "input": float(p["prompt"]) * 1_000_000,
                    "output": float(p["completion"]) * 1_000_000,
                    "context": int(entry["context_length"] or 0) or None,
                }
            except (KeyError, TypeError, ValueError):
                continue
        _live_pricing = pricing or None
        if _live_pricing:
            print(f"💲 Live pricing loaded from OpenRouter ({len(pricing)} models)")
    except Exception as e:
        print(f"⚠️ Live pricing fetch failed ({e}); using static MODEL_PRICING fallback")
        _live_pricing = None
    return _live_pricing


def _candidate_slugs(model: str) -> list:
    """Map a model name to the OpenRouter ids it may be listed under.

    Direct-API ids differ from OpenRouter slugs: "claude-fable-5" is listed
    as "anthropic/claude-fable-5", "claude-opus-4-8" as
    "anthropic/claude-opus-4.8", "gpt-5.6-sol" as "openai/gpt-5.6-sol".
    """
    candidates = [model]
    dotted = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)
    if dotted != model:
        candidates.append(dotted)
    for name in list(candidates):
        if "/" not in name:
            if name.startswith("claude"):
                candidates.append(f"anthropic/{name}")
            elif name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
                candidates.append(f"openai/{name}")
    return candidates


# Prices verified against ACTUAL billing for direct-API runs. These outrank
# the OpenRouter live feed, which lists brokered-route prices that can
# differ from what the provider bills a direct API key. Keys are bare
# direct-API ids (no "/"), which never name an OpenRouter-routed run, so
# these overrides cannot touch OpenRouter cohorts (whose real billed cost
# is preferred via usage.include anyway).
#
# claude-fable-5: calibrated 2026-08-27 against a Console credit diff over
# a single isolated run (attempt 468: 3,214,460 in / 439,972 out drew
# $102.58; $25/$50 predicts $102.36, +0.2% residual; the $10/$50 the live
# feed lists for anthropic/claude-fable-5 predicts $54.14, -47%). Single
# calibration point — input/output split assumes output stayed at the
# listed $50; re-verify against the next isolated run's credit diff.
DIRECT_API_PRICING = {
    "claude-fable-5": {"input": 25.00, "output": 50.00},
}


def resolve_pricing(model: str) -> Optional[Dict[str, float]]:
    """Return {input, output} $/MTok for a model, preferring live data.

    Order: DIRECT_API_PRICING (billing-verified direct-API rates) -> live
    OpenRouter prices (exact id, then direct-API id mapped to its
    OpenRouter slug) -> static MODEL_PRICING -> None. Falling back or
    failing to price is warned once per model so silent 0-cost rows can't
    accumulate unnoticed.
    """
    if model in DIRECT_API_PRICING:
        return DIRECT_API_PRICING[model]
    live = _fetch_live_pricing()
    if live:
        for slug in _candidate_slugs(model):
            if slug in live:
                return live[slug]
    if model in MODEL_PRICING:
        if live and model not in _pricing_warned:
            _pricing_warned.add(model)
            print(f"⚠️ {model} not in live pricing; using static table price")
        return MODEL_PRICING[model]
    if model not in _pricing_warned:
        _pricing_warned.add(model)
        print(f"⚠️ No pricing found for {model}; cost will be logged as 0.0")
    return None


# FALLBACK ONLY: resolve_context_window() prefers the live context_length
# from the same OpenRouter fetch; these cover the direct-API ids in use when
# that fetch fails.
MODEL_CONTEXT_WINDOWS = {
    "claude-fable-5": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-6": 200_000,
    "gpt-5.6-sol": 400_000,
}
DEFAULT_CONTEXT_WINDOW = 128_000


def resolve_context_window(model: str) -> int:
    """Return the model's input context window in tokens.

    Order: live OpenRouter context_length -> static MODEL_CONTEXT_WINDOWS ->
    DEFAULT_CONTEXT_WINDOW (conservative). Warned once per model on fallback.
    """
    live = _fetch_live_pricing()
    if live:
        for slug in _candidate_slugs(model):
            ctx = (live.get(slug) or {}).get("context")
            if ctx:
                return ctx
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    key = f"ctx:{model}"
    if key not in _pricing_warned:
        _pricing_warned.add(key)
        print(
            f"⚠️ No context window found for {model}; "
            f"assuming {DEFAULT_CONTEXT_WINDOW:,} tokens"
        )
    return DEFAULT_CONTEXT_WINDOW


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate cost in USD for an API call.

    Args:
        model: Model name (e.g., "claude-fable-5", "openai/gpt-5.2")
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens

    Returns:
        Cost in USD (0.0 when no price is known — warned by resolve_pricing)
    """
    pricing = resolve_pricing(model)
    if not pricing:
        return 0.0

    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost


def get_model_defaults(model: str) -> Dict:
    """Get per-model default parameters.

    Args:
        model: Model name/slug

    Returns:
        Dict of default parameters for the model (empty if no special defaults)
    """
    return MODEL_DEFAULTS.get(model, {})
