"""Derive the agent identity from the behavior-determining fields in cfg.

`agent.model_name` / `agent.agent_folder` used to be free-form yaml strings,
which let operators flip `chatgpt_web.agent_mode` or `chatgpt_web.model`
without updating the DB label — two functionally different runs ended up
under the same `task_attempts.agent_model_name`. This module makes the
identity a pure function of the fields that actually change agent output,
so drift is impossible.

BENCHMARK-AWARE: the repo runs two experiments with different DBs, prompt
sets, and label conventions. `cfg.benchmark` ("v1" | "v2") selects the
identity namespace:

  v1 — the MBABench V1 wave (BizbenchV1 DB, pv9 prompts). Labels bifurcate
       on every UI axis that changes agent output: Claude mode (chat/cowork)
       + model + effort; ChatGPT mode (chat/work) + model + intelligence or
       effort+speed. agent_mode is refused (removed from the ChatGPT UI
       ~mid-2026; historical rows keep the old label).
  v2 — the MBABenchV2 task set (MBABenchV2 DB, rubric-v9 3-step prompts).
       Labels bifurcate on model only; agent_mode=True collapses to the
       chatgpt_agent identity (that backend's model dropdown is cosmetic).

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


VALID_BENCHMARKS = ("v1", "v2")


def _benchmark(cfg: SimpleNamespace) -> str:
    bench = (getattr(cfg, "benchmark", None) or "v2").lower()
    if bench not in VALID_BENCHMARKS:
        raise UnknownAgentCombination(
            f"benchmark={bench!r} is not one of {VALID_BENCHMARKS}. Set "
            f"`benchmark: v1` (BizbenchV1 wave conventions) or "
            f"`benchmark: v2` (MBABenchV2 task set) in the run config."
        )
    return bench


# =========================================================================
# V1 identity tables — MBABench V1 wave (BizbenchV1 DB)
# =========================================================================

# Signature: (claude_web.mode, claude_web.model, claude_web.effort). The
# claude.ai UI (2026-07) exposes reasoning effort AND a Chat/Cowork mode
# toggle as first-class controls that change agent output, so both
# bifurcate the DB label. mode defaults to "chat"; effort=None entries are
# the pre-effort-era runs, kept for DB continuity.
_V1_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", "sonnet_4_6", None): AgentIdentity("claude_web", "claude_web"),
    ("chat", "opus_4_6", None): AgentIdentity(
        "claude_opus_4_6", "claude_opus_4_6"
    ),
    ("chat", "haiku_4_5", None): AgentIdentity(
        "claude_haiku_4_5", "claude_haiku_4_5"
    ),
    # 2026-07 benchmark refresh (names collision-checked against
    # mbabench/attempts/, mbabench/BizbenchV1/attempts/, and DB
    # agent_model_name on 2026-07-21; signed off 2026-07-21):
    ("chat", "fable_5", "max"): AgentIdentity(
        "claude_web_chat_fable5_max", "claude_web_chat_fable5_max"
    ),
    ("cowork", "fable_5", "max"): AgentIdentity(
        "claude_web_cowork_fable5_max", "claude_web_cowork_fable5_max"
    ),
    # 2026-07-24 grading-variance experiment (chat-mode Opus 4.8 at max
    # effort; collision-checked against DB agent_model_name + S3 prefixes;
    # _var suffix keeps it separate from any future production Opus 4.8 wave):
    ("chat", "opus_4_8", "max"): AgentIdentity(
        "claude_web_chat_opus4.8_max_var", "claude_web_chat_opus4.8_max_var"
    ),
}


# Signatures (mode defaults to "chat"):
#   chat: ("chat", chatgpt_web.model, chatgpt_web.intelligence)
#   work: ("work", chatgpt_web.model, chatgpt_web.effort, chatgpt_web.speed)
# The chat picker is model (submenu) + intelligence (radios); the work
# picker is model + effort + speed under the pill's Advanced section.
# intelligence=None entries are the one-axis-era runs (model carried the
# legacy instant/thinking/pro values), kept for DB continuity.
_V1_CHATGPT_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", None, None): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("chat", "instant", None): AgentIdentity(
        "chatgpt_instant", "chatgpt_instant"
    ),
    ("chat", "thinking", None): AgentIdentity(
        "chatgpt_thinking", "chatgpt_thinking"
    ),
    ("chat", "pro", None): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
    # 2026-07 benchmark refresh (collision-checked + signed off 2026-07-21):
    ("chat", "gpt_5_6_sol", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.6_sol_pro", "chatgpt_web_chat_gpt5.6_sol_pro"
    ),
    ("work", "gpt_5_6_sol", "ultra", "standard"): AgentIdentity(
        "chatgpt_web_work_gpt5.6_sol_ultra", "chatgpt_web_work_gpt5.6_sol_ultra"
    ),
    # 2026-07-24 grading-variance experiment (chat-mode GPT-5.5 at Pro
    # intelligence; collision-checked against DB agent_model_name + S3
    # prefixes; _var suffix separates it from any future production wave):
    ("chat", "gpt_5_5", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.5_pro_var", "chatgpt_web_chat_gpt5.5_pro_var"
    ),
}


# =========================================================================
# V2 identity tables — MBABenchV2 task set (MBABenchV2 DB)
# =========================================================================

# Signature: (claude_web.mode, claude_web.model). Mode joined the key
# 2026-08-12 so chat and cowork cohorts stay separable in the DB; the
# chat entries keep the original model-only labels for continuity with
# rows recorded before the change. Extend the tuple — and every entry —
# when adding another Claude field that should bifurcate the DB label
# (e.g. effort, once V2 runs start pinning it).
_V2_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", "sonnet_4_6"): AgentIdentity(
        "claude_sonnet_4_6", "claude_sonnet_4_6"
    ),
    ("chat", "opus_4_6"): AgentIdentity("claude_opus_4_6", "claude_opus_4_6"),
    ("chat", "opus_4_8"): AgentIdentity("claude_opus_4_8", "claude_opus_4_8"),
    ("chat", "haiku_4_5"): AgentIdentity("claude_haiku_4_5", "claude_haiku_4_5"),
    ("chat", "fable_5"): AgentIdentity("claude_fable_5", "claude_fable_5"),
    # 2026-08-12: cowork cohort (collision-checked against
    # task_attempts.agent_model_name in both DBs — 0 rows).
    ("cowork", "fable_5"): AgentIdentity(
        "claude_fable_5_cowork", "claude_fable_5_cowork"
    ),
}


# When agent_mode=True, ChatGPT Agent is its own routed backend — the
# `model` dropdown becomes cosmetic, so agent-mode runs always collapse to
# one identity regardless of chatgpt_web.model. Only non-agent runs
# bifurcate by model.
_V2_CHATGPT_AGENT_IDENTITY = AgentIdentity("chatgpt_agent", "chatgpt_agent")

# Signature for non-agent-mode runs: (chatgpt_web.mode, chatgpt_web.model).
# Mode joined the key 2026-08-12 (chat entries keep their original labels
# for DB continuity). model=None means "let the session default win"; that
# + agent_mode=False is the legacy `chatgpt_web` label, kept for DB
# continuity with pre-refactor rows.
_V2_CHATGPT_NON_AGENT_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", None): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("chat", "instant"): AgentIdentity("chatgpt_instant", "chatgpt_instant"),
    ("chat", "thinking"): AgentIdentity(
        "chatgpt_thinking", "chatgpt_thinking"
    ),
    ("chat", "pro"): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
    # 2026-08-12: GPT-5.6 Sol for v2 runs (collision-checked against
    # task_attempts.agent_model_name in both DBs — 0 rows). Mirrors the
    # model-only v2 Claude naming (claude_fable_5 -> chatgpt_gpt_5_6_sol).
    ("chat", "gpt_5_6_sol"): AgentIdentity(
        "chatgpt_gpt_5_6_sol", "chatgpt_gpt_5_6_sol"
    ),
    # 2026-08-12: work-mode cohort (collision-checked in both DBs — 0 rows).
    ("work", "gpt_5_6_sol"): AgentIdentity(
        "chatgpt_gpt_5_6_sol_work", "chatgpt_gpt_5_6_sol_work"
    ),
}


def resolve_agent_identity(cfg: SimpleNamespace) -> AgentIdentity:
    provider = getattr(getattr(cfg, "provider", None), "kind", None)
    bench = _benchmark(cfg)
    if provider == "claude":
        return (
            _resolve_claude_v1(cfg) if bench == "v1" else _resolve_claude_v2(cfg)
        )
    if provider == "chatgpt":
        return (
            _resolve_chatgpt_v1(cfg) if bench == "v1" else _resolve_chatgpt_v2(cfg)
        )
    raise UnknownAgentCombination(
        f"provider.kind={provider!r} has no identity resolver. "
        f"Add one in infra/configs/agent_identity.py."
    )


def _claude_block(cfg: SimpleNamespace) -> SimpleNamespace:
    block = getattr(cfg, "claude_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=claude but cfg.claude_web block is missing."
        )
    return block


def _chatgpt_block(cfg: SimpleNamespace) -> SimpleNamespace:
    block = getattr(cfg, "chatgpt_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=chatgpt but cfg.chatgpt_web block is missing."
        )
    return block


# ---- v1 resolvers ----------------------------------------------------------


def _resolve_claude_v1(cfg: SimpleNamespace) -> AgentIdentity:
    block = _claude_block(cfg)
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    effort = getattr(block, "effort", None)
    key = (mode, model, effort)
    try:
        return _V1_CLAUDE_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v1 Claude identity for (claude_web.mode, claude_web.model, "
            f"claude_web.effort)={key!r}. Known: {list(_V1_CLAUDE_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


def _resolve_chatgpt_v1(cfg: SimpleNamespace) -> AgentIdentity:
    block = _chatgpt_block(cfg)
    if bool(getattr(block, "agent_mode", False)):
        raise UnknownAgentCombination(
            "chatgpt_web.agent_mode=true, but Agent mode no longer exists "
            "in the ChatGPT UI (removed ~mid-2026) — the run would silently "
            "execute as a non-agent chat under the wrong DB label. Set "
            "agent_mode: false and pick chatgpt_web.model + "
            "chatgpt_web.intelligence instead."
        )
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    if mode == "work":
        effort = getattr(block, "effort", None)
        speed = getattr(block, "speed", None) or "standard"
        key = (mode, model, effort, speed)
        axes = "(mode, model, effort, speed)"
    else:
        intelligence = getattr(block, "intelligence", None)
        key = (mode, model, intelligence)
        axes = "(mode, model, intelligence)"
    try:
        return _V1_CHATGPT_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v1 ChatGPT identity for chatgpt_web {axes}={key!r}. "
            f"Known: {list(_V1_CHATGPT_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


# ---- v2 resolvers ----------------------------------------------------------


def _resolve_claude_v2(cfg: SimpleNamespace) -> AgentIdentity:
    block = _claude_block(cfg)
    model = getattr(block, "model", None)
    mode = (getattr(block, "mode", None) or "chat").lower()
    key = (mode, model)
    try:
        return _V2_CLAUDE_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v2 Claude identity for "
            f"(claude_web.mode, claude_web.model)={key!r}. "
            f"Known: {list(_V2_CLAUDE_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


def _resolve_chatgpt_v2(cfg: SimpleNamespace) -> AgentIdentity:
    block = _chatgpt_block(cfg)
    agent_mode = bool(getattr(block, "agent_mode", True))
    if agent_mode:
        # ChatGPT Agent is its own backend; chatgpt_web.model is cosmetic.
        return _V2_CHATGPT_AGENT_IDENTITY
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    key = (mode, model)
    try:
        return _V2_CHATGPT_NON_AGENT_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v2 ChatGPT identity for "
            f"(chatgpt_web.mode, chatgpt_web.model, agent_mode=False)"
            f"={key!r}. "
            f"Known (non-agent): {list(_V2_CHATGPT_NON_AGENT_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )
