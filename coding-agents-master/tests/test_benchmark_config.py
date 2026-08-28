"""Offline checks for the benchmark (v1|v2) switch and the v8 template.

Run from coding-agents-master:  python tests/test_benchmark_config.py
No Docker, DB, S3, or API keys needed.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coding_agent.config import load_config  # noqa: E402
from coding_agent.prompt_builder import (  # noqa: E402
    parse_prompt_version,
    prompt_file_paths,
    template_name,
)


def _cfg(extra: str) -> str:
    base = """
mode: internal
agent_model_name: claudecode_anthropic/claude-haiku-4-5
"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as f:
        f.write(base + extra)
        return f.name


def main() -> int:
    # v1 default: unchanged historical wiring.
    v1 = load_config(_cfg(""))
    assert v1.benchmark == "v1"
    assert v1.template_version == "v7"
    assert v1.s3_root == "BizbenchV1"
    print("OK  default config -> v1, v7, BizbenchV1 root")

    # v2: root/template flip together (v9 = the Questions-sheet template).
    v2 = load_config(_cfg("benchmark: v2\n"))
    assert v2.benchmark == "v2"
    assert v2.template_version == "v9"
    assert v2.s3_root == "MBABenchV2"
    assert v2.s3_bucket  # from config/config.yaml aws.s3_bucket or the default
    print("OK  benchmark v2 -> v9, MBABenchV2 root")

    # the old internal: stanza is refused, not silently honoured.
    try:
        load_config(_cfg("benchmark: v2\ninternal:\n  s3_root: BizbenchV1\n"))
        raise AssertionError("internal: should be refused")
    except ValueError as e:
        assert "internal" in str(e)
        print("OK  stale internal: stanza refused")

    # bad benchmark refused.
    try:
        load_config(_cfg("benchmark: v3\n"))
        raise AssertionError("benchmark v3 should be refused")
    except ValueError:
        print("OK  benchmark v3 refused")

    # v9 template resolves for every task_source, passes the rubric
    # checksum guard, and yields prompt_version 109.
    for src in ("fmwc", "modeloff", "wsp", "jp"):
        assert template_name(src, "v9") == "task_template_shared_v9.txt"
    sys_path, tpl_path = prompt_file_paths(v2, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 109
    print("OK  v9 template: shared across sources, checksum guard passed, pv=109")

    # v9 carries the Questions-sheet convention; its rubric is byte-identical
    # to v8's (guard hashes are the same constant).
    tpl_text = tpl_path.read_text()
    assert "ANSWERS (the 'Questions' sheet)" in tpl_text
    assert "8. The 'Questions' sheet is intact" in tpl_text
    assert "start solution.xlsx as a copy of the starting workbook" in tpl_text
    assert "the reserved answer cells are in the column headed 'Answers'" in tpl_text, (
        "step-1 plan bullet lost the header-anchored answer-column wording"
    )
    assert "reserved answer cells in column B (" not in tpl_text, (
        "stale hard-coded column-B plan wording survived"
    )
    print("OK  v9 template carries the Questions-sheet convention")

    # v8 still selectable and guarded: pv 108.
    v2_v8 = load_config(_cfg("benchmark: v2\ntemplate_version: v8\n"))
    sys_path, tpl_path = prompt_file_paths(v2_v8, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 108
    print("OK  v8 template still selectable, checksum guard passed, pv=108")

    # v7 unchanged: still pv 107 with its own guard.
    sys_path, tpl_path = prompt_file_paths(v1, "fmwc")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 107
    print("OK  v7 template unchanged, pv=107")

    print("ALL BENCHMARK CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
