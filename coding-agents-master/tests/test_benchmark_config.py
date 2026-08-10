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
run_name: t
mode: internal
identity: test_identity
agent:
  cli: claude
  model: claude-haiku-4-5
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
    assert (v1.internal.s3_bucket, v1.internal.s3_root) == ("mbabench", "BizbenchV1")
    print("OK  default config -> v1, v7, mbabench/BizbenchV1")

    # v2: bucket/root/template flip together.
    v2 = load_config(_cfg("benchmark: v2\n"))
    assert v2.benchmark == "v2"
    assert v2.template_version == "v8"
    assert (v2.internal.s3_bucket, v2.internal.s3_root) == ("mbabench", "MBABenchV2")
    print("OK  benchmark v2 -> v8, mbabench/MBABenchV2")

    # explicit internal overrides still win.
    v2b = load_config(_cfg("benchmark: v2\ninternal:\n  s3_bucket: custom\n"))
    assert v2b.internal.s3_bucket == "custom"
    assert v2b.internal.s3_root == "MBABenchV2"
    print("OK  explicit internal.s3_bucket override wins")

    # bad benchmark refused.
    try:
        load_config(_cfg("benchmark: v3\n"))
        raise AssertionError("benchmark v3 should be refused")
    except ValueError:
        print("OK  benchmark v3 refused")

    # v8 template resolves for every task_source, passes the rubric
    # checksum guard, and yields prompt_version 108.
    for src in ("fmwc", "modeloff", "wsp", "jp"):
        assert template_name(src, "v8") == "task_template_shared_v8.txt"
    sys_path, tpl_path = prompt_file_paths(v2, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 108
    print("OK  v8 template: shared across sources, checksum guard passed, pv=108")

    # v7 unchanged: still pv 107 with its own guard.
    sys_path, tpl_path = prompt_file_paths(v1, "fmwc")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 107
    print("OK  v7 template unchanged, pv=107")

    print("ALL BENCHMARK CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
