#!/usr/bin/env python3
"""Generate system prompt v16 (prompt_version 1609 = system v16 + template v9)
from v15: the same tool manual with the passages that contradicted the House
Standards removed, so the attached standards govern.

Why (Pat, 2026-09-19): the manual's style advice predates
house_standards/House_Standards_v1.md and told the agent the opposite of it in
five places. The agent is told to "conform to it throughout" and then handed
specific instructions that break it; specific instructions win. The GUI and
coding agents get no such manual, so they never saw the conflict.

Minimal edits, nothing else touched (task template stays v9):
  1. HOUSE STANDARDS block: one added sentence - where this manual's modelling
     or formatting guidance conflicts with the standards, the standards govern;
     the tool limits described in the manual still apply.
  2. NUMBER SIGN CONSISTENCY: "Convention A (Recommended)" = outflows POSITIVE
     contradicted "Costs and outflows negative ... everywhere". The A/B menu is
     replaced by a pointer to the standards; the consistency rules stay.
  3. Color Standards heading: "(font color, NOT cell fill)" contradicted
     "Inputs also carry a pale blue fill".
  4. Number Notation: "0.00%" contradicted "percentages to one"; the integer
     and currency codes showed zero as 0 / no decimals against "Zeros as
     dashes. $mm to two decimals". The three codes are removed; the dates line
     keeps the number_format syntax example.
  5. Consistency of Styles: Calibri, headers 12, data 11 contradicted
     "Arial 10 throughout".
The standards' own wording is NOT restated in the manual: the agent reads the
values from HOUSE_STANDARDS.md exactly as the GUI and coding agents do.

One tool-mechanics line is added with the same cut (not a standards matter):
  6. set_cell_formula now stores post-2007 functions with the file format's
     "_xlfn." prefix (they were "#NAME?" in Excel and LibreOffice without it);
     the manual says so, so that a formula read back with the prefix is not
     mistaken for a fault.

Deterministic; run from cli-agents-master/ and diff. tests/test_v16_prompts.py
pins the result.

    uv run python tools/build_v16_prompts.py [--check]
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "excel_cli_agent" / "prompts"
SYS_IN, SYS_OUT = PROMPTS / "system_prompt_v15.txt", PROMPTS / "system_prompt_v16.txt"

EDITS = (
    # 1. precedence sentence
    (
        "Read it before you build and conform to it throughout. Where the case's own "
        "instructions conflict with it, the case instructions govern.\n",
        "Read it before you build and conform to it throughout. Where the case's own "
        "instructions conflict with it, the case instructions govern. Where the modelling "
        "or formatting guidance in these system instructions conflicts with it, the house "
        "standards govern; the tool limits described here still apply.\n",
    ),
    # 2. signs
    (
        "NUMBER SIGN CONSISTENCY:\n"
        "\n"
        "Convention A (Recommended - Investment Banking Standard):\n"
        "- Outflows (CapEx, expenses, payments) are POSITIVE numbers in their rows\n"
        "- Formulas SUBTRACT these positive outflows: =Revenue - CapEx - OpEx\n"
        "- Cash inflows are POSITIVE, cash outflows are POSITIVE but subtracted in formulas\n"
        "\n"
        "Convention B (Alternative):\n"
        "- Outflows are NEGATIVE numbers in their rows\n"
        "- Formulas ADD all items: =Revenue + CapEx + OpEx (CapEx is already negative)\n"
        "\n"
        "Rules:\n"
        "1. Pick Convention A or B at the START of your model - document it in your input/assumptions sheet\n"
        "2. CapEx sign MUST be consistent across ALL sheets (assumptions, workings, answers)\n"
        "3. If CapEx = 500 in your assumptions sheet, it must stay 500 (positive) everywhere if using Convention A\n"
        "4. NEVER mix: positive CapEx on one sheet, negative on another\n",
        "NUMBER SIGN CONSISTENCY:\n"
        "\n"
        "Follow the sign convention set in the HOUSE STANDARDS, and apply it identically on every sheet:\n"
        "1. A line item's sign MUST be consistent across ALL sheets (assumptions, workings, answers)\n"
        "2. NEVER mix: positive CapEx on one sheet, negative on another\n",
    ),
    # 3. input fill
    (
        "Color Standards (font color, NOT cell fill) -- convention: blue=inputs, black=formulas, "
        "green=cross-sheet links, red=external links:\n",
        "Color Standards (font colors below; cell fills as the HOUSE STANDARDS specify) -- convention: "
        "blue=inputs, black=formulas, green=cross-sheet links, red=external links:\n",
    ),
    # 4. number formats
    (
        "Number Notation:\n"
        "- Integers: number_format: \"#,##0\"\n"
        "- Percentages: number_format: \"0.00%\"\n"
        "- Currency: number_format: \"$#,##0_);($#,##0)\"\n",
        "Number Notation (number_format takes an Excel format code; decimals and the display of zeros "
        "follow the HOUSE STANDARDS):\n",
    ),
    # 6. tool mechanics: the stored form of post-2007 functions
    (
        "- Returns calculated value immediately\n",
        "- Returns calculated value immediately\n"
        "- Stores functions added to Excel after 2007 with the file format's _xlfn. prefix (names declared "
        "in LET with _xlpm.): write formulas plainly - the tool adds the prefixes - and expect to see them "
        "when you read a formula back; Excel displays them without\n",
    ),
    # 5. font
    (
        "- Use sans-serif font family (such as Calibri) ALL sheets: font: {\"name\": \"Calibri\"}\n"
        "- Headers: font: {\"bold\": true, \"size\": 12}\n"
        "- Data cells: font: {\"size\": 11}\n",
        "- Use the font family and size the HOUSE STANDARDS specify on ALL sheets: "
        "font: {\"name\": \"<font name>\", \"size\": <size>}\n"
        "- Headers: font: {\"bold\": true}\n",
    ),
)

# Nothing that contradicted the standards may survive.
GONE = ("Convention A", "Convention B", "NOT cell fill", "0.00%", "$#,##0_);($#,##0)", "Calibri",
        "\"size\": 12", "\"size\": 11")


def swap(text: str, old: str, new: str) -> str:
    """Exact-match replace that refuses to silently no-op or double-fire."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"expected exactly 1 occurrence, found {n}: {old[:70]!r}")
    return text.replace(old, new)


def build_system_prompt(v15: str) -> str:
    t = v15
    for old, new in EDITS:
        t = swap(t, old, new)
    for needle in GONE:
        if needle in t:
            raise SystemExit(f"contradicting passage survived: {needle!r}")
    return t


def main() -> int:
    check = "--check" in sys.argv
    text = build_system_prompt(SYS_IN.read_text())
    if check:
        if not SYS_OUT.exists() or SYS_OUT.read_text() != text:
            print(f"DRIFT: {SYS_OUT.name} differs from the generated text"); return 1
    else:
        SYS_OUT.write_text(text)
    print(f"{SYS_OUT.name}: {len(text):,} chars md5 {hashlib.md5(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
