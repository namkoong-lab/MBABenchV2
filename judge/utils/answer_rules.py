"""The answer-equivalence rulebook — ONE source, TWO consumers (judge v6).

`compare()` is what the harness answer checker (utils/answer_check.py) runs
on every Questions-sheet answer, and `render_rules_text()` is rendered
VERBATIM into the single-pass judge prompt (template_8), so the deterministic
engine and the LLM judge apply the same definition of "the same number" and
cannot drift apart.

The rules remove *noise*, not *standards*: a genuinely different value is a
mismatch no matter how it is dressed. Mined 2026-09-02 from 394 cited
"Final calculation accuracy" mistakes across the deprecated grading corpus
(tolerance / sign / percent-form / display / missing classes all attested).

Rules (numbering matches render_rules_text and the v6 spec §3):
  1. Tolerance follows the requested precision ("round to two decimal
     places" => equal within one cent — one unit of the last requested
     decimal, since two correctly-rounded floats can differ by a full unit),
     else a per-answer ROUND() in the golden formula, else
     |a-b| <= max(1e-9, 1e-6*max(|a|,|b|)).
  2. Sign convention, GUARDED by an outflow lexicon on the question text
     (expense/cost/spend/outflow/depreciation/amortization/capex/tax):
     |a| == |b| within tolerance is accepted ONLY for such rows. One-off
     flips on every other row stay failures — for a "difference" question
     the sign IS the answer. (Patrick 2026-09-02: lexicon only; the earlier
     "consistent across the block" clause was dropped as too loose.)
  3. Percent x100 / fraction form: when the Unit column says %, the question
     text says percent/rate/margin/..., the golden cell is %-formatted, or
     the attempt wrote a literal "42%": a == 100b or b == 100a is equal
     (within tolerance, applied on the golden's scale).
  4. Values, never rendered strings: parentheses = minus, $/commas/%
     stripped, "(480,051.30)" parses to -480051.30.
  5. Sentinel text synonyms ("Outside model horizon" ~ "N/A" ~ "beyond
     horizon") compare equal to each other — text-vs-text only.
  6. Numeric-as-text ("42", "$1,234.00") parses before comparing.
  7. Dates: ISO / US / Excel serial are equal when they name the same day.
  8. Zero forms: 0 / 0.0 / accounting "-" equal zero. A BLANK cell is
     "unanswered" — completeness, not equivalence.
  9. Units scale (x1000 / x1e6) is NEVER auto-accepted (Patrick): flagged
     as `possible_unit_scale_difference` for human review; verdict stays
     mismatch.

Hardcoded answers are detected by the checker (formula-vs-constant on the
answer cell), not here; the rulebook text describes the standard so the
judge applies it identically.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Any, Optional

ABS_TOL = 1e-9
REL_TOL = 1e-6

# Rule 2 — question-text lexicon under which a sign flip is a convention,
# not an error. Word-boundary matched, case-insensitive. Deliberately short
# (Patrick 2026-09-02): only rows whose quantity is unambiguously an outflow.
OUTFLOW_LEXICON = (
    "expense", "expenses", "cost", "costs", "spend", "spending", "spent",
    "outflow", "outflows", "depreciation", "amortization", "amortisation",
    "capex", "capital expenditure", "capital expenditures", "tax", "taxes",
)

# Rule 3 — question-text cues that the quantity is a percentage / ratio.
PERCENT_LEXICON = (
    "percent", "percentage", "%", "ratio", "rate", "margin", "irr", "yield",
    "growth", "return", "proportion", "cagr", "roi", "roe", "roa", "wacc",
    "share of",
)

# Rule 5 — attempt-side sentinel synonyms, all canonicalised to "n/a".
SENTINEL_SYNONYMS = {
    "n/a", "na", "n.a.", "n.a", "not applicable", "not available",
    "outside model horizon", "outside the model horizon", "beyond horizon",
    "beyond model horizon", "beyond the model horizon", "outside horizon",
    "out of horizon", "out of range", "not in model horizon",
    "outside of model horizon",
}

# Rule 8 — accounting zero renderings.
ZERO_FORMS = {"-", "--", "—", "–", "−"}

# Rule 9 — scale factors that are flagged, never accepted.
UNIT_SCALE_FACTORS = (1_000.0, 1_000_000.0, 1_000_000_000.0)

_WORD_NUMBERS = {
    "zero": 0, "no": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}

_NUMERIC_TEXT_RE = re.compile(
    r"""^\s*
        (?P<open>\()?\s*
        (?P<sign>[-−–+])?\s*
        (?P<cur>[$€£¥])?\s*
        (?P<sign2>[-−–+])?\s*
        (?P<num>(\d{1,3}(,\d{3})+|\d+)(\.\d+)?|\.\d+)
        \s*(?P<pct>%)?\s*
        (?P<close>\))?\s*
        (?P<suffix>x|X|bps|pp)?\s*$""",
    re.VERBOSE,
)
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]00:00(?::00)?)?\s*$")
_US_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
_ROUND_RE = re.compile(r"ROUND\s*\(", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------


def norm_text(s: Any) -> str:
    """Casefold, collapse whitespace, unify quotes/dashes, strip edge punctuation."""
    if s is None:
        return ""
    t = str(s)
    t = (
        t.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-").replace("−", "-")
        .replace(" ", " ")
    )
    t = re.sub(r"\s+", " ", t).strip().casefold()
    return t.strip(" .:;")


def canonical_sentinel(s: str) -> Optional[str]:
    """'n/a' when *s* is a recognised sentinel synonym, else None."""
    t = norm_text(s).rstrip(".")
    return "n/a" if t in SENTINEL_SYNONYMS else None


# ---------------------------------------------------------------------------
# Scalar parsing (rules 4, 6, 7, 8)
# ---------------------------------------------------------------------------


@dataclass
class Scalar:
    kind: str                 # "empty" | "number" | "text" | "date" | "bool"
    value: Any = None
    pct_literal: bool = False  # the source text carried an explicit "%"
    raw: Any = None


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_scalar(v: Any) -> Scalar:
    """Classify one cell value for comparison (rules 4/6/7/8)."""
    if v is None:
        return Scalar("empty", raw=v)
    if isinstance(v, bool):
        return Scalar("bool", v, raw=v)
    if _is_number(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return Scalar("text", str(v), raw=v)
        return Scalar("number", float(v), raw=v)
    if isinstance(v, datetime):
        return Scalar("date", v.date(), raw=v)
    if isinstance(v, date):
        return Scalar("date", v, raw=v)
    if isinstance(v, dt_time):
        return Scalar("text", v.isoformat(), raw=v)
    s = str(v)
    if s.strip() == "":
        return Scalar("empty", raw=v)
    st = s.strip()
    if st in ZERO_FORMS:
        return Scalar("number", 0.0, raw=v)
    m = _NUMERIC_TEXT_RE.match(st)
    if m:
        num = float(m.group("num").replace(",", ""))
        negative = bool(m.group("open") and m.group("close"))
        sign = m.group("sign") or m.group("sign2")
        if sign and sign != "+":
            negative = True  # "-" (inside or outside parentheses) is negative
        if negative:
            num = -num
        return Scalar("number", num, pct_literal=bool(m.group("pct")), raw=v)
    m = _ISO_DATE_RE.match(st)
    if m:
        try:
            return Scalar("date", date(int(m.group(1)), int(m.group(2)), int(m.group(3))), raw=v)
        except ValueError:
            pass
    m = _US_DATE_RE.match(st)
    if m:
        try:
            return Scalar("date", date(int(m.group(3)), int(m.group(1)), int(m.group(2))), raw=v)
        except ValueError:
            pass
    low = st.casefold()
    if low in ("yes", "true"):
        return Scalar("bool", True, raw=v)
    if low in ("no", "false"):
        return Scalar("bool", False, raw=v)
    return Scalar("text", st, raw=v)


def excel_serial_to_date(n: float) -> Optional[date]:
    """Excel 1900-system serial -> date (whole-day serials only)."""
    try:
        from openpyxl.utils.datetime import from_excel

        d = from_excel(n)
        if isinstance(d, datetime):
            return d.date()
        if isinstance(d, date):
            return d
    except Exception:  # noqa: BLE001 — best-effort conversion
        return None
    return None


# ---------------------------------------------------------------------------
# Precision directives (rule 1)
# ---------------------------------------------------------------------------


@dataclass
class Precision:
    dp: Optional[int]         # decimal places requested; None = no directive
    scale: str = "value"      # "value" | "percent" (dp applies to the % rendering)
    source: str = "none"      # "header" | "round" | "format" | "none"


def parse_precision_directive(header_text: Any) -> Precision:
    """Read the Questions-sheet header phrase into a Precision.

    Understands "round your answers to two decimal places", "four decimal
    places", "whole numbers", and a task-68 style "Format return cells as
    0.00%" (2 dp on the percent rendering).
    """
    t = norm_text(header_text)
    if not t:
        return Precision(None)
    m = re.search(r"(\d+|zero|one|two|three|four|five|six|seven|eight)\s+decimal\s+place", t)
    if m:
        tok = m.group(1)
        dp = int(tok) if tok.isdigit() else _WORD_NUMBERS[tok]
        return Precision(dp, "value", "header")
    if re.search(r"whole\s+number|nearest\s+(whole|integer|unit)", t):
        return Precision(0, "value", "header")
    m = re.search(r"0\.(0+)%", t)
    if m:
        return Precision(len(m.group(1)), "percent", "header")
    if re.search(r"\b0%", t):
        return Precision(0, "percent", "header")
    return Precision(None)


def round_dp_from_formula(formula: Any) -> Optional[int]:
    """The outermost ROUND(...,n) digit count in a golden formula, if any."""
    if not isinstance(formula, str) or not _ROUND_RE.search(formula):
        return None
    # Find the LAST ",<int>)" that closes a ROUND — outermost ROUND ends the
    # formula in the common '=ROUND(expr, 2)' shape.
    m = re.search(r"ROUND\s*\((.*),\s*(-?\d+)\s*\)\s*$", formula.strip(), re.IGNORECASE)
    if m:
        return int(m.group(2))
    return None


def percent_format(number_format: Any) -> bool:
    return isinstance(number_format, str) and "%" in number_format


def tolerance_for(expected: float, got: float, precision: Precision,
                  expected_pct_format: bool) -> tuple[float, str]:
    """Absolute tolerance for one comparison plus a label of what set it."""
    if precision.dp is None:
        # No stated precision: the locked Phase-B rule (abs 1e-9 / rel 1e-6).
        return max(ABS_TOL, REL_TOL * max(abs(expected), abs(got))), "global_fallback"
    # ONE unit of the last requested decimal place: two values that are
    # "the same to two decimal places" can still differ by a full cent once
    # each side rounds its own float path (x.xx49 vs x.xx51). Grading 193
    # failed 480,051.30 vs .31 exactly this way. Two units is a real
    # difference.
    unit = 10.0 ** -precision.dp
    if precision.scale == "percent" or (
        precision.scale == "value" and expected_pct_format
    ):
        # The dp applies to the rendered percentage (42.13%); the stored
        # value is the fraction (0.4213), so shrink the unit by 100.
        unit /= 100.0
    # A stated precision is THE tolerance (the relative 1e-6 rule would be
    # looser than a cent on any six-figure answer). A hair of float slack
    # keeps 0.01 vs 0.0100000001 from flipping on representation.
    return max(ABS_TOL, unit * (1 + 1e-6)), f"{precision.source}_dp{precision.dp}"


def numbers_equal(a: float, b: float, tol_abs: float) -> bool:
    return abs(a - b) <= tol_abs


# ---------------------------------------------------------------------------
# Context + comparison (rules 1-9)
# ---------------------------------------------------------------------------


@dataclass
class AnswerContext:
    label: str = ""                       # the question text
    unit: Any = None                      # Unit-column text, if any
    precision: Precision = field(default_factory=lambda: Precision(None))
    expected_formula: Any = None          # golden formula text (for ROUND)
    expected_number_format: Any = None    # golden cell number format
    percent_directive: bool = False       # sheet header declares %-as-decimal


def is_outflow_label(label: Any) -> bool:
    t = norm_text(label)
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t) for w in OUTFLOW_LEXICON)


def is_percent_context(ctx: AnswerContext, got: Scalar) -> tuple[bool, str]:
    if got.pct_literal:
        return True, "literal_percent_sign"
    u = norm_text(ctx.unit)
    if u and ("%" in u or "percent" in u or u in ("pct", "pp", "bps")):
        return True, "unit_column"
    if percent_format(ctx.expected_number_format):
        return True, "golden_percent_format"
    if ctx.percent_directive:
        return True, "header_directive"
    t = norm_text(ctx.label)
    if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t) if w != "%" else "%" in t
           for w in PERCENT_LEXICON):
        return True, "question_text"
    return False, ""


def effective_precision(ctx: AnswerContext) -> Precision:
    """Header directive first, then a per-answer ROUND() in the golden formula."""
    if ctx.precision.dp is not None:
        return ctx.precision
    n = round_dp_from_formula(ctx.expected_formula)
    if n is not None:
        return Precision(n, "value", "round")
    return Precision(None)


def compare(expected: Any, got: Any, ctx: Optional[AnswerContext] = None) -> dict:
    """Compare one golden answer against one attempt answer.

    Returns {verdict: match|mismatch|missing, rule: str|None, flags: [...],
             detail: str, expected_parsed, got_parsed, tolerance, abs_delta}.
    Never raises.
    """
    ctx = ctx or AnswerContext()
    e = parse_scalar(expected)
    g = parse_scalar(got)
    out = {
        "verdict": None,
        "rule": None,
        "flags": [],
        "detail": "",
        "expected_parsed": e.value if e.kind != "date" else e.value.isoformat(),
        "got_parsed": g.value if g.kind != "date" else g.value.isoformat(),
        "expected_kind": e.kind,
        "got_kind": g.kind,
        "tolerance": None,
        "abs_delta": None,
    }
    if g.kind == "empty":
        out["verdict"] = "missing"
        out["detail"] = "answer cell is blank (unanswered)"
        if e.kind == "empty":
            out["flags"].append("expected_also_blank")
        return out
    if e.kind == "empty":
        out["verdict"] = "missing"
        out["flags"].append("missing_expected")
        out["detail"] = "golden answer cell is blank"
        return out

    # --- numbers ---------------------------------------------------------
    if e.kind == "number" and g.kind == "number":
        return _compare_numbers(e, g, ctx, out)

    # --- dates (rule 7) ---------------------------------------------------
    if e.kind == "date" or g.kind == "date":
        ed = e.value if e.kind == "date" else (
            excel_serial_to_date(e.value) if e.kind == "number" else None)
        gd = g.value if g.kind == "date" else (
            excel_serial_to_date(g.value) if g.kind == "number" else None)
        if ed and gd and ed == gd:
            out.update(verdict="match", rule="date_form", detail="same calendar day")
        else:
            out.update(verdict="mismatch", rule=None,
                       detail=f"dates differ ({ed} vs {gd})" if ed and gd
                       else "date vs non-date")
        return out

    # --- booleans (Yes/No) -----------------------------------------------
    if e.kind == "bool" or g.kind == "bool":
        eb = e.value if e.kind == "bool" else _text_to_bool(e.value)
        gb = g.value if g.kind == "bool" else _text_to_bool(g.value)
        if eb is not None and gb is not None:
            if eb == gb:
                out.update(verdict="match", rule="exact", detail="same yes/no")
            else:
                out.update(verdict="mismatch", detail="opposite yes/no")
            return out
        out.update(verdict="mismatch", flags=["type_mismatch"],
                   detail=f"{e.kind} vs {g.kind}")
        return out

    # --- text vs text (rule 5) -------------------------------------------
    if e.kind == "text" and g.kind == "text":
        es, gs = canonical_sentinel(e.value), canonical_sentinel(g.value)
        if es and gs:
            out.update(verdict="match", rule="sentinel_synonym", detail="both n/a-class")
            return out
        if norm_text(e.value) == norm_text(g.value):
            out.update(verdict="match", rule="exact", detail="same text")
        else:
            out.update(verdict="mismatch", detail="text differs")
        return out

    # --- mixed kinds ------------------------------------------------------
    if e.kind == "number" and g.kind == "text":
        if canonical_sentinel(g.value):
            out.update(verdict="mismatch", flags=["sentinel_for_numeric"],
                       detail="attempt answered n/a-class text where a number was expected")
        else:
            out.update(verdict="mismatch", flags=["type_mismatch"],
                       detail="text where a number was expected")
        return out
    out.update(verdict="mismatch", flags=["type_mismatch"],
               detail=f"{e.kind} vs {g.kind}")
    return out


def _text_to_bool(v) -> Optional[bool]:
    t = norm_text(v)
    if t in ("yes", "y", "true"):
        return True
    if t in ("no", "n", "false"):
        return False
    return None


def _compare_numbers(e: Scalar, g: Scalar, ctx: AnswerContext, out: dict) -> dict:
    a, b = float(e.value), float(g.value)
    precision = effective_precision(ctx)
    e_pct_fmt = percent_format(ctx.expected_number_format)
    tol, tol_src = tolerance_for(a, b, precision, e_pct_fmt)
    out["tolerance"] = tol
    out["tolerance_source"] = tol_src
    out["abs_delta"] = abs(a - b)

    if a == b:
        out.update(verdict="match", rule="exact", detail="identical values")
        return out
    if numbers_equal(a, b, tol):
        out.update(verdict="match", rule="tolerance",
                   detail=f"|delta| {abs(a - b):.6g} <= tolerance {tol:.6g} ({tol_src})")
        return out

    pct_ctx, pct_why = is_percent_context(ctx, g)
    outflow = is_outflow_label(ctx.label)

    # Rule 3 — percent form, tolerance measured on the golden's scale.
    if pct_ctx:
        for factor, form in ((100.0, "attempt_x100"), (0.01, "attempt_/100")):
            b2 = b * factor
            tol2, _ = tolerance_for(a, b2, precision, e_pct_fmt)
            if numbers_equal(a, b2, tol2):
                out.update(verdict="match", rule="percent_form",
                           detail=f"{form} equals golden ({pct_why})")
                return out
            if outflow and numbers_equal(abs(a), abs(b2), tol2):
                out.update(verdict="match", rule="sign_outflow+percent_form",
                           detail=f"{form}, |value| equal on an outflow row ({pct_why})")
                return out

    # Rule 2 — sign convention, outflow rows only.
    if outflow and numbers_equal(abs(a), abs(b), tol):
        out.update(verdict="match", rule="sign_outflow",
                   detail="|value| equal; question names an outflow quantity")
        return out
    if numbers_equal(abs(a), abs(b), tol):
        out["flags"].append("sign_flip_not_outflow")

    # Rule 9 — flag scale differences, never accept them.
    if a != 0 and b != 0:
        ratio = abs(a / b)
        for f in UNIT_SCALE_FACTORS:
            if abs(ratio - f) <= f * 1e-6 or abs(ratio - 1 / f) <= (1 / f) * 1e-6:
                out["flags"].append("possible_unit_scale_difference")
                break

    out.update(verdict="mismatch",
               detail=f"|delta| {abs(a - b):.6g} > tolerance {tol:.6g} ({tol_src})")
    return out


# ---------------------------------------------------------------------------
# Prompt rendering (the second consumer)
# ---------------------------------------------------------------------------

RULES_VERSION = "v6.1"


def render_rules_text() -> str:
    """The rulebook as prompt text. Rendered verbatim into template_8 so the
    judge and the harness share one definition of equality."""
    outflow = ", ".join(sorted({w for w in OUTFLOW_LEXICON if " " not in w}))
    return f"""Answer-equivalence rulebook ({RULES_VERSION}) — how to decide whether an attempt's answer equals the golden answer. These rules remove noise, not standards: a genuinely different value is wrong however it is dressed.
  1. Tolerance follows the requested precision. If the Questions sheet asks for N decimal places, values that differ by no more than ONE unit of the Nth decimal are THE SAME (two decimal places => within 0.01, so 480,051.30 and 480,051.31 are the same answer; "whole numbers" => within 1). For a percentage stored as a fraction (0.4213 shown as 42.13%), the decimal places apply to the percentage rendering (within 0.0001 of the fraction). With no stated precision, use |a-b| <= max(1e-9, 1e-6*max(|a|,|b|)). Never fail an answer for a rounding difference inside the requested precision — a penny is not a mistake; two cents is.
  2. Sign convention is accepted ONLY on outflow rows. If the question names an outflow quantity ({outflow}), an answer equal in magnitude but opposite in sign is THE SAME. On every other row the sign is part of the answer — a flipped "difference", "net", or "change" is WRONG.
  3. Percent form. When the unit is %, the question asks for a percent/rate/margin/return/ratio, the golden cell is percent-formatted, or the answer carries a literal "%", then 0.42 and 42 (and "42%") are THE SAME answer.
  4. Compare values, never rendered strings. (1,890,487.51) shown in parentheses IS -1890487.51; $, commas and % are display only; number formats, fonts and alignment belong to Formatting, not Accuracy.
  5. Sentinel text is interchangeable: "Outside model horizon", "N/A", "beyond horizon" and similar mean the same thing when compared with each other — but a sentinel where the golden has a NUMBER is a wrong answer.
  6. Numbers written as text ("42", "$1,234.00", "(480,051.30)") are read as numbers before comparing.
  7. Dates: 2027-01-01, 01/01/2027 and the Excel serial for that day are THE SAME date.
  8. Zero: 0, 0.0 and an accounting "-" are all zero. A BLANK answer cell is UNANSWERED (that is a completeness failure, not an equivalence question).
  9. Unit scale is never forgiven: 1,234 vs 1,234,000 is WRONG even if a thousands/millions convention explains it — record it as a mistake and say "possible unit-scale difference" so a reviewer can see the cause.
 10. Hardcoded answers: an answer typed as a constant instead of a live formula referencing the model is a mistake for Final calculation accuracy even when the number is right (the rubric requires the workbook to calculate it). Text answers (Yes/No, sentinels) may be literal."""
