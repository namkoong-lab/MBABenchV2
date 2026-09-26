#!/usr/bin/env python3
"""Generate coding_agent/prompts/task_template_shared_v13.txt — the benchmark-v2
default: the rubric-free House Standards task template.

v13 (prompt_version 113) is the v11 text BYTE-IDENTICAL: v9 with every
rubric-derived passage removed (no weights paragraph, no conventions
summary, no "== FULL RUBRIC ==" block, no rubric-driven QA list) plus the
pointer to HOUSE_STANDARDS.md, which prompt_builder stages into the
workspace root from prompts/house_standards_v1.md. That is the design the
whole 101-task rerun runs on (gui/excel 204/205, cli v15): the agent gets
the task, the Questions-sheet answer mechanics, the harness guidance and the
house standards — nothing about grading.

Why a new number for the same text: prompt_version 111 already carries the
2026-09-08 rubric-effect experiment's recorded, graded rows on the same
claude-fable-5-max identity. The judge's latest-prompt guard and the
good-attempt manifest key on prompt_version, so reusing 111 would fold those
30 experiment rows into the rerun cohort. 113 keeps them apart (the same
reason v12 was cut instead of reusing 110).

Deterministic: v13 = v11 (which is itself pinned by SCRUBBED_MD5 and
reproducible from tools/build_v10_v11_templates.py).

    uv run python tools/build_v13_template.py [--check]
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "coding_agent" / "prompts"
V11 = PROMPTS / "task_template_shared_v11.txt"
V13 = PROMPTS / "task_template_shared_v13.txt"
V11_MD5 = "008713b35953c2d1356dd81e7f51d4e3"  # prompt_builder.SCRUBBED_MD5["task_template_shared_v11.txt"]


def main() -> int:
    check = "--check" in sys.argv
    text = V11.read_text()
    if hashlib.md5(text.encode()).hexdigest() != V11_MD5:
        print("v11 no longer matches its pinned md5 — refusing to derive v13 from a drifted source")
        return 1
    assert "== FULL RUBRIC" not in text and "rubric" not in text.lower() and text.count("HOUSE_STANDARDS.md") == 2
    if check:
        if not V13.exists() or V13.read_text() != text:
            print(f"DRIFT: {V13.name} differs from v11"); return 1
    else:
        V13.write_text(text)
    print(f"{V13.name}: {len(text):,} chars md5 {hashlib.md5(text.encode()).hexdigest()} (== v11)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
