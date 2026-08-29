"""Offline checks for the agent identity registry.

Run from coding-agents-master:  python tests/test_agent_identity.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coding_agent.agent_identity import (  # noqa: E402
    REGISTRY_PATH,
    AgentIdentityError,
    load_registry,
    resolve_agent_identity,
)
from coding_agent.config import load_config  # noqa: E402

GOOD = """
- agent_model_name: a/claude-x-max
  cli: claude
  model: claude-x
  effort: max
  extra_args: []
  env: {}
- agent_model_name: b/codex-y
  cli: codex
  model: gpt-y
  effort: null
  extra_args: ["--foo"]
  env: {K: "1"}
"""


def _registry(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


def _expect(fn, needle: str) -> None:
    try:
        fn()
    except AgentIdentityError as e:
        assert needle in str(e), str(e)
        return
    raise AssertionError(f"expected refusal mentioning {needle!r}")


def main() -> int:
    # The committed registry loads and pins both v1 wave cohorts.
    reg = load_registry(REGISTRY_PATH)
    assert reg["claudecode_anthropic/claude-fable-5-max"].axes == ("claude", "claude-fable-5", "max")
    assert reg["codex_openai/gpt-5.6-sol-xhigh"].axes == ("codex", "gpt-5.6-sol", "xhigh")
    print("OK  committed registry loads")

    path = _registry(GOOD)
    ident = resolve_agent_identity({"agent_model_name": "b/codex-y"}, path)
    assert ident.extra_args == ["--foo"] and ident.env == {"K": "1"} and ident.effort is None
    assert ident.extra_configs() == {"cli": "codex", "model": "gpt-y", "effort": None,
                                     "extra_args": ["--foo"], "env": {"K": "1"}}
    print("OK  label resolves to its pinned settings")

    _expect(lambda: resolve_agent_identity({}, path), "agent_model_name")
    _expect(lambda: resolve_agent_identity({"agent_model_name": "nope"}, path), "- agent_model_name: nope")
    _expect(lambda: resolve_agent_identity(
        {"agent_model_name": "a/claude-x-max", "agent": {"model": "claude-x"}}, path), "agent")
    _expect(lambda: resolve_agent_identity(
        {"agent_model_name": "a/claude-x-max", "model": "claude-x"}, path), "model")
    print("OK  missing / unknown label and agent keys in the config are refused")

    _expect(lambda: load_registry(_registry(GOOD + GOOD.split("- agent_model_name: b")[0])),
            "already used")
    dup_axes = GOOD.replace("a/claude-x-max", "z/other", 1).replace("b/codex-y", "a/claude-x-max")
    _expect(lambda: load_registry(_registry(GOOD + "\n" + dup_axes.split("- agent_model_name: a/claude")[0])),
            "already mapped")
    _expect(lambda: load_registry(_registry("- agent_model_name: x\n  cli: claude\n  model: m\n")),
            "missing 'effort'")
    _expect(lambda: load_registry(_registry(
        "- agent_model_name: x\n  cli: bash\n  model: m\n  effort: null\n  extra_args: []\n  env: {}\n")),
        "cli must be")
    print("OK  duplicate labels / axes and incomplete entries are refused")

    # load_config builds AgentConfig from the identity and refuses stale keys.
    cfg = load_config(ROOT / "run_configs" / "example_codex.yaml")
    assert cfg.agent.cli == "codex" and cfg.agent.model == "gpt-5.6-sol" and cfg.agent.effort == "xhigh"
    assert cfg.agent_model_name == "codex_openai/gpt-5.6-sol-xhigh"
    assert cfg.extra_configs()["sandbox_image"] == "mbabench-coding-agent:v2"
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("mode: internal\nidentity: old\nagent:\n  cli: claude\n  model: m\n")
    f.close()
    try:
        load_config(f.name)
        raise AssertionError("stale keys should be refused")
    except ValueError as e:
        assert "identity" in str(e) and "agent" in str(e)
    print("OK  load_config: identity -> AgentConfig, stale keys refused")

    print("ALL AGENT IDENTITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
