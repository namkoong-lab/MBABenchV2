"""Resolve a run's cohort label (task_attempts.agent_model_name) from its
model settings, via the agent_identities.yaml registry.

The four axes (model, reasoning_effort, thinking_budget_tokens,
max_completion_tokens) define a cohort. The registry maps each combination
to a label; trial counting, task-selection filters, and the DB row all use
the resolved label. An unregistered combination refuses to run and prints a
paste-ready registry stanza, so changing a setting can never silently write
into another cohort.
"""

from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

import yaml

from .models_config import DEFAULT_MAX_COMPLETION_TOKENS

REGISTRY_PATH = Path(__file__).resolve().parent / "agent_identities.yaml"

# (model, reasoning_effort, thinking_budget_tokens, max_completion_tokens)
AxisKey = Tuple[str, Optional[str], Optional[int], int]


class AgentIdentity(NamedTuple):
    agent_model_name: str
    base_url: str


class AgentIdentityError(ValueError):
    """Registry is invalid, or the run's axes have no registry entry."""


def _axes_from_entry(entry: Dict[str, Any], where: str) -> AxisKey:
    try:
        model = entry["model"]
    except KeyError:
        raise AgentIdentityError(f"{where}: entry is missing 'model'")
    effort = entry.get("reasoning_effort")
    budget = entry.get("thinking_budget_tokens")
    max_tokens = entry.get("max_completion_tokens")
    if max_tokens is None:
        raise AgentIdentityError(f"{where}: entry is missing 'max_completion_tokens'")
    return (str(model), None if effort is None else str(effort),
            None if budget is None else int(budget), int(max_tokens))


def load_registry(path: Path = REGISTRY_PATH) -> Dict[AxisKey, AgentIdentity]:
    """{axes: AgentIdentity}, refusing duplicate labels or axis combos."""
    entries = yaml.safe_load(path.read_text()) or []
    if not isinstance(entries, list):
        raise AgentIdentityError(f"{path}: expected a top-level list of entries")

    by_axes: Dict[AxisKey, AgentIdentity] = {}
    labels: Dict[str, AxisKey] = {}
    for i, entry in enumerate(entries):
        where = f"{path.name} entry {i + 1}"
        if not isinstance(entry, dict):
            raise AgentIdentityError(f"{where}: expected a mapping")
        label = entry.get("agent_model_name")
        if not label:
            raise AgentIdentityError(f"{where}: entry is missing 'agent_model_name'")
        base_url = entry.get("base_url")
        if not base_url:
            raise AgentIdentityError(
                f"{where}: entry is missing 'base_url' — provider routing "
                f"keys off it, so every agent must pin one"
            )
        axes = _axes_from_entry(entry, where)
        if axes in by_axes:
            raise AgentIdentityError(
                f"{where}: axes {axes} already mapped to "
                f"'{by_axes[axes].agent_model_name}' — one combination, one cohort"
            )
        if label in labels:
            raise AgentIdentityError(
                f"{where}: agent_model_name '{label}' already used for axes "
                f"{labels[label]} — labels must be unique"
            )
        by_axes[axes] = AgentIdentity(str(label), str(base_url))
        labels[label] = axes
    return by_axes


def axes_from_config(config: Dict[str, Any]) -> AxisKey:
    """The identity axes as the executor will actually apply them."""
    effort = config.get("reasoning_effort")
    budget = config.get("thinking_budget_tokens")
    max_tokens = config.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS)
    return (str(config["model"]), None if effort is None else str(effort),
            None if budget is None else int(budget), int(max_tokens))


def _suggest_label(axes: AxisKey) -> str:
    model, effort, budget, max_tokens = axes
    parts = [f"openpyxl_{model.replace('/', '_')}"]
    if effort is not None:
        parts.append(str(effort))
    if budget is not None:
        parts.append(f"think{budget // 1000}k" if budget % 1000 == 0 else f"think{budget}")
    return "-".join(parts)


def _stanza(axes: AxisKey, base_url: Optional[str]) -> str:
    model, effort, budget, max_tokens = axes
    return (
        f"- agent_model_name: {_suggest_label(axes)}\n"
        f"  model: {model}\n"
        f"  reasoning_effort: {effort if effort is not None else 'null'}\n"
        f"  thinking_budget_tokens: {budget if budget is not None else 'null'}\n"
        f"  max_completion_tokens: {max_tokens}\n"
        f"  base_url: {base_url or 'https://<provider endpoint for this model>'}"
    )


def resolve_agent_identity(config: Dict[str, Any], path: Path = REGISTRY_PATH) -> AgentIdentity:
    """The cohort identity (label + base_url) for this run's settings, or a
    refusal telling the operator exactly what to add to the registry.

    A base_url in the batch config may only repeat the registry's — a
    conflicting value would route the cohort's requests to a different
    provider than its registered identity, so it refuses to run.
    """
    axes = axes_from_config(config)
    registry = load_registry(path)
    identity = registry.get(axes)
    if identity is None:
        model, effort, budget, max_tokens = axes
        raise AgentIdentityError(
            f"No agent identity registered for model={model!r}, "
            f"reasoning_effort={effort!r}, thinking_budget_tokens={budget!r}, "
            f"max_completion_tokens={max_tokens} — this combination has no "
            f"cohort name yet. Add an entry to {path} (edit the label to "
            f"taste):\n\n{_stanza(axes, config.get('base_url'))}\n"
        )
    config_url = config.get("base_url")
    if config_url and config_url.rstrip("/") != identity.base_url.rstrip("/"):
        raise AgentIdentityError(
            f"base_url {config_url!r} in the batch config conflicts with the "
            f"registered base_url {identity.base_url!r} for "
            f"'{identity.agent_model_name}'. Remove the key (the registry's "
            f"value is applied automatically), or register a separate agent "
            f"identity for this endpoint."
        )
    return identity


def resolve_agent_model_name(config: Dict[str, Any], path: Path = REGISTRY_PATH) -> str:
    """The cohort label alone; see resolve_agent_identity."""
    return resolve_agent_identity(config, path).agent_model_name
