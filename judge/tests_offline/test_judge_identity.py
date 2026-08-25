"""Offline tests for the judge identity registry (utils/judge_identity.py).

No DB, S3, or LLM calls. Mirrors coding-agents-master/tests/test_agent_identity.py:
registry validation (duplicate label / duplicate axes / missing key / bad
provider), unknown-label refusal with a paste-ready stanza, and get_client
picking the right base_url per provider.

Run:  python judge/tests_offline/test_judge_identity.py
"""

import os
import sys
import tempfile
from pathlib import Path

_JUDGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JUDGE_ROOT))

from utils.judge_identity import (  # noqa: E402
    PROVIDERS,
    REGISTRY_PATH,
    JudgeIdentityError,
    load_registry,
    resolve_judge_identity,
)

_failures = []


def check(cond, msg):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


def _write_registry(text: str) -> Path:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", prefix="judge_ids_", delete=False
    )
    f.write(text)
    f.close()
    return Path(f.name)


def _expect_error(fn, needle: str, msg: str):
    try:
        fn()
    except JudgeIdentityError as e:
        check(needle in str(e), f"{msg} (error mentions {needle!r})")
    else:
        check(False, f"{msg} — no JudgeIdentityError raised")


VALID = """
- grader_model: google/gemini-2.5-pro
  provider: openrouter
  model: google/gemini-2.5-pro
  effort: minimal
- grader_model: openai/gpt-5.5
  provider: openai
  model: gpt-5.5
  effort: none
"""


def main() -> int:
    print("Step 1: valid registry loads; identity fields resolve")
    path = _write_registry(VALID)
    reg = load_registry(path)
    check(set(reg) == {"google/gemini-2.5-pro", "openai/gpt-5.5"}, "both labels load")
    ident = resolve_judge_identity("openai/gpt-5.5", path)
    check(ident.provider == "openai", "provider pinned")
    check(ident.model == "gpt-5.5", "wire model id pinned")
    check(ident.effort == "none", "effort pinned")
    check(ident.base_url is None, "openai uses the SDK default base_url")
    check(ident.api_key_provider == "openai", "openai key entry")
    check(
        ident.settings() == {"provider": "openai", "model": "gpt-5.5", "effort": "none"},
        "settings() is the pinned dict",
    )
    check(
        resolve_judge_identity("google/gemini-2.5-pro", path).base_url
        == "https://openrouter.ai/api/v1",
        "openrouter base_url",
    )

    print("Step 2: duplicate label refused")
    dup_label = _write_registry(
        VALID
        + """
- grader_model: openai/gpt-5.5
  provider: openrouter
  model: openai/gpt-5.5
  effort: null
"""
    )
    _expect_error(lambda: load_registry(dup_label), "labels must be unique", "dup label")

    print("Step 3: duplicate axes refused")
    dup_axes = _write_registry(
        VALID
        + """
- grader_model: gpt-5.5-alias
  provider: openai
  model: gpt-5.5
  effort: none
"""
    )
    _expect_error(lambda: load_registry(dup_axes), "one combination", "dup axes")

    print("Step 4: missing key refused")
    missing = _write_registry(
        """
- grader_model: some/model
  provider: openrouter
  model: some/model
"""
    )
    _expect_error(lambda: load_registry(missing), "effort", "missing effort key")

    print("Step 5: null non-nullable and unknown provider refused")
    null_model = _write_registry(
        """
- grader_model: some/model
  provider: openrouter
  model: null
  effort: null
"""
    )
    _expect_error(lambda: load_registry(null_model), "may not be null", "null model")
    bad_provider = _write_registry(
        """
- grader_model: some/model
  provider: azure
  model: some-model
  effort: null
"""
    )
    _expect_error(lambda: load_registry(bad_provider), "provider must be one of", "bad provider")

    print("Step 6: unknown label refuses and prints a paste-ready stanza")
    _expect_error(
        lambda: resolve_judge_identity("brand/new-model", path),
        "- grader_model: brand/new-model",
        "unknown label stanza",
    )
    _expect_error(lambda: resolve_judge_identity("", path), "--model", "empty label")

    print("Step 7: the committed registry loads and pins the default grader")
    committed = load_registry(REGISTRY_PATH)
    check(len(committed) > 0, f"{REGISTRY_PATH.name} loads ({len(committed)} entries)")
    check(
        "google/gemini-2.5-pro" in committed,
        "default_grader (google/gemini-2.5-pro) is registered",
    )
    for label, i in committed.items():
        if i.provider == "openrouter" and label.startswith("openai/"):
            check(False, f"{label}: openai/* must never route via openrouter")
    check(
        all(i.provider in PROVIDERS for i in committed.values()),
        "every committed provider is known",
    )

    print("Step 8: get_client builds the right base_url per provider")
    for env in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ[env] = "test-key-offline"
    from utils import llm_utils

    llm_utils._clients.clear()
    multi = _write_registry(
        VALID
        + """
- grader_model: anthropic/claude-opus-4-8
  provider: anthropic
  model: claude-opus-4-8
  effort: null
- grader_model: google/gemini-3-flash-preview
  provider: gemini
  model: gemini-3-flash-preview
  effort: minimal
"""
    )
    expected = {
        "google/gemini-2.5-pro": "https://openrouter.ai/api/v1",
        "openai/gpt-5.5": "https://api.openai.com/v1",
        "anthropic/claude-opus-4-8": "https://api.anthropic.com/v1",
        "google/gemini-3-flash-preview": "https://generativelanguage.googleapis.com/v1beta/openai",
    }
    for label, base in expected.items():
        client = llm_utils.get_client(resolve_judge_identity(label, multi))
        check(
            str(client.base_url).rstrip("/") == base,
            f"{label} -> {base}",
        )
    check(len(llm_utils._clients) == 4, "one cached client per provider")
    llm_utils._clients.clear()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        return 1
    print("All judge-identity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
