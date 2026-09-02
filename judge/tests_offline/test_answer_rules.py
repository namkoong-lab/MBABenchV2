"""Offline tests for the answer-equivalence rulebook (utils/answer_rules.py).

Run from judge/:  python tests_offline/test_answer_rules.py
Fixtures include the canary cases that motivated judge v6: grading 193's
penny failure (480,051.30 vs .31 under "two decimal places") and its
expenses sign-convention failure, plus the rules the spec enumerates.
"""
import sys
from datetime import date, datetime
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

from utils import answer_rules as R  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)
    else:
        print("OK ", msg)


def ctx(label="", unit=None, dp=None, scale="value", formula=None, fmt=None, directive=False,
        got_formula=None):
    return R.AnswerContext(
        label=label, unit=unit,
        precision=R.Precision(dp, scale, "header" if dp is not None else "none"),
        expected_formula=formula, expected_number_format=fmt,
        percent_directive=directive, got_formula=got_formula,
    )


# --- parsing (rules 4, 6, 7, 8) -------------------------------------------
check(R.parse_scalar("(480,051.30)").value == -480051.30, "parentheses = minus, commas stripped")
check(R.parse_scalar("$1,234.00").value == 1234.0, "currency stripped")
check(R.parse_scalar("-$1,234").value == -1234.0, "sign before currency")
s = R.parse_scalar("42%")
check(s.value == 42.0 and s.pct_literal, "literal percent parsed with flag")
check(R.parse_scalar("-").value == 0.0 and R.parse_scalar("—").value == 0.0, "accounting dash is zero")
check(R.parse_scalar("").kind == "empty" and R.parse_scalar(None).kind == "empty", "blank is empty")
check(R.parse_scalar("2027-01-01").value == date(2027, 1, 1), "ISO date")
check(R.parse_scalar("01/02/2027").value == date(2027, 1, 2), "US date")
check(R.parse_scalar(datetime(2027, 1, 1, 0, 0)).value == date(2027, 1, 1), "datetime -> date")
check(R.parse_scalar("Yes").kind == "bool" and R.parse_scalar("no").value is False, "yes/no -> bool")
check(R.parse_scalar(True).kind == "bool", "bool stays bool")
check(R.parse_scalar("N/A").kind == "text", "sentinel stays text at parse time")
check(R.parse_scalar("abc").kind == "text", "plain text")

# --- precision directives (rule 1) ----------------------------------------
p = R.parse_precision_directive("Questions (please round your answers to two decimal places)")
check(p.dp == 2 and p.scale == "value" and p.source == "header", "two decimal places")
check(R.parse_precision_directive("Questions (please round your final answers to four decimal places)").dp == 4, "four dp")
check(R.parse_precision_directive("Questions (please round your answers to whole numbers)").dp == 0, "whole numbers")
p68 = R.parse_precision_directive(
    "Questions (Answer Yes/No questions with 'Yes' or 'No'. Answer return questions with the "
    "return stored as a decimal proportion (e.g., store 0.10 for a 10% return, or 0.0005 for a "
    "0.05% return). Format return cells as 0.00%.)"
)
check(p68.dp == 2 and p68.scale == "percent", "task-68 header: 2 dp on the percent rendering")
check(R.parse_precision_directive("Questions").dp is None, "no directive")
check(R.round_dp_from_formula("=ROUND(Doctor!CG25-Trade!CG25,2)") == 2, "ROUND(...,2) in golden formula")
check(R.round_dp_from_formula("=SUM(A1:A3)") is None, "no ROUND")


class _AF:  # openpyxl ArrayFormula stand-in: the formula text lives in .text
    def __init__(self, text):
        self.text = text


check(R.round_dp_from_formula(_AF("=ROUND(INDEX('Solution Model'!$F$31:$Y$50,_xlfn.XMATCH(1,A:A)),2)")) == 2,
      "v6.4: ROUND read from an ArrayFormula object (every golden answer formula is one)")
check(R.round_dp_from_formula(_AF('=_xlfn.LET(_xlpm.I,INDEX(A1:B2,1),_xlfn.IFNA(ROUND(_xlpm.I,4),"n/a"))')) == 4,
      "v6.4: ROUND found inside LET()/IFNA() wrappers by walking to its own parenthesis")
check(R.round_dp_from_formula("=ROUNDUP(A1,2)") is None and R.round_dp_from_formula("=MROUND(A1,5)") is None,
      "ROUNDUP / MROUND are not ROUND")
check(R.round_dp_from_formula("=ROUND(A1,2)+ROUND(B1,4)") == 2, "leftmost ROUND wins")

# --- tolerance (rule 1): the grading-193 penny --------------------------------
r = R.compare(480051.31, 480051.30, ctx("Total value", dp=2))
check(r["verdict"] == "match" and r["rule"] == "tolerance", "penny on a six-figure answer is THE SAME (grading 193)")
r = R.compare(480051.31, 480050.31, ctx("Total value", dp=2))
check(r["verdict"] == "mismatch", "1.00 off on a six-figure answer is a mismatch")
r = R.compare(480051.3049, 480051.30, ctx("x", formula="=ROUND(A1,2)"))
check(r["verdict"] == "match" and r["tolerance_source"].startswith("round_dp2"), "per-answer ROUND sets the tolerance")
r = R.compare(100.9, 101.0, ctx("x", dp=0))
check(r["verdict"] == "match", "whole numbers: 101 is the correct rounding of 100.9")
r = R.compare(100.9, 100.0, ctx("x", dp=0))
check(r["verdict"] == "mismatch", "whole numbers: 100 is the WRONG rounding of 100.9")
r = R.compare(1234.5678, 1234.5678001, ctx("x"))
check(r["verdict"] == "match" and r["tolerance_source"] == "global_fallback", "global fallback 1e-6 relative")
r = R.compare(1234.5678, 1234.57, ctx("x"))
check(r["verdict"] == "mismatch", "global fallback rejects a 0.002 difference with no directive")

# --- scale awareness (Patrick 2026-09-02): small answers are not forgiven -----
r = R.compare(0.5, 0.51, ctx("ratio", dp=2))
check(r["verdict"] == "mismatch", "0.51 vs 0.50 at two decimals is a 2% error -> mismatch")
r = R.compare(0.5, 0.505, ctx("ratio", dp=2))
check(r["verdict"] == "match" and r["attempt_rounded"] is False,
      "v6.4: unrounded 0.505 rounds to 0.50 -> same number; recorded unrounded for the Rounding check")
r = R.compare(0.5, 0.5051, ctx("ratio", dp=2))
check(r["verdict"] == "mismatch", "0.5051 rounds to 0.51 -> different")
r = R.compare(100.64, 100.63598814847582, ctx("implied share value", dp=2))
check(r["verdict"] == "match" and r["attempt_rounded"] is False and r["requested_decimals"] == 2,
      "SAP case: the golden's number before rounding is THE SAME number (sweep: 22 attempts failed on this)")
r = R.compare(100.64, 100.646, ctx("implied share value", dp=2))
check(r["verdict"] == "mismatch", "100.646 rounds to 100.65 -> different")
check(R.is_rounded_to(-1890487.51, 2) and not R.is_rounded_to(-1890487.505, 2)
      and not R.is_rounded_to(12779502.005323779, 2) and R.is_rounded_to(0.1 + 0.2, 2),
      "is_rounded_to: large values keep their third decimal visible; float noise is forgiven")
r = R.compare(5000.004, 5000.0, ctx("cash", dp=2))
check(r["verdict"] == "match", "5,000.00 vs 5,000.004 is the same (correct rounding)")
r = R.compare(5000.004, 5000.004, ctx("cash", dp=2))
check(r["verdict"] == "match" and r["rule"] == "exact", "identical unrounded values are exact")
r = R.compare(5000.004, 5000.0041, ctx("cash", dp=2))
check(r["verdict"] == "match", "unrounded but inside one-part-per-million noise")
r = R.compare(0.5, 0.51, ctx("ratio", dp=2, formula="=ROUND(A1,2)"))
check(r["verdict"] == "match" and "coarse_rounding" in r["flags"],
      "both sides rounded: a full unit is allowed but flagged coarse on a small answer")
r = R.compare(0.0537, 0.05, ctx("share", dp=2))
check(r["verdict"] == "match" and "coarse_rounding" in r["flags"], "coarse rounding on a small answer is flagged")
r = R.compare(1234.5678, 1234.57, ctx("x", dp=2))
check(r["verdict"] == "match", "correctly rounded 1234.57 matches unrounded golden")
r = R.compare(1234.5678, 1234.5, ctx("x", dp=2, got_formula="=ROUND(B2,1)"))
check(r["verdict"] == "mismatch", "rounded to fewer places than asked, off by 0.07 -> mismatch")

# --- percent scale of the precision --------------------------------------
r = R.compare(0.4213, 0.42, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "mismatch", "2 dp on a %-formatted fraction means 0.00005, so 0.42 vs 0.4213 fails")
r = R.compare(0.42127, 0.4213, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "match", "0.4213 is the correct 42.13% rounding of 0.42127")
r = R.compare(0.4213, 0.42134, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "match" and r["attempt_rounded"] is False,
      "v6.4: unrounded 0.42134 rounds to 42.13% -> same; recorded unrounded")
r = R.compare(0.4213, 0.42136, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "mismatch", "0.42136 rounds to 42.14% -> different")
af4 = _AF("=ROUND((B5-C5)/C5,4)")
r = R.compare(-0.0872, -0.0871985157699443, ctx("relative difference between actuals and budget",
                                                 unit="[%]", dp=4, fmt="0.0000%", formula=af4))
check(r["verdict"] == "match" and r["requested_decimals"] == 4 and r["decimals_source"] == "round",
      "TechVentures case: golden ROUND(x,4) sets 4 stored decimals (not 6 for the % format) -> same")
r = R.compare(-0.0872, -8.7199, ctx("relative difference between actuals and budget",
                                     unit="[%]", dp=4, fmt="0.0000%", formula=af4))
check(r["verdict"] == "match" and r["rule"] == "percent_form" and r["attempt_rounded"] is True,
      "percent-scale answer -8.7199 for -0.0872: same, and rounded in its own unit")
r = R.compare(0.23, 0.24, ctx("Return on equity", dp=2, formula=_AF("=ROUND(INDEX(A1:A9,1),2)")))
check(r["verdict"] == "match" and "coarse_rounding" in r["flags"],
      "both sides rounded (golden ROUND via array formula): a full unit is allowed, flagged coarse")
r = R.compare(0.23, 0.2359, ctx("Return on equity", dp=2, formula=_AF("=ROUND(INDEX(A1:A9,1),2)")))
check(r["verdict"] == "mismatch", "unrounded 0.2359 rounds to 0.24, not 0.23 -> different")
r = R.compare(0.000487, 0.0005, ctx("return", dp=2, scale="percent"))
check(r["verdict"] == "match", "task-68 style: 0.05% is the correct rounding of 0.0487%")

# --- sign convention (rule 2): outflow rows only --------------------------
r = R.compare(-500.0, 500.0, ctx("Total operating expenses in 2028"))
check(r["verdict"] == "match" and r["rule"] == "sign_outflow", "expense row: sign flip accepted (grading 193 expenses case)")
r = R.compare(-1890487.51, 1890487.51, ctx("What will be the net worth difference (Doctor vs. Trade)?"))
check(r["verdict"] == "mismatch" and "sign_flip_not_outflow" in r["flags"], "difference row: sign flip is WRONG, flagged")
r = R.compare(-12.0, 12.0, ctx("Depreciation expense 2029"))
check(r["verdict"] == "match", "depreciation is an outflow word")
r = R.compare(-12.0, 12.0, ctx("Net income 2029"))
check(r["verdict"] == "mismatch", "net income is not an outflow word")
check(R.is_outflow_label("Capex in year 3") and not R.is_outflow_label("Costa Rica revenue"), "word-boundary lexicon (Costa != cost)")
for lab in ("What is the amount for/of Energy in 11/2029?", "What is the amount for/of Raw material in 5/2029?",
            "What is the amount for/of SG&A in 3/2030?", "What is the amount for/of Interest (Loan) in 2031?",
            "What is the amount for/of Production wages in 5/2029?", "What is the amount for/of R&D in 2030?",
            "What is the amount for/of Repayment Loans in 2032?", "Income tax for the year 2029"):
    check(R.is_outflow_label(lab), f"v6.4 extended outflow lexicon: {lab[:45]!r}")
for lab in ("Interest income 2029", "What is the amount for/of Changes in Net Working Capital in 2030?",
            "What is the amount for/of Intangible Assets growth in 2030?", "Net income 2029",
            "Equity value at valuation date", "What is the gross margin in 2030?"):
    check(not R.is_outflow_label(lab), f"inflow guard / non-outflow: {lab[:45]!r}")
r = R.compare(3361886.42, -3361886.42, ctx("What is the amount for/of Energy in 11/2029?", dp=2))
check(r["verdict"] == "match" and r["rule"] == "sign_outflow", "Fixings energy row: flipped sign accepted (v6.4)")
r = R.compare(-8172.42, 8172.42, ctx("What is the amount for/of Changes in Net Working Capital in 2030?", dp=2))
check(r["verdict"] == "mismatch" and "sign_flip_not_outflow" in r["flags"],
      "NWC change keeps its sign (Patrick: a flipped change is WRONG)")

# --- percent form (rule 3) ------------------------------------------------
r = R.compare(0.4213, 42.13, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "match" and r["rule"] == "percent_form", "42.13 vs 0.4213 with unit % is the same")
r = R.compare(42.13, 0.4213, ctx("What is the growth rate?"))
check(r["verdict"] == "match" and r["rule"] == "percent_form", "question text cue: rate")
r = R.compare(0.4213, "42.13%", ctx("x"))
check(r["verdict"] == "match", "literal % on the attempt triggers percent form")
r = R.compare(0.4213, 42.13, ctx("Total cash"))
check(r["verdict"] == "mismatch", "no percent cue: x100 is a mismatch")
r = R.compare(-0.05, 5.0, ctx("Interest expense rate", unit="%"))
check(r["verdict"] == "match" and r["rule"] == "sign_outflow+percent_form", "percent + outflow sign combine")
r = R.compare(0.4213, 42.1, ctx("gross margin", unit="[%]", dp=2, fmt="0.00%"))
check(r["verdict"] == "mismatch", "42.1 vs 42.13% is off by 0.03 points -> mismatch even in percent form")

# --- unit scale (rule 9) is flagged, never accepted ------------------------
r = R.compare(1234.0, 1234000.0, ctx("Revenue"))
check(r["verdict"] == "mismatch" and "possible_unit_scale_difference" in r["flags"], "x1000 flagged, still a mismatch")
r = R.compare(1234000.0, 1234.0, ctx("Revenue"))
check("possible_unit_scale_difference" in r["flags"], "/1000 flagged too")

# --- text, sentinels, dates, bools ----------------------------------------
r = R.compare("Outside model horizon", "n/a", ctx("x"))
check(r["verdict"] == "match" and r["rule"] == "sentinel_synonym", "sentinel synonyms match (text vs text)")
r = R.compare(5.0, "N/A", ctx("x"))
check(r["verdict"] == "mismatch" and "sentinel_for_numeric" in r["flags"], "sentinel where a number is expected is wrong")
r = R.compare("Yes", "yes ", ctx("x"))
check(r["verdict"] == "match", "yes/no case-insensitive")
r = R.compare("Yes", "No", ctx("x"))
check(r["verdict"] == "mismatch", "yes vs no")
r = R.compare(date(2027, 1, 1), "2027-01-01", ctx("x"))
check(r["verdict"] == "match" and r["rule"] == "date_form", "date vs ISO string")
r = R.compare(date(2027, 1, 1), 46388, ctx("x"))
check(r["verdict"] == "match", "date vs Excel serial 46388")
r = R.compare("Some text", "Some  text ", ctx("x"))
check(r["verdict"] == "match", "whitespace-normalised text")
r = R.compare(0.0, "-", ctx("x"))
check(r["verdict"] == "match", "zero vs accounting dash")

# --- missing --------------------------------------------------------------
r = R.compare(5.0, None, ctx("x"))
check(r["verdict"] == "missing", "blank attempt = unanswered")
r = R.compare(5.0, "", ctx("x"))
check(r["verdict"] == "missing", "empty-string attempt = unanswered")
r = R.compare(None, 5.0, ctx("x"))
check(r["verdict"] == "missing" and "missing_expected" in r["flags"], "blank golden flagged")

# --- rendered text: one source, two consumers -----------------------------
txt = R.render_rules_text()
check(R.RULES_VERSION in txt, "rulebook text carries its version")
for word in ("scale-aware", "half a unit", "one part in a million", "outflow", "Percent form",
             "parentheses", "Sentinel", "Unit scale", "Hardcoded"):
    check(word in txt, f"rulebook text mentions {word!r}")
check("capex" in txt and "depreciation" in txt, "rulebook text lists the outflow lexicon")
check("ROUNDS TO" in txt and "Rounded outputs" in txt and "energy" in txt,
      "v6.4 text: rounds-to rule, rounding-compliance pointer, extended lexicon")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("ALL ANSWER-RULES CHECKS PASSED")
