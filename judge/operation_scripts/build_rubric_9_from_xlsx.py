"""Rebuild the rubric_9 pair and the live agent-prompt rubric blocks from the
canonical checklist workbook (the 2026-08 in-place revision).

Source of truth: Patrick's "v2 Financial Modeling Checklist to turn into
rubric.xlsx", sheet `Rubric Ass.` — 132 rows carrying No./Category/Name/
In-Category Weighting/Description/Good/Bad (plus Conditional score flag,
recorded for audit only). The sheet IS the annotation edition: names,
categories, order and the 37-check conditional set match the S3 suitability
annotations exactly.

What it writes (all in place, versions unchanged — Patrick-approved):
    judge/prompts/rubrics/rubric_9.json          (3-check swap + text revisions)
    judge/prompts/rubrics/rubric_9_weights.json  (xlsx weight system adopted)
    gui-agents-master/tasks_configs/prompts_v3/step2_build.txt   (live, pv202)
    gui-agents-master/tasks_configs/prompts/v2_2.txt             (live, pv203)
    excel-agents-master/...  byte-copies of the two gui files    (parity sets)

Frozen sets are never touched: gui/excel prompts_v2 (pv200), prompts/v2_1.txt
(pv201), CLI v12, coding v8.

After running this, regenerate the derived CLI/coding prompts:
    cd cli-agents-master    && python tools/build_v13_prompts.py
    cd coding-agents-master && python tools/build_v9_template.py
and refresh coding_agent/prompt_builder.py's V9_RUBRIC_MD5 / V9_RUBRIC_LEN
with the values the coding builder prints.

Usage (from the repo root):
    uv run python judge/operation_scripts/build_rubric_9_from_xlsx.py --roundtrip
        Regenerate every artifact from the CURRENT rubric_9 pair and require
        byte-identity with what is on disk (format lock; no writes).
    uv run python judge/operation_scripts/build_rubric_9_from_xlsx.py [--xlsx PATH]
        Parse + validate the workbook, then write all artifacts.
"""

import argparse
import json
import sys
from pathlib import Path

import openpyxl

JUDGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = JUDGE_ROOT.parent

RUBRIC_PATH = JUDGE_ROOT / "prompts/rubrics/rubric_9.json"
WEIGHTS_PATH = JUDGE_ROOT / "prompts/rubrics/rubric_9_weights.json"

# Live prompt files embedding the rubric block (weights sentence + full rubric).
GUI = REPO_ROOT / "gui-agents-master/tasks_configs"
EXCEL = REPO_ROOT / "excel-agents-master/tasks_configs"
LIVE_PROMPT_FILES = [
    GUI / "prompts_v3/step2_build.txt",
    GUI / "prompts/v2_2.txt",
]
# excel copies must stay byte-identical to their gui counterparts
# (excel-agents-master/tests/test_prompt_parity.py).
EXCEL_COPIES = {
    GUI / "prompts_v3/step2_build.txt": EXCEL / "prompts_v3/step2_build.txt",
    GUI / "prompts/v2_2.txt": EXCEL / "prompts/v2_2.txt",
}

RUBRIC_MARKER = "== FULL RUBRIC"
WEIGHTS_SENTENCE_PREFIX = "Category weights (share of total score): "

DEFAULT_XLSX = Path(
    "/Users/patrick/Downloads/v2 Financial Modeling Checklist to turn into rubric.xlsx"
)
SHEET = "Rubric Ass."
EXPECTED_CHECKS = 132
EXPECTED_CONDITIONAL = 37


# --------------------------------------------------------------------------
# Rendering (must reproduce the existing artifacts byte-for-byte — locked by
# --roundtrip against the pre-revision files)
# --------------------------------------------------------------------------


def dump_json(data) -> str:
    return json.dumps(data, indent=2) + "\n"


def render_weights_sentence(category_weights: dict) -> str:
    parts = []
    for cat, w in category_weights.items():
        pct = round(w * 100, 4)
        parts.append(f"{cat} {pct:g}%")
    return WEIGHTS_SENTENCE_PREFIX + ", ".join(parts) + "."


def render_rubric_block(rubric: dict) -> str:
    """The '== FULL RUBRIC' block exactly as the prompt files carry it."""
    n_checks = sum(len(v) for v in rubric.values())
    cat_blocks = []
    for category, checks in rubric.items():
        check_texts = [
            f"{c['name']}\n{c['description']}\n  Good: {c['good']}\n  Bad:  {c['bad']}"
            for c in checks
        ]
        sep = "\n\n" + "-" * 80 + "\n\n"
        cat_blocks.append(
            "=" * 80 + "\n" + category.upper() + "\n" + "=" * 80 + "\n\n"
            + sep.join(check_texts)
        )
    return (
        f"== FULL RUBRIC ({len(rubric)} categories, {n_checks} checks) ==\n\n"
        + "\n\n\n".join(cat_blocks)
        + "\n"
    )


def splice_prompt(text: str, rubric: dict, category_weights: dict) -> str:
    """Replace the weights-sentence tail and the rubric block in a prompt file."""
    # Weights sentence: from the prefix to the end of that line.
    start = text.index(WEIGHTS_SENTENCE_PREFIX)
    end = text.index("\n", start)
    text = text[:start] + render_weights_sentence(category_weights) + text[end:]
    # Rubric block: from the marker to EOF (both live files end with the rubric).
    return text[: text.index(RUBRIC_MARKER)] + render_rubric_block(rubric)


# --------------------------------------------------------------------------
# XLSX parsing + validation
# --------------------------------------------------------------------------


def parse_xlsx(xlsx_path: Path):
    """Parse `Rubric Ass.` into (rubric, weights, audit) dicts.

    Category key order follows first appearance in the sheet (the sheet is
    grouped by category, alphabetically — same order as the current
    rubric_9.json). CategoryWeights order is descending weight, ties by name.
    """
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    col = {name: header.index(name) for name in (
        "No.", "Category", "Name", "In-Category Weighting",
        "Overall Weighting of Question", "Description", "Good", "Bad",
        "Conditional score flag",
    )}

    errors = []
    entries = []
    for r in rows[1:]:
        if r is None or r[col["No."]] is None:
            continue
        entry = {
            "no": int(r[col["No."]]),
            "category": str(r[col["Category"]]).strip(),
            "name": str(r[col["Name"]]).strip(),
            "in_cat_w": r[col["In-Category Weighting"]],
            "overall_w": r[col["Overall Weighting of Question"]],
            "description": str(r[col["Description"]]).strip(),
            "good": str(r[col["Good"]]).strip(),
            "bad": str(r[col["Bad"]]).strip(),
            "conditional": bool(r[col["Conditional score flag"]]),
        }
        for field in ("category", "name", "description", "good", "bad"):
            if not entry[field] or entry[field] == "None":
                errors.append(f"row no={entry['no']}: empty {field}")
            if "\n" in entry[field]:
                errors.append(f"row no={entry['no']}: embedded newline in {field}")
        if entry["in_cat_w"] is None or entry["overall_w"] is None:
            errors.append(f"row no={entry['no']}: missing weight cell")
        entries.append(entry)
    wb.close()

    if len(entries) != EXPECTED_CHECKS:
        errors.append(f"expected {EXPECTED_CHECKS} data rows, found {len(entries)}")
    if [e["no"] for e in entries] != list(range(1, len(entries) + 1)):
        errors.append("No. column is not the contiguous sequence 1..132")

    # Group by category preserving sheet order.
    rubric, per_cat_w = {}, {}
    for e in entries:
        rubric.setdefault(e["category"], []).append(
            {
                "name": e["name"],
                "description": e["description"],
                "good": e["good"],
                "bad": e["bad"],
            }
        )
        per_cat_w.setdefault(e["category"], []).append(e)

    # Weight-system validation: in-category weights sum to 1 per category;
    # the implied category weight (overall / in-category) is constant within
    # each category; category weights sum to exactly 1.
    implied = {}
    for cat, es in per_cat_w.items():
        s = sum(e["in_cat_w"] for e in es)
        if abs(s - 1.0) > 1e-9:
            errors.append(f"{cat}: in-category weights sum to {s!r}, not 1")
        cat_ws = {round(e["overall_w"] / e["in_cat_w"], 9) for e in es}
        if len(cat_ws) != 1:
            errors.append(f"{cat}: implied category weight not constant: {cat_ws}")
        implied[cat] = next(iter(cat_ws))
    total = sum(implied.values())
    if abs(total - 1.0) > 1e-9:
        errors.append(f"category weights sum to {total!r}, not 1")

    n_conditional = sum(1 for e in entries if e["conditional"])
    if n_conditional != EXPECTED_CONDITIONAL:
        errors.append(
            f"expected {EXPECTED_CONDITIONAL} conditional checks, found {n_conditional}"
        )

    if errors:
        sys.exit("xlsx validation failed:\n  " + "\n  ".join(errors))

    # CategoryWeights: descending weight, ties alphabetical (matches the
    # adopted vector's presentation order).
    cw_order = sorted(implied, key=lambda c: (-implied[c], c))
    category_weights = {c: round(implied[c], 9) for c in cw_order}

    weights = {"CategoryWeights": [category_weights]}
    for cat in rubric:
        weights[cat] = [
            {"name": e["name"], "weight": round(e["in_cat_w"] * 100, 4)}
            for e in per_cat_w[cat]
        ]

    audit = {
        "n_checks": len(entries),
        "conditional_nos": [e["no"] for e in entries if e["conditional"]],
        "category_counts": {c: len(v) for c, v in rubric.items()},
        "category_weights": category_weights,
    }
    return rubric, weights, audit


# --------------------------------------------------------------------------


def roundtrip() -> int:
    """Regenerate every artifact from the CURRENT pair; require byte-identity."""
    rubric = json.loads(RUBRIC_PATH.read_text())
    weights = json.loads(WEIGHTS_PATH.read_text())
    category_weights = weights["CategoryWeights"][0]

    failures = []
    if dump_json(rubric) != RUBRIC_PATH.read_text():
        failures.append(str(RUBRIC_PATH))
    if dump_json(weights) != WEIGHTS_PATH.read_text():
        failures.append(str(WEIGHTS_PATH))
    for p in LIVE_PROMPT_FILES + list(EXCEL_COPIES.values()):
        current = p.read_text()
        if splice_prompt(current, rubric, category_weights) != current:
            failures.append(str(p))
    if failures:
        print("ROUNDTRIP FAILED — regeneration is not byte-identical for:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"Roundtrip OK — {len(LIVE_PROMPT_FILES + list(EXCEL_COPIES.values()))} "
          f"prompt files + 2 JSON files regenerate byte-identically.")
    return 0


def apply(xlsx_path: Path) -> int:
    rubric, weights, audit = parse_xlsx(xlsx_path)
    category_weights = weights["CategoryWeights"][0]

    RUBRIC_PATH.write_text(dump_json(rubric))
    WEIGHTS_PATH.write_text(dump_json(weights))
    print(f"Wrote {RUBRIC_PATH}")
    print(f"Wrote {WEIGHTS_PATH}")

    for gui_path in LIVE_PROMPT_FILES:
        new_text = splice_prompt(gui_path.read_text(), rubric, category_weights)
        gui_path.write_text(new_text)
        print(f"Wrote {gui_path}")
        excel_path = EXCEL_COPIES[gui_path]
        excel_path.write_text(new_text)
        print(f"Wrote {excel_path} (byte-copy)")

    print("\nAudit:")
    print(f"  checks: {audit['n_checks']}, category counts: {audit['category_counts']}")
    print(f"  conditional checks ({len(audit['conditional_nos'])}): "
          f"{audit['conditional_nos']}")
    print(f"  CategoryWeights: {audit['category_weights']}")
    print("\nNext: run the CLI and coding prompt builders (see module docstring).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--roundtrip", action="store_true",
                    help="verify regeneration from the current rubric pair is "
                         "byte-identical to what is on disk (no writes)")
    args = ap.parse_args()
    if args.roundtrip:
        return roundtrip()
    if not args.xlsx.exists():
        sys.exit(f"xlsx not found: {args.xlsx} (if moved, ask Patrick)")
    return apply(args.xlsx)


if __name__ == "__main__":
    raise SystemExit(main())
