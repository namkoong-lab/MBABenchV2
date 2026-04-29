"""Derive the agent identity from the behavior-determining fields in cfg.

`agent.model_name` / `agent.agent_folder` used to be free-form yaml strings,
which let operators flip `chatgpt_web.agent_mode` or `chatgpt_web.model`
without updating the DB label — two functionally different runs ended up
under the same `task_attempts.agent_model_name`. This module makes the
identity a pure function of the fields that actually change agent output,
so drift is impossible.

To add a new mode: add an entry to the relevant `_*_IDENTITIES` table.
Unknown combinations raise `UnknownAgentCombination`, which forces a
naming decision before an unclassified label reaches the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


@dataclass(frozen=True)
class AgentIdentity:
    model_name: str  # → task_attempts.agent_model_name
    agent_folder: str  # → S3 prefix segment
    agent_model_type: str = "gui"  # → task_attempts.agent_model_type


class UnknownAgentCombination(ValueError):
    pass


# Signature: (claude_web.model,). Extend the tuple — and every entry — when
# adding another Claude field that should bifurcate the DB label (e.g.
# enable_extended_thinking, once it's promoted into the schema).
_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("sonnet_4_6",): AgentIdentity("claude_web", "claude_web"),
    ("opus_4_6",): AgentIdentity("claude_opus_4_6", "claude_opus_4_6"),
    ("haiku_4_5",): AgentIdentity("claude_haiku_4_5", "claude_haiku_4_5"),
}


# When agent_mode=True, ChatGPT Agent is its own routed backend — the
# `model` dropdown becomes cosmetic, so agent-mode runs always collapse to
# one identity regardless of chatgpt_web.model. Only non-agent runs
# bifurcate by model.
_CHATGPT_AGENT_IDENTITY = AgentIdentity("chatgpt_agent", "chatgpt_agent")

# Signature for non-agent-mode runs: (chatgpt_web.model,). model=None means
# "let the session default win"; that + agent_mode=False is the legacy
# `chatgpt_web` label, kept for DB continuity with pre-refactor rows.
_CHATGPT_NON_AGENT_IDENTITIES: dict[tuple, AgentIdentity] = {
    (None,): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("instant",): AgentIdentity("chatgpt_instant", "chatgpt_instant"),
    ("thinking",): AgentIdentity("chatgpt_thinking", "chatgpt_thinking"),
    ("pro",): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
}


def resolve_agent_identity(cfg: SimpleNamespace) -> AgentIdentity:
    provider = getattr(getattr(cfg, "provider", None), "kind", None)
    if provider == "claude":
        return _resolve_claude(cfg)
    if provider == "chatgpt":
        return _resolve_chatgpt(cfg)
    raise UnknownAgentCombination(
        f"provider.kind={provider!r} has no identity resolver. "
        f"Add one in infra/configs/agent_identity.py."
    )


def _resolve_claude(cfg: SimpleNamespace) -> AgentIdentity:
    block = getattr(cfg, "claude_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=claude but cfg.claude_web block is missing."
        )
    model = getattr(block, "model", None)
    key = (model,)
    try:
        return _CLAUDE_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No Claude identity for claude_web.model={model!r}. "
            f"Known: {list(_CLAUDE_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


def _resolve_chatgpt(cfg: SimpleNamespace) -> AgentIdentity:
    block = getattr(cfg, "chatgpt_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=chatgpt but cfg.chatgpt_web block is missing."
        )
    agent_mode = bool(getattr(block, "agent_mode", True))
    if agent_mode:
        # ChatGPT Agent is its own backend; chatgpt_web.model is cosmetic.
        return _CHATGPT_AGENT_IDENTITY
    model = getattr(block, "model", None)
    key = (model,)
    try:
        return _CHATGPT_NON_AGENT_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No ChatGPT identity for "
            f"(chatgpt_web.model, agent_mode=False)={key!r}. "
            f"Known (non-agent): {list(_CHATGPT_NON_AGENT_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )
