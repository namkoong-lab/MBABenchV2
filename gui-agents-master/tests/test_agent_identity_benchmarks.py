"""Offline unit checks for dual-benchmark identity resolution.

Run from gui-agents-master:  .venv/bin/python tests/test_agent_identity_benchmarks.py
No browser, DB, or AWS involved.
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

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
                           speed="standard", agent_mode=False),
     "chatgpt_web_work_gpt5.6_sol_ultra"),
    ("v1", "chatgpt", dict(mode="chat", model="gpt_5_5", intelligence="pro",
                           agent_mode=False),
     "chatgpt_web_chat_gpt5.5_pro_var"),
    ("v2", "claude", dict(model="fable_5"), "claude_fable_5"),
    ("v2", "claude", dict(mode="chat", model="fable_5"), "claude_fable_5"),
    ("v2", "claude", dict(mode="cowork", model="fable_5"),
     "claude_fable_5_cowork"),
    ("v2", "claude", dict(model="opus_4_8"), "claude_opus_4_8"),
    ("v2", "chatgpt", dict(model="pro", agent_mode=False), "chatgpt_web_pro"),
    ("v2", "chatgpt", dict(model="gpt_5_6_sol", agent_mode=False),
     "chatgpt_gpt_5_6_sol"),
    ("v2", "chatgpt", dict(mode="work", model="gpt_5_6_sol", agent_mode=False),
     "chatgpt_gpt_5_6_sol_work"),
    ("v2", "chatgpt", dict(model="pro", agent_mode=True), "chatgpt_agent"),
]


def main() -> int:
    for bench, prov, block, want in CASES:
        got = resolve_agent_identity(cfg(bench, prov, block)).model_name
        assert got == want, f"{bench}/{prov}/{block}: {got} != {want}"
        print(f"OK  {bench} {prov:8s} -> {got}")

    try:
        resolve_agent_identity(cfg("v1", "chatgpt", dict(agent_mode=True)))
        raise AssertionError("v1 agent_mode=True should be refused")
    except UnknownAgentCombination:
        print("OK  v1 agent_mode refused")

    try:
        resolve_agent_identity(cfg("v3", "claude", dict(model="fable_5")))
        raise AssertionError("benchmark v3 should be refused")
    except UnknownAgentCombination:
        print("OK  benchmark v3 refused")

    try:
        resolve_agent_identity(
            cfg("v2", "claude", dict(mode="cowork", model="opus_4_8"))
        )
        raise AssertionError("v2 cowork opus_4_8 has no entry; should refuse")
    except UnknownAgentCombination:
        print("OK  v2 cowork without a table entry refused")

    print("ALL IDENTITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
