"""Derive the agent identity from the behavior-determining fields in cfg.

The identity is a pure function of the config fields that change agent
output, so `task_attempts.agent_model_name` cannot disagree with what the
run actually did.

BENCHMARK-AWARE: the repo runs two experiments with different DBs, prompt
sets, and label conventions. `cfg.benchmark` ("v1" | "v2") selects the
identity namespace:

  v1 — the MBABench V1 wave (BizbenchV1 DB, pv9 prompts). Labels bifurcate
       on every UI axis that changes agent output: Claude mode (chat/cowork)
       + model + effort; ChatGPT mode (chat/work) + model + intelligence or
       effort+speed.
  v2 — the MBABenchV2 task set (MBABenchV2 DB, rubric-v9 3-step prompts).
       Labels bifurcate on the same UI axes as v1: Claude mode + model +
       effort; ChatGPT mode + model + intelligence (chat) or effort + speed
       (work).

The tables below are APPEND-ONLY. Every label is referenced by rows already
in `task_attempts`, so editing one silently rewrites what those rows mean.
To add a mode, add an entry. Unknown combinations raise
`UnknownAgentCombination`, which forces a naming decision before an
unclassified label reaches the DB.
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
# claude.ai UI exposes reasoning effort and a Chat/Cowork mode toggle as
# first-class controls that change agent output, so both bifurcate the DB
# label. mode defaults to "chat"; effort=None matches a run that pinned no
# effort.
_V1_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", "sonnet_4_6", None): AgentIdentity("claude_web", "claude_web"),
    ("chat", "opus_4_6", None): AgentIdentity(
        "claude_opus_4_6", "claude_opus_4_6"
    ),
    ("chat", "haiku_4_5", None): AgentIdentity(
        "claude_haiku_4_5", "claude_haiku_4_5"
    ),
    ("chat", "fable_5", "max"): AgentIdentity(
        "claude_web_chat_fable5_max", "claude_web_chat_fable5_max"
    ),
    ("cowork", "fable_5", "max"): AgentIdentity(
        "claude_web_cowork_fable5_max", "claude_web_cowork_fable5_max"
    ),
    # _var: the grading-variance experiment, kept separate from a
    # production Opus 4.8 wave.
    ("chat", "opus_4_8", "max"): AgentIdentity(
        "claude_web_chat_opus4.8_max_var", "claude_web_chat_opus4.8_max_var"
    ),
}


# Signatures (mode defaults to "chat"):
#   chat: ("chat", chatgpt_web.model, chatgpt_web.intelligence)
#   work: ("work", chatgpt_web.model, chatgpt_web.effort, chatgpt_web.speed)
# The chat picker is model (submenu) + intelligence (radios); the work
# picker is model + effort + speed under the pill's Advanced section.
# intelligence=None entries match the one-axis model values
# (instant/thinking/pro), which carry no separate intelligence setting.
_V1_CHATGPT_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", None, None): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("chat", "instant", None): AgentIdentity(
        "chatgpt_instant", "chatgpt_instant"
    ),
    ("chat", "thinking", None): AgentIdentity(
        "chatgpt_thinking", "chatgpt_thinking"
    ),
    ("chat", "pro", None): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
    ("chat", "gpt_5_6_sol", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.6_sol_pro", "chatgpt_web_chat_gpt5.6_sol_pro"
    ),
    ("work", "gpt_5_6_sol", "ultra", "standard"): AgentIdentity(
        "chatgpt_web_work_gpt5.6_sol_ultra", "chatgpt_web_work_gpt5.6_sol_ultra"
    ),
    # _var: the grading-variance experiment, kept separate from a
    # production GPT-5.5 wave.
    ("chat", "gpt_5_5", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.5_pro_var", "chatgpt_web_chat_gpt5.5_pro_var"
    ),
}


# =========================================================================
# V2 identity tables — MBABenchV2 task set (MBABenchV2 DB)
# =========================================================================

# Signature: (claude_web.mode, claude_web.model, claude_web.effort). Mode is
# in the key so chat and cowork cohorts stay separable in the DB; the chat
# labels carry no mode segment.
#
# Effort joined the key (and the labels) after the cowork wave: it is a
# reasoning axis that changes agent output, so a max-effort answer is not the
# same result as a medium-effort one and the two must not share a label.
# `claude_web.effort` has defaulted to "max" for the whole life of this repo
# and every v2 template pins it, so keying the pre-existing cohorts at "max"
# is what they always were — the DB rows they wrote need the matching rename
# (see the backfill note in docs, scoped to prompt_version 201).
#
# Haiku is the exception: claude.ai exposes no Effort control for it, so the
# config value is inert there and stays out of the label. Both the pinned
# default and an explicit null map to the one bare haiku identity.
_V2_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", "sonnet_4_6", "max"): AgentIdentity(
        "claude_sonnet_4_6_max", "claude_sonnet_4_6_max"
    ),
    ("chat", "opus_4_6", "max"): AgentIdentity(
        "claude_opus_4_6_max", "claude_opus_4_6_max"
    ),
    ("chat", "opus_4_8", "max"): AgentIdentity(
        "claude_opus_4_8_max", "claude_opus_4_8_max"
    ),
    ("chat", "haiku_4_5", "max"): AgentIdentity(
        "claude_haiku_4_5", "claude_haiku_4_5"
    ),
    ("chat", "haiku_4_5", None): AgentIdentity(
        "claude_haiku_4_5", "claude_haiku_4_5"
    ),
    ("chat", "fable_5", "max"): AgentIdentity(
        "claude_fable_5_max", "claude_fable_5_max"
    ),
    ("cowork", "fable_5", "max"): AgentIdentity(
        "claude_fable_5_cowork_max", "claude_fable_5_cowork_max"
    ),
    ("cowork", "opus_5", "max"): AgentIdentity(
        "claude_opus_5_cowork_max", "claude_opus_5_cowork_max"
    ),
}


# Signatures (mode defaults to "chat"):
#   chat: (chatgpt_web.mode, chatgpt_web.model, chatgpt_web.intelligence)
#   work: (chatgpt_web.mode, chatgpt_web.model, chatgpt_web.effort,
#          chatgpt_web.speed)
# model=None means "let the session default win". Intelligence is the
# chat-mode reasoning axis and changes agent output, so it bifurcates the
# label — a GPT-5.5 answer at Instant is not the same result as one at Pro.
#
# intelligence=None keeps the bare label: those are the runs that pinned no
# intelligence and let the session default stand, which is every v2 chat
# cohort recorded before this axis was added. Reading `None` as its own key
# rather than folding it into a default is what keeps those rows meaning
# what they meant.
#
# Work mode does not carry this axis — its pickers are effort + speed, which
# are its own two reasoning knobs and now key its label the way v1 already
# does. Every v2 work run has been ultra/standard since the cohort started,
# so that entry is what the existing rows always were; they need the matching
# rename (backfill scoped to prompt_version 201). Speed stays out of the
# label while it is "standard" (the UI default), matching the v1 convention —
# a fast cohort becomes chatgpt_gpt_5_6_sol_work_ultra_fast.
_V2_CHATGPT_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", None, None): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("chat", "instant", None): AgentIdentity(
        "chatgpt_instant", "chatgpt_instant"
    ),
    ("chat", "thinking", None): AgentIdentity(
        "chatgpt_thinking", "chatgpt_thinking"
    ),
    ("chat", "pro", None): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
    ("chat", "gpt_5_6_sol", None): AgentIdentity(
        "chatgpt_gpt_5_6_sol", "chatgpt_gpt_5_6_sol"
    ),
    # Sol with the chat tier actually pinned (dispatcher template
    # chatgpt_sol56_chat.yaml). Distinct from the entry above, which is the
    # cohort that let the session default stand.
    ("chat", "gpt_5_6_sol", "pro"): AgentIdentity(
        "chatgpt_gpt_5_6_sol_pro", "chatgpt_gpt_5_6_sol_pro"
    ),
    ("chat", "gpt_5_5", None): AgentIdentity(
        "chatgpt_gpt_5_5", "chatgpt_gpt_5_5"
    ),
    ("chat", "gpt_5_5", "instant"): AgentIdentity(
        "chatgpt_gpt_5_5_instant", "chatgpt_gpt_5_5_instant"
    ),
    ("work", "gpt_5_6_sol", "ultra", "standard"): AgentIdentity(
        "chatgpt_gpt_5_6_sol_work_ultra", "chatgpt_gpt_5_6_sol_work_ultra"
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
    effort = getattr(block, "effort", None)
    key = (mode, model, effort)
    try:
        return _V2_CLAUDE_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v2 Claude identity for (claude_web.mode, claude_web.model, "
            f"claude_web.effort)={key!r}. "
            f"Known: {list(_V2_CLAUDE_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


def _resolve_chatgpt_v2(cfg: SimpleNamespace) -> AgentIdentity:
    block = _chatgpt_block(cfg)
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    if mode == "work":
        effort = getattr(block, "effort", None)
        # null speed is the UI's "standard", so both spell the same cohort.
        speed = getattr(block, "speed", None) or "standard"
        key = (mode, model, effort, speed)
        axes = "(mode, model, effort, speed)"
    else:
        key = (mode, model, getattr(block, "intelligence", None))
        axes = "(mode, model, intelligence)"
    try:
        return _V2_CHATGPT_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No v2 ChatGPT identity for chatgpt_web {axes}={key!r}. "
            f"Known: {list(_V2_CHATGPT_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )
