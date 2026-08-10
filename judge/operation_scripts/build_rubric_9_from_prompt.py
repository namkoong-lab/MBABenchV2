"""Build the v2 judge rubric (rubric_9.json + rubric_9_weights.json) from the
v2 benchmark build prompt.

The 132-check / 12-category rubric that V2 agents are graded against exists
byte-exact inside the step-2 build prompt
(gui-agents-master/tasks_configs/prompts_v2/step2_build.txt). This script
parses that text into the judge's rubric JSON format so the prompt shown to
agents and the rubric used by the judge can never drift apart.

Category weights come from the same prompt's stated weighting ("Category
weights (share of total score): Accuracy 18%, ..."). Per-check weights are
uniform within a category, summing to 100 (the judge normalizes by the
category sum, so only ratios matter).

Usage (from judge/):
    python operation_scripts/build_rubric_9_from_prompt.py
Writes prompts/rubrics/rubric_9.json and prompts/rubrics/rubric_9_weights.json.
"""
import json
import re
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parents[1]
PROMPT = (
    JUDGE_DIR.parent
    / "gui-agents-master/tasks_configs/prompts_v2/step2_build.txt"
)
OUT_RUBRIC = JUDGE_DIR / "prompts/rubrics/rubric_9.json"
OUT_WEIGHTS = JUDGE_DIR / "prompts/rubrics/rubric_9_weights.json"

# Stated in the prompt: "Category weights (share of total score): ..."
CATEGORY_WEIGHTS = {
    "Accuracy": 0.18,
    "Formatting": 0.13,
    "Structure": 0.11,
    "Error Checks": 0.11,
    "Assumptions": 0.095,
    "Formulas": 0.09,
    "Flexibility": 0.08,
    "Potential Dangers": 0.065,
    "Documentation": 0.05,
    "Model Outputs & Executive Summary": 0.05,
    "Purpose & Scope": 0.03,
    "Rounding": 0.01,
}

# Section headers in the prompt are uppercase; map to canonical names.
HEADER_TO_NAME = {k.upper(): k for k in CATEGORY_WEIGHTS}


def parse(text: str) -> dict[str, list[dict]]:
    # The rubric starts at the first ==== banner after the marker line.
    start = text.index("== FULL RUBRIC")
    body = text[start:]
    # Split into "==== / HEADER / ====" sections.
    parts = re.split(r"={10,}\n([A-Z &']+)\n={10,}\n", body)
    # parts[0] is the preamble before the first header; then (header, content) pairs.
    rubric: dict[str, list[dict]] = {}
    for header, content in zip(parts[1::2], parts[2::2]):
        name = HEADER_TO_NAME.get(header.strip())
        if name is None:
            raise SystemExit(f"Unknown rubric section header: {header!r}")
        checks = []
        for block in re.split(r"-{10,}\n", content):
            block = block.strip("\n").strip()
            if not block:
                continue
            m = re.match(
                r"(?P<name>[^\n]+)\n(?P<desc>.*?)\n\s*Good:\s*(?P<good>.*?)\n\s*Bad:\s*(?P<bad>.*)\Z",
                block,
                re.S,
            )
            if not m:
                raise SystemExit(
                    f"Unparseable check block in {name}:\n{block[:200]}"
                )
            clean = lambda s: re.sub(r"\s+", " ", s).strip()
            checks.append(
                {
                    "name": clean(m.group("name")),
                    "description": clean(m.group("desc")),
                    "good": clean(m.group("good")),
                    "bad": clean(m.group("bad")),
                }
            )
        if not checks:
            raise SystemExit(f"No checks parsed for category {name}")
        rubric[name] = checks
    return rubric


def main() -> int:
    text = PROMPT.read_text()
    rubric = parse(text)

    n_checks = sum(len(v) for v in rubric.values())
    assert set(rubric) == set(CATEGORY_WEIGHTS), (
        set(rubric) ^ set(CATEGORY_WEIGHTS)
    )
    assert n_checks == 132, f"expected 132 checks, parsed {n_checks}"
    assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9

    weights = {"CategoryWeights": [dict(CATEGORY_WEIGHTS)]}
    for cat, checks in rubric.items():
        # Uniform within-category weights summing to 100 (ratios are all
        # the scorer uses; 100 mirrors the rubric_6_weights convention).
        w = round(100.0 / len(checks), 4)
        weights[cat] = [{"name": c["name"], "weight": w} for c in checks]

    OUT_RUBRIC.write_text(json.dumps(rubric, indent=2) + "\n")
    OUT_WEIGHTS.write_text(json.dumps(weights, indent=2) + "\n")
    for cat, checks in rubric.items():
        print(f"{len(checks):3d}  {cat}")
    print(f"Wrote {OUT_RUBRIC.name} ({n_checks} checks) and {OUT_WEIGHTS.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
