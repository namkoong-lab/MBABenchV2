#!/usr/bin/env python3
"""Generate coding_agent/prompts/task_template_shared_v14.txt and _v15.txt — the
Stage 5 prompt-ablation arms (prompt_version 114 / 115) run on the production
Claude Code / Fable 5.1 max identity (HANDOFF_STAGE5_PROMPT_ABLATION_2026-09-23.md).

v13 (pv 113) stays the master prompt: v10 + the HOUSE STANDARDS pointer, with
house_standards_v1.md staged into the workspace as HOUSE_STANDARDS.md. The two
arms are existing templates re-cut BYTE-IDENTICAL under new numbers (Patrick,
2026-09-23):

  v14 (pv 114) = the v9 text: the current prompt with the 132-check rubric
                 added back, and NO house standards (nothing staged).
  v15 (pv 115) = the v10 text: the current prompt minus the house standards —
                 rubric-free, no HOUSE_STANDARDS.md pointer, nothing staged.

Why new numbers rather than reusing 109/110: those prompt_versions already carry
the 2026-09-08 rubric-effect experiment's recorded, graded rows on the older
claude-fable-5-max identity; the judge's latest-prompt guard and the good-attempt
manifest key on prompt_version, so the ablation cohorts get their own numbers
(the same reason v13 was cut instead of reusing 111).

Neither arm declares TEMPLATE_EXTRAS or TEMPLATE_ATTACHMENTS: the absence of the
house standards is the point of the ablation, and a row under 114/115 therefore
carries no extra_configs.house_standards.

Deterministic: v14 = v9 (whole-file md5 pinned in prompt_builder.RECUT_MD5; v9's
own rubric-section guard applies to it too) and v15 = v10 (pinned in
prompt_builder.SCRUBBED_MD5 and guarded rubric-free, like v10 itself).

    uv run --no-sync python tools/build_v14_v15_templates.py [--check]
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "coding_agent" / "prompts"

# (source, target, source md5 as pinned in prompt_builder, target prompt_version)
ARMS = [
    ("task_template_shared_v9.txt", "task_template_shared_v14.txt", "7610bdae4c5eba21984ebcc9b19e3886", 114),
    ("task_template_shared_v10.txt", "task_template_shared_v15.txt", "5b607903172352e82f8fbbac986d96ef", 115),
]


def main() -> int:
    check = "--check" in sys.argv
    rc = 0
    for src_name, dst_name, src_md5, pv in ARMS:
        src, dst = PROMPTS / src_name, PROMPTS / dst_name
        data = src.read_bytes()
        if hashlib.md5(data).hexdigest() != src_md5:
            print(f"{src_name} no longer matches its pinned md5 — refusing to derive {dst_name} from a drifted source")
            return 1
        text = data.decode()
        assert "HOUSE_STANDARDS.md" not in text, f"{src_name} unexpectedly points at HOUSE_STANDARDS.md"
        if dst_name.endswith("v14.txt"):
            assert text.count("== FULL RUBRIC") == 1, "v14 must carry the inlined rubric (it is v9)"
        else:
            assert "== FULL RUBRIC" not in text and "rubric" not in text.lower(), "v15 must stay rubric-free (it is v10)"
        if check:
            if not dst.exists() or dst.read_bytes() != data:
                print(f"DRIFT: {dst_name} differs from {src_name} (or is missing)")
                rc = 1
                continue
        else:
            dst.write_bytes(data)
        print(f"{dst_name}: {len(data):,} bytes md5 {src_md5} (== {src_name}, prompt_version {pv})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
