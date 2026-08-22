"""Offline unit checks for dual-benchmark identity resolution.

Run from gui-agents-master:  python -m pytest tests/test_agent_identity_benchmarks.py
No browser, DB, or AWS involved.
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.configs.agent_identity import (  # noqa: E402
    UnknownAgentCombination,
    resolve_agent_identity,
)


def cfg(bench, provider, block):
    return NS(
        benchmark=bench,
        provider=NS(kind=provider),
        **{f"{provider}_web": NS(**block)},
    )


CASES = [
    ("v1", "claude", dict(mode="cowork", model="fable_5", effort="max"),
     "claude_web_cowork_fable5_max"),
    ("v1", "claude", dict(mode="chat", model="sonnet_4_6", effort=None),
     "claude_web"),
    ("v1", "chatgpt", dict(mode="work", model="gpt_5_6_sol", effort="ultra",
                           speed="standard"),
     "chatgpt_web_work_gpt5.6_sol_ultra"),
    ("v1", "chatgpt", dict(mode="chat", model="gpt_5_5", intelligence="pro"),
     "chatgpt_web_chat_gpt5.5_pro_var"),
    # effort bifurcates the v2 Claude label and rides in the name
    ("v2", "claude", dict(model="fable_5", effort="max"), "claude_fable_5_max"),
    ("v2", "claude", dict(mode="chat", model="fable_5", effort="max"),
     "claude_fable_5_max"),
    ("v2", "claude", dict(mode="cowork", model="fable_5", effort="max"),
     "claude_fable_5_cowork_max"),
    ("v2", "claude", dict(model="opus_4_8", effort="max"),
     "claude_opus_4_8_max"),
    # haiku exposes no Effort control, so the axis stays out of its label and
    # the pinned default and an explicit null land on the same cohort
    ("v2", "claude", dict(model="haiku_4_5", effort="max"), "claude_haiku_4_5"),
    ("v2", "claude", dict(model="haiku_4_5", effort=None), "claude_haiku_4_5"),
    ("v2", "chatgpt", dict(model="pro"), "chatgpt_web_pro"),
    ("v2", "chatgpt", dict(model="gpt_5_6_sol"), "chatgpt_gpt_5_6_sol"),
    ("v2", "chatgpt", dict(model="gpt_5_6_sol", intelligence="pro"),
     "chatgpt_gpt_5_6_sol_pro"),
    # intelligence bifurcates the v2 chat label; None keeps the bare one, so
    # cohorts recorded before that axis existed still resolve to their label
    ("v2", "chatgpt", dict(model="gpt_5_5"), "chatgpt_gpt_5_5"),
    ("v2", "chatgpt", dict(model="gpt_5_5", intelligence=None),
     "chatgpt_gpt_5_5"),
    ("v2", "chatgpt", dict(model="gpt_5_5", intelligence="instant"),
     "chatgpt_gpt_5_5_instant"),
    ("v2", "chatgpt", dict(model="gpt_5_6_sol", intelligence=None),
     "chatgpt_gpt_5_6_sol"),
    # work mode has no intelligence axis; setting one must not move its label
    ("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol", effort="ultra",
                           speed="standard", intelligence="pro"),
     "chatgpt_gpt_5_6_sol_work_ultra"),
    ("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol", effort="ultra",
                           speed="standard"),
     "chatgpt_gpt_5_6_sol_work_ultra"),
    # null speed is the UI's "standard"; both spell the same work cohort
    ("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol", effort="ultra",
                           speed=None),
     "chatgpt_gpt_5_6_sol_work_ultra"),
]


@pytest.mark.parametrize("bench,prov,block,want", CASES)
def test_identity_resolves(bench, prov, block, want):
    got = resolve_agent_identity(cfg(bench, prov, block)).model_name
    assert got == want, f"{bench}/{prov}/{block}: {got} != {want}"


def test_unknown_benchmark_refused():
    with pytest.raises(UnknownAgentCombination):
        resolve_agent_identity(cfg("v3", "claude", dict(model="fable_5")))


def test_combination_without_a_table_entry_refused():
    """An unlisted axis combination must force a naming decision, not
    invent a label for the DB."""
    with pytest.raises(UnknownAgentCombination):
        resolve_agent_identity(
            cfg("v2", "claude", dict(mode="cowork", model="opus_4_8"))
        )


def test_v2_off_default_reasoning_axes_refused():
    """effort/speed key the v2 labels, so a cohort at a new setting must be
    named before it can write rows — not silently reuse the max/ultra label."""
    with pytest.raises(UnknownAgentCombination):
        resolve_agent_identity(
            cfg("v2", "claude", dict(model="fable_5", effort="high"))
        )
    with pytest.raises(UnknownAgentCombination):
        resolve_agent_identity(
            cfg("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol",
                                      effort="ultra", speed="fast"))
        )
