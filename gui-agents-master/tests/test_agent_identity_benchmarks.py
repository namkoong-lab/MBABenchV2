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
    ("v2", "claude", dict(model="fable_5"), "claude_fable_5"),
    ("v2", "claude", dict(mode="chat", model="fable_5"), "claude_fable_5"),
    ("v2", "claude", dict(mode="cowork", model="fable_5"),
     "claude_fable_5_cowork"),
    ("v2", "claude", dict(model="opus_4_8"), "claude_opus_4_8"),
    ("v2", "chatgpt", dict(model="pro"), "chatgpt_web_pro"),
    ("v2", "chatgpt", dict(model="gpt_5_6_sol"), "chatgpt_gpt_5_6_sol"),
    ("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol"),
     "chatgpt_gpt_5_6_sol_work"),
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
