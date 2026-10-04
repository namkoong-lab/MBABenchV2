"""66 Formatting/Zeros as dashes.

Rule (House Standards "Zeros as dashes"; rubric_9 #66; Patrick 2026-10-02: the plain rule
applies, so counters and 1/0 switches showing 0 must also show a dash).  Full write-up in
docs/checks/66.md.

Every cell holding a NUMERIC value of EXACTLY 0 (a constant or a formula result, on every
worksheet: hidden sheets, rows and columns included; whole delivered workbook) must DISPLAY a
dash.  What the cell displays is worked out as Excel does:
  * its number format's section for 0 (3+ sections: the 3rd; 1-2 sections: the 1st;
    conditional sections; built-in ids from the en-US table: 41-44 dash, 37-40 digits),
    rendered by detchecks.core.numfmt;
  * a conditional format that fires on the cell and carries a number format replaces it
    (rules evaluated per cell; GradingError when a rule cannot be evaluated and the verdict
    depends on it);
  * the sheet option showZeros="0" hides it under General and single-section formats, but NOT
    under a format with an explicit zero section (3 numeric sections), which still prints that
    section (Excel, measured 2026-10-03, base formats only: under a conditional format's number
    format a digit / text zero is UNCERTAIN -> GradingError); data bars / icon sets with
    showValue="0" hide it.
Outcome per zero cell (constants below):
  dash      only dash characters ('-', en/em dash, minus sign) besides spaces / padding,
            currency symbols, brackets and the format's own number decorations (the literals
            its positive section prints around every number: "kr", "CHF", "USD ", "x", " bps",
            '%'): '-', ' -   ', ' $-   ', ' -   kr', 'CHF -', '-x', '(-)', '* -' fill         -> pass
  digit     '0', '0.00', '0%', '$0.00', '0.00E+00', General '0', '@' -> '0'           -> FAIL
  text      visible, no digit, not only dashes ("nil", "n/a", "n-a", "zero-rated",
            a label the positive section does not print: '0;-0;"-x"')                -> FAIL  (NON_DASH_TEXT_FAILS,
                                                                                         STRICT_DASH)
  blank     ';;;', an empty zero section (the FORMAT prints nothing)                  -> pass  (BLANK_ZERO_PASSES;
                                                                                         check 94 charges it)
  hidden    showZeros=0 sheet (no zero section), hidden-value data bar / icon set   -> pass  (HIDDEN_ZERO_PASSES;
            (NOT charged by 94)                                                         question for Patrick)
  date/time a date or time format (0 shows '1/0/1900', '0:00')                        -> not judged (DATE_TIME_IN_SCOPE)
  uncertain Excel's display is unmeasured: a digit / text zero under a CONDITIONAL-format
            number format on a showZeros=0 sheet; a date section mixed with digit
            placeholders ('0 bps', '0 days': unquoted unit letters read as date codes)    -> GradingError
Never judged (not numeric zeros): booleans (FALSE), text '0', errors, empty cells, formulas
returning "".  Non-anchor cells of a merged range are not displayed and are not judged
(SKIP_MERGED_NON_ANCHOR).  Values that are not exactly 0 (a 2.33E-10 residue showing '0.00')
are not zeros (DISPLAYED_ZERO_COUNTS = False).

Values are read only where the decision needs them: a formula result is read only when its
formatting would show a zero as something other than a pass (base format on this sheet, or a
conditional format that may fire).  An untrusted value is not refused at once: the cell is
buffered until the sheet tail (merges, conditional formats) is known, and GradingError (no
fallback) is raised only if a zero there would still fail (MAX_DEFERRED cells per sheet are
buffered; the rest are re-checked in a second pass).
Members of a multi-cell array / data-table range that are not written in the file (no <c>; some
writers store only the anchor) are displayed by Excel under the row / column style; when such a
style (or a conditional format there) could show a zero as a non-pass, their values are needed
and unavailable -> GradingError.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from functools import lru_cache
from typing import Optional

from ..core import numfmt as N
from ..core.refs import location, make_ref, parse_range, range_to_str
from ..errors import GradingError
from ._cftext import text_rule_fires
from .base import Check

# ---------------------------------------------------------------------------- policy switches
BLANK_ZERO_PASSES = True        # a zero its FORMAT prints as nothing is not a '0' (check 94 charges format blanks)
HIDDEN_ZERO_PASSES = True       # a zero hidden by the sheet option showZeros="0" or by a data bar / icon set with
                                # showValue="0" passes (check 94 does NOT charge these; question for Patrick)
NON_DASH_TEXT_FAILS = True      # a zero shown as visible non-dash text ("nil", "n/a", '#####') fails
STRICT_DASH = True              # a dash display may hold only dash characters besides spaces / padding, currency
                                # symbols, brackets and the format's own number decorations (review 66-S1: the
                                # literals / '%' its positive section prints around every number, so ' -   kr',
                                # 'CHF -', 'USD -', '-x', '- bps' are dashes exactly like ' $-   ' and ' CHF -   '
                                # via [$CHF]); 'n-a', 'zero-rated', '"nil"*-', '0;-0;"-x"' are text, not a dash
DATE_TIME_IN_SCOPE = False      # zeros under date/time/elapsed formats ('1/0/1900', '0:00') are not judged
DISPLAYED_ZERO_COUNTS = False   # True: non-zero values that DISPLAY as zero (0.0004 under '0') count too
SKIP_MERGED_NON_ANCHOR = True   # values hidden under a merged range (not its top-left cell) are not displayed
CF_COUNTS = True                # conditional-format number formats count where they fire
MAX_UNKNOWN_RULES = 8           # more unevaluable CF rules on one cell than this -> GradingError
MAX_DEFERRED = 50_000           # untrusted formula cells buffered per sheet until the tail is known (rest: 2nd pass)
MAX_EXAMPLES = 8

# ---------------------------------------------------------------------------- display classes
DASH, DIGIT, TEXT, BLANK, HIDDEN, DATETIME, NONZERO, UNCERTAIN = (
    "dash", "digit", "text", "blank", "hidden", "datetime", "nonzero", "uncertain")
DASH_CHARS = "-\u2013\u2014\u2212"     # hyphen, en dash, em dash, minus sign (as core numfmt)
_DECOR = "()[]"                         # brackets allowed around a dash: '(-)'
_CUR_TAG_RX = re.compile(r"\[\$([^\]]*)\]")


def fails(cls: str) -> bool:
    """Does a zero (or displayed zero) of this display class break the rule?"""
    if cls == DIGIT:
        return True
    if cls == TEXT:
        return NON_DASH_TEXT_FAILS
    if cls == BLANK:
        return not BLANK_ZERO_PASSES
    if cls == HIDDEN:
        return not HIDDEN_ZERO_PASSES
    if cls == DATETIME:
        return DATE_TIME_IN_SCOPE
    return False                                  # DASH, NONZERO


_NUMBER_KINDS = ("number", "scientific", "fraction", "general")


@lru_cache(maxsize=4096)
def decorations(code: str) -> tuple:
    """The literal strings a format prints around every POSITIVE number (review 66-S1): the
    quoted / escaped literals, [$xxx] currency symbols and '%' of the section a positive value
    uses, when that section lays out a number.  A currency code ("kr", "CHF", "USD ", "R$ "), a
    unit label ("x", " bps") or '%' next to the zero section's dash is such a decoration, so
    ' -   kr', 'CHF -', '-x', '-%' are dashes like ' $-   ' (Excel's own sv-SE / de-CH accounting
    formats quote the currency code).  A literal holding a digit or a dash is never one, nor is
    anything of a literal-only positive section ('"nil";"nil";"nil"-'); spaces are dropped anyway.
    Longest first, so 'R$' is removed before '$' could be."""
    try:
        f = N.parse_format(code)
    except GradingError:
        return ()
    idx = N.select_section(f, 1.0)[0]
    if idx is None or f.sections[idx].kind not in _NUMBER_KINDS:
        return ()
    out = set()
    for kind, v in f.sections[idx].tokens:
        s = v if kind == "lit" else "%" if kind == "pct" else ""
        s = s.strip()
        if s and not any(ch.isdigit() or ch in DASH_CHARS for ch in s):
            out.add(s)
    return tuple(sorted(out, key=lambda s: (-len(s), s)))


def pure_dash(text: str, fill: Optional[str], code: str) -> bool:
    """A display that is only a dash: after removing spaces / padding, currency symbols (Unicode
    Sc, and [$xxx-nnn] currency tags of the code), brackets and the format's own number
    decorations (`decorations`: the literals its positive section prints, review 66-S1), only
    dash characters remain (at least one, or a dash fill with nothing else visible).  ' $-   ',
    '(-)', '--', '* -', ' -   kr' / 'CHF -' / 'USD -' (quoted currency codes), '-x' / '-%' / '- bps'
    (unit labels the positive section shows too) are dashes; 'n-a', 'zero-rated', 'N/A - nil',
    'nil' with a '-' fill, and '-x' under '0;-0;"-x"' (no 'x' on the numbers) are not."""
    t = text
    for m in _CUR_TAG_RX.finditer(code or ""):
        sym = m.group(1).split("-", 1)[0]
        if sym:
            t = t.replace(sym, "")
    for dec in decorations(code or ""):
        t = t.replace(dec, "")
    rest = [ch for ch in t if not (ch.isspace() or ch in _DECOR or unicodedata.category(ch) == "Sc")]
    if rest:
        return all(ch in DASH_CHARS for ch in rest)
    return fill is not None and fill in DASH_CHARS


def mixed_date_section(code: str, section_index: Optional[int]) -> bool:
    """Does a date / elapsed section also hold digit placeholders outside a fractional-seconds
    group ('ss.000')?  '0 bps', '0 days', '0 yrs', '#,##0 d' are numeric formats whose unquoted unit
    letters (b d y m h s) the format engine reads as date codes; Excel's Format Cells dialog rejects
    them, agent tools (openpyxl) write them anyway, and what Excel then shows is unmeasured
    (review 66-S3) -> UNCERTAIN, never 'a date, not judged'.  The fractional-seconds group is
    recognised exactly as the engine renders it (a '.' followed by '0' placeholders)."""
    if section_index is None:
        return False
    try:
        toks = N.parse_format(code).sections[section_index].tokens
    except (GradingError, IndexError):
        return False
    i, n = 0, len(toks)
    while i < n:
        k, v = toks[i]
        if k == "dot" and i + 1 < n and toks[i + 1] == ("digit", "0"):
            i += 1
            while i < n and toks[i] == ("digit", "0"):
                i += 1
            continue
        if k == "digit":
            return True
        i += 1
    return False


def classify(value: float, code: str, date1904: bool = False) -> tuple[str, str]:
    """(display class, rendered text) of a numeric value under a format code."""
    r = N.render(value, code, date1904=date1904)
    if not r.certain:
        return UNCERTAIN, r.text
    if r.kind in ("date", "elapsed"):
        if mixed_date_section(code, r.section_index):
            return UNCERTAIN, r.text
        return DATETIME, r.text
    if r.is_blank:
        return BLANK, r.text
    digits = [ch for ch in r.text if ch.isdigit()]
    if value == 0:
        if r.zero_as_dash:
            if not STRICT_DASH or pure_dash(r.text, r.fill_char, code):
                return DASH, r.text
            return TEXT, r.text
        return (DIGIT if digits else TEXT), r.text
    if digits and all(ch == "0" for ch in digits):
        return DIGIT, r.text                      # a non-zero value that displays as zero
    return NONZERO, r.text


# ---------------------------------------------------------------------------- CF evaluation
UNKNOWN = object()
_NUM_RX = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_REF = r"(\$?)([A-Za-z]{1,3})(\$?)(\d{1,7})"
_WRAP_RX = re.compile(r"\b(?:ABS|ROUND|ROUNDUP|ROUNDDOWN|TRUNC|INT)\(\s*(\$?[A-Za-z]{1,3}\$?\d{1,7})\s*(?:,\s*-?\d+\s*)?\)",
                      re.I)
_SELF_CMP_RX = re.compile(r"^\s*" + _REF + r"\s*(<>|<=|>=|=|<|>)\s*(.+?)\s*$")
_CMP_SELF_RX = re.compile(r"^\s*(.+?)\s*(<>|<=|>=|=|<|>)\s*" + _REF + r"\s*$")
_FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "<>": "<>"}
_OPS = {"=": lambda k: k == 0, "<>": lambda k: k != 0, "<": lambda k: k < 0, ">": lambda k: k > 0,
        "<=": lambda k: k <= 0, ">=": lambda k: k >= 0}


def literal(f: Optional[str]):
    """A CF operand / expression as a literal (float, str, bool) or UNKNOWN."""
    if f is None:
        return UNKNOWN
    t = f.strip()
    if t.startswith("="):
        t = t[1:].strip()
    if _NUM_RX.match(t):
        return float(t)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"' and '"' not in t[1:-1].replace('""', ""):
        return t[1:-1].replace('""', '"')
    if t.upper() in ("TRUE", "FALSE"):
        return t.upper() == "TRUE"
    return UNKNOWN


def _rank(v) -> int:
    return 2 if isinstance(v, bool) else 1 if isinstance(v, str) else 0


def _cmp(a, b) -> int:
    """Excel comparison: numbers < text < logicals; text case-insensitive."""
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 1:
        a, b = a.casefold(), b.casefold()
    return (a > b) - (a < b)


def _col_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def fires(rule, value: float, where=None):
    """True / False when the CF rule fires / does not fire on a cell holding the number
    `value`; UNKNOWN when that cannot be told from the cell alone.  where = (row, col,
    anchor_row, anchor_col): relative references in a CF formula are relative to the top-left
    cell of the rule's first range."""
    t = rule.type
    if t in ("dataBar", "iconSet", "colorScale"):
        return True                               # applies to every number in its range
    if t == "cellIs":
        ops = [literal(f) for f in rule.formulas]
        if not ops or any(o is UNKNOWN for o in ops):
            return UNKNOWN
        op = rule.operator or "between"
        if op in ("between", "notBetween"):
            if len(ops) < 2:
                return UNKNOWN
            lo, hi = sorted(ops[:2], key=lambda x: (_rank(x), x.casefold() if isinstance(x, str) else x))
            inside = _cmp(value, lo) >= 0 and _cmp(value, hi) <= 0
            return inside if op == "between" else not inside
        k = _cmp(value, ops[0])
        f = {"equal": k == 0, "notEqual": k != 0, "greaterThan": k > 0, "lessThan": k < 0,
             "greaterThanOrEqual": k >= 0, "lessThanOrEqual": k <= 0}.get(op)
        return UNKNOWN if f is None else f
    if t == "containsBlanks" or t == "containsErrors":
        return False                              # a number is neither blank nor an error
    if t == "notContainsBlanks" or t == "notContainsErrors":
        return True
    if t in ("containsText", "notContainsText", "beginsWith", "endsWith"):
        if rule.text is None:
            return UNKNOWN
        # containsText / notContainsText use SEARCH wildcards (Excel 2026-10-03, _cftext.py)
        return text_rule_fires(t, rule.text, N.general_text(float(value)))
    if t == "expression":
        if len(rule.formulas) != 1:
            return UNKNOWN
        return _eval_expression(rule.formulas[0], value, where)
    return UNKNOWN                                # top10, aboveAverage, duplicates, timePeriod ...


def _eval_expression(formula: Optional[str], value: float, where):
    """Literal expressions (TRUE, 1, 0), and '<self> <op> <literal>' in either order where
    <self> is the cell itself, optionally wrapped as ABS(<self>), ROUND(<self>,n) and
    similar (exact for value 0)."""
    if formula is None:
        return UNKNOWN
    f = formula.strip()
    if f.startswith("="):
        f = f[1:].strip()
    o = literal(f)
    if o is not UNKNOWN:
        if isinstance(o, str):
            return UNKNOWN
        return bool(o)
    if where is None:
        return UNKNOWN
    if value == 0:                                # ABS(0) = ROUND(0, n) = INT(0) = 0
        f = _WRAP_RX.sub(lambda m: m.group(1), f)
    r, c, r0, c0 = where
    m = _SELF_CMP_RX.match(f)
    if m:
        dc, col, dr, row, op, rhs = m.groups()
    else:
        m = _CMP_SELF_RX.match(f)
        if not m:
            return UNKNOWN
        rhs, op, dc, col, dr, row = m.groups()
        op = _FLIP[op]
    lit = literal(rhs)
    if lit is UNKNOWN:
        return UNKNOWN
    rr = int(row) if dr else int(row) + (r - r0)
    cc = _col_num(col) if dc else _col_num(col) + (c - c0)
    if (rr, cc) != (r, c):
        return UNKNOWN                            # the rule reads another cell
    return _OPS[op](_cmp(value, lit))


class _Rule:
    """A CF rule that can change how a zero displays: it sets a number format, hides the
    value (data bar / icon set showValue=0), or stops lower-priority rules (stopIfTrue)."""
    __slots__ = ("prio", "order", "rule", "boxes", "anchor", "fmt", "fmt_err", "hide", "stop")


# ---------------------------------------------------------------------------- the check
class _Style:
    __slots__ = ("code", "err", "zcls", "ztext", "needs_value", "needs_value_hidden")


def _hide(cls: str, code: Optional[str]) -> str:
    """Display class of a ZERO under its cell's BASE format on a sheet with showZeros="0" (Excel,
    measured 2026-10-03): under General and single-section formats (no zero section of its own) a
    zero that would show a digit or text is hidden; a format with an explicit zero section still
    prints that section (its class is kept: '0.00;-0.00;0.00' -> digit, '0;-0;"nil"' -> text); a
    format with conditional sections is unmeasured (a digit / text zero becomes UNCERTAIN).  A
    dash, a format blank, a date/time and an unverified render are kept.  A number format applied
    by a CONDITIONAL format is not covered by the measurement: see `_cf_hide`."""
    if cls not in (DIGIT, TEXT) or code is None:
        return cls
    try:
        hidden = N.zero_hidden_by_show_zeros_off(code)
    except GradingError:
        return UNCERTAIN
    if hidden is None:
        return UNCERTAIN
    return HIDDEN if hidden else cls


def _cf_hide(cls: str) -> str:
    """Display class of a ZERO shown under a conditional format's number format on a sheet with
    showZeros="0": Patrick's 2026-10-03 session measured base formats only, so whether Excel
    hides such a zero or prints the CF format's digit / text is unknown -> UNCERTAIN (GradingError
    when it decides; review 66-S2).  A dash, blank or hidden outcome passes either way and is kept."""
    return UNCERTAIN if cls in (DIGIT, TEXT) else cls


class C66(Check):
    number = 66
    key = "Formatting/Zeros as dashes"
    needs_cells = True
    needs_rows = True                             # row default styles of array members missing from the file
    needs_values = True
    sheet_kinds = ("worksheet", "dialogsheet", "macrosheet")

    def start(self, wb):
        super().start(wb)
        self.st = wb.styles
        self.date1904 = bool(wb.date1904)
        self._styles: dict = {}
        self._zcache: dict = {}
        self._outcome_cache: dict = {}
        self.n_values_read = 0
        self.n_deferred_total = 0
        self.n_absent_members = 0
        self.counts = defaultdict(int)            # zero cells by outcome
        self.n_failing = 0
        self.show_zeros_off: list = []
        self.cf_rule_sheets: list = []
        self.cf_second_pass: list = []
        self.deferred_second_pass: list = []
        self.hidden_sheet_failures: list = []
        self.examples: list = []
        self.pending: dict = {}
        self._p = None

    # -------------------------------------------------------------- formats
    def _zclass(self, code: str) -> tuple[str, str]:
        x = self._zcache.get(code)
        if x is None:
            x = classify(0.0, code, self.date1904)
            self._zcache[code] = x
        return x

    def _style(self, s: int) -> _Style:
        x = self._styles.get(s)
        if x is None:
            x = _Style()
            try:
                x.code = N.resolve_format(self.st.num_fmt_id(s), self.st.num_fmts)
                x.zcls, x.ztext = self._zclass(x.code)
                x.err = None
            except GradingError as e:             # NumFmtError: raised only if a zero needs this format
                x.code, x.zcls, x.ztext, x.err = None, UNCERTAIN, "", str(e)
            other = DISPLAYED_ZERO_COUNTS and x.zcls != DATETIME
            x.needs_value = x.err is not None or x.zcls == UNCERTAIN or fails(x.zcls) or other
            hz = _hide(x.zcls, x.code)
            x.needs_value_hidden = x.err is not None or x.zcls == UNCERTAIN or fails(hz) or hz == UNCERTAIN or other
            self._styles[s] = x
        return x

    def _needs_here(self, st: _Style) -> bool:
        """Does a numeric cell under this BASE format need its value on the current sheet
        (before conditional formats, which are known only at the sheet's end)?"""
        return st.needs_value if self.show_zeros_on else st.needs_value_hidden

    def _here(self, cls: str, code: Optional[str], via_cf: bool = False) -> str:
        """Class of a zero on the current sheet: showZeros=0 applied to its BASE format (measured)
        or to a conditional format's number format (unmeasured -> UNCERTAIN for digit / text)."""
        if self.show_zeros_on:
            return cls
        return _cf_hide(cls) if via_cf else _hide(cls, code)

    def _value_class(self, v: float, st: _Style) -> tuple[str, str]:
        if v == 0:
            return st.zcls, st.ztext
        return classify(v, st.code, self.date1904)

    # -------------------------------------------------------------- first pass
    def sheet_start(self, head):
        self.cand: list = []                      # (row, col, style, value): zeros that need a decision
        self.deferred: list = []                  # (row, col, style, source): untrusted values, decided at sheet_end
        self.n_deferred = 0
        self._arrays: dict = {}                   # anchor ref -> [r1, c1, r2, c2, members seen, members, anchor top-left]
        self._arr_max_r2 = 0
        self._row_styles = defaultdict(list)      # custom row style -> rows (only rows inside open array ranges)
        self._row = (0, None, False)              # (row, custom style, recorded)
        v = head.view
        self.show_zeros_on = not (v is not None and v.show_zeros is False)
        if not self.show_zeros_on:
            self.show_zeros_off.append(head.name)

    def row(self, row):
        s = row.style
        rec = False
        if s is not None and self._arrays and row.r <= self._arr_max_r2:
            self._row_styles[s].append(row.r)
            rec = True
        self._row = (row.r, s, rec)

    def _array_bookkeeping(self, cell):
        """Count the members of multi-cell array / data-table ranges that are written in the file;
        ranges with all members present are dropped (only incomplete ones are kept)."""
        a = cell.array
        if a is not None:
            rec = self._arrays.get(a.anchor_ref)
            if rec is not None:
                rec[4] += 1
                if rec[4] >= rec[5]:
                    del self._arrays[a.anchor_ref]
            return
        f = cell.formula
        if f is None or f.kind not in ("array", "dataTable") or not f.ref:
            return
        b = parse_range(f.ref)
        if b is None or (b[0] == b[2] and b[1] == b[3]):
            return
        need = (b[2] - b[0] + 1) * (b[3] - b[1] + 1) - 1
        self._arrays[cell.ref] = [b[0], b[1], b[2], b[3], 0, need, (b[0], b[1]) == (cell.row, cell.col)]
        self._arr_max_r2 = max(self._arr_max_r2, b[2])
        r, s, rec = self._row
        if s is not None and not rec and r == cell.row:
            self._row_styles[s].append(r)
            self._row = (r, s, True)

    def _numeric(self, cell, read: bool):
        """The cell's number (float), or None when it holds no number.  read=False: only when
        known without a formula-value read that could raise (stats)."""
        if cell.is_formula_result:
            if read:
                self.n_values_read += 1
                v = self.require_value(cell)
            elif cell.t == "n" and cell.value_trusted:
                v = cell.value
            else:
                return None
        else:
            if cell.t != "n":
                return None                       # text, boolean, error, ISO date
            v = cell.value
        if isinstance(v, bool) or not isinstance(v, float):
            return None
        return v

    def cell(self, cell):
        if cell.is_blank:
            return
        if cell.is_formula_result:
            self._array_bookkeeping(cell)
        elif cell.t != "n":
            return                                # text '0', booleans, errors: not numeric zeros
        st = self._styles.get(cell.s) or self._style(cell.s)
        if not self._needs_here(st):
            # the format shows a zero as a pass (dash / blank / hidden) or not judged (date/time): no
            # value needed (a CF number format that could change that is handled by the second pass);
            # zeros whose value is known without a read that could raise are counted for the stats
            v = self._numeric(cell, read=False)
            if v == 0:
                self.counts[f"base_{self._here(st.zcls, st.code)}"] += 1
            return
        if cell.is_formula_result and not cell.value_trusted and not DISPLAYED_ZERO_COUNTS:
            # the value is needed unless the tail (a merge, a conditional format) makes any zero
            # here pass: decide at sheet_end, raise there if it is still needed
            self.n_deferred += 1
            if len(self.deferred) < MAX_DEFERRED:
                self.deferred.append((cell.row, cell.col, cell.s, cell.value_source))
            return
        v = self._numeric(cell, read=True)
        if v is None:
            return
        if v != 0 and not DISPLAYED_ZERO_COUNTS:
            return
        if st.err is not None:
            raise GradingError(f"{self.key}: {cell.sheet}!{cell.ref} holds the number {v!r} but its number "
                               f"format cannot be read: {st.err}")
        if v != 0:
            cls = self._value_class(v, st)[0]
            if cls != UNCERTAIN and not fails(cls):
                return                            # a CF number format could still show it as 0: second pass
        self.cand.append((cell.row, cell.col, cell.s, v))

    # -------------------------------------------------------------- conditional formats
    def _cf_rules(self, tail) -> list:
        out = []
        order = 0
        for cf in tail.conditional_formats:
            if not cf.ranges:
                continue
            anchor = (cf.ranges[0][0], cf.ranges[0][1])
            for rule in cf.rules:
                order += 1
                x = _Rule()
                x.prio = rule.priority if rule.priority is not None else 10 ** 9
                x.order, x.rule, x.boxes, x.anchor = order, rule, list(cf.ranges), anchor
                x.fmt = x.fmt_err = None
                x.hide = rule.type in ("dataBar", "iconSet") and rule.show_value is False
                x.stop = bool(rule.stop_if_true)
                if rule.type not in ("dataBar", "iconSet", "colorScale"):
                    d = rule.dxf if rule.dxf is not None else self.st.dxf(rule.dxf_id)
                    if d is not None:
                        if d.num_fmt_code is not None:
                            x.fmt = d.num_fmt_code
                        elif d.num_fmt_id is not None:
                            try:
                                x.fmt = N.resolve_format(d.num_fmt_id, self.st.num_fmts)
                            except GradingError as e:
                                x.fmt, x.fmt_err = "", str(e)
                if x.fmt is None and not x.hide and not x.stop:
                    continue
                out.append(x)
        out.sort(key=lambda x: (x.prio, x.order))
        return out

    @staticmethod
    def _cover(rules, r: int, c: int, index: Optional[dict] = None) -> tuple:
        """Indexes (priority order) of the rules whose ranges contain (r, c).  index: a per-sheet
        {col: [(r1, r2, i)]} cache filled lazily."""
        if index is None:
            return tuple(i for i, x in enumerate(rules)
                         if any(b[0] <= r <= b[2] and b[1] <= c <= b[3] for b in x.boxes))
        col = index.get(c)
        if col is None:
            col = [(b[0], b[2], i) for i, x in enumerate(rules) for b in x.boxes if b[1] <= c <= b[3]]
            index[c] = col
        return tuple(sorted({i for r1, r2, i in col if r1 <= r <= r2}))

    @staticmethod
    def _cover_box(rules, r1: int, c1: int, r2: int, c2: int) -> tuple:
        """Indexes of the rules whose ranges intersect a rectangle."""
        return tuple(i for i, x in enumerate(rules)
                     if any(b[0] <= r2 and r1 <= b[2] and b[1] <= c2 and c1 <= b[3] for b in x.boxes))

    def _effects(self, rules, ids, fired) -> list:
        """All (format code or None, hidden) outcomes of the covering rules (priority order):
        the first firing rule with a number format sets it; a firing hidden-value data bar
        hides the number; stopIfTrue ends the evaluation.  Unknown rules branch."""
        unknown = [k for k, f in enumerate(fired) if f is UNKNOWN]
        if len(unknown) > MAX_UNKNOWN_RULES:
            return None
        out = set()
        for combo in itertools.product((True, False), repeat=len(unknown)):
            fl = list(fired)
            for k, i in enumerate(unknown):
                fl[i] = combo[k]
            fmt, hide = None, False
            for i, f in zip(ids, fl):
                if not f:
                    continue
                x = rules[i]
                if x.fmt is not None and fmt is None:
                    if x.fmt_err is not None:
                        raise GradingError(f"{self.key}: a conditional format's number format cannot be read: "
                                           f"{x.fmt_err}")
                    fmt = x.fmt
                hide = hide or x.hide
                if x.stop:
                    break
            out.add((fmt, hide))
        return sorted(out, key=lambda e: (e[0] is None, e[0] or "", e[1]))

    def _outcomes(self, v: float, st: _Style, rules, ids, pos: Optional[tuple]) -> list:
        """[(class, text, format code, via_cf)] the cell can show under its CF rules.  pos = (row,
        col), or None for a whole range (rules that read the cell's position become unknown)."""
        fired = tuple(fires(rules[i].rule, v, None if pos is None else pos + rules[i].anchor) for i in ids)
        key = (v if DISPLAYED_ZERO_COUNTS else 0.0, st.code, ids, fired, self.show_zeros_on)
        res = self._outcome_cache.get(key)
        if res is not None:
            return res
        effects = self._effects(rules, ids, fired) if ids else [(None, False)]
        if effects is None:
            raise GradingError(f"{self.key}: a cell is covered by more than {MAX_UNKNOWN_RULES} conditional-format "
                               f"rules this check cannot evaluate")
        res = []
        for fmt, hide in effects:
            code = fmt if fmt is not None else st.code
            if hide:
                cls, text = HIDDEN, ""
            elif fmt is None:
                cls, text = self._value_class(v, st)
            else:
                cls, text = classify(v, fmt, self.date1904)
            if v == 0 and not self.show_zeros_on and cls in (DIGIT, TEXT):
                # showZeros=0: a BASE format hides the zero unless it has a zero section (measured);
                # under a CF number format Excel's reading is unmeasured -> UNCERTAIN (66-S2)
                cls = self._here(cls, code, via_cf=fmt is not None)
                if cls == HIDDEN:
                    text = ""
            res.append((cls, text, code, fmt is not None))
        self._outcome_cache[key] = res
        return res

    def _zero_could_fail(self, st: _Style, rules, ids, pos: Optional[tuple]) -> bool:
        """Could a zero under this base format (and these CF rules) break the rule or be
        undecidable?  If not, the cell's value is not needed."""
        try:
            res = self._outcomes(0.0, st, rules, ids, pos)
        except GradingError:
            return True
        return any(fails(x[0]) or x[0] == UNCERTAIN for x in res)

    def _decide(self, sheet: str, ref: str, v: float, st: _Style, rules, ids, r: int, c: int):
        """(fails?, class, text, code, via_cf) for one zero cell; GradingError when it cannot be
        decided (no guessing)."""
        res = self._outcomes(v, st, rules, ids, (r, c))
        unc = [x for x in res if x[0] == UNCERTAIN]
        if unc:
            raise GradingError(f"{self.key}: {sheet}!{ref} holds {v!r}; how Excel displays it under "
                               + ("conditional-format " if unc[0][3] else "")
                               + f"number format {unc[0][2]!r}"
                               + ("" if self.show_zeros_on else " on a sheet with showZeros=0")
                               + " is not verified, so the check cannot tell whether it shows a dash")
        verdicts = {fails(x[0]) for x in res}
        if len(verdicts) > 1:
            shows = ", ".join(sorted({repr(x[1].strip()) for x in res}))
            raise GradingError(f"{self.key}: cannot decide whether {sheet}!{ref} (value {v!r}, format {st.code!r}) "
                               f"shows a dash: it depends on conditional formatting this check cannot evaluate "
                               f"(possible displays {shows})")
        bad = [x for x in res if fails(x[0])]
        if bad:
            return (True,) + bad[0]
        return (False,) + res[0]

    # -------------------------------------------------------------- merges
    @staticmethod
    def _merge_index(tail) -> dict:
        """{col: (row starts, [(r1, r2, c1)] sorted by r1, disjoint?)} for the non-anchor test."""
        if not SKIP_MERGED_NON_ANCHOR:
            return {}
        by_col = defaultdict(list)
        for m in tail.merges:
            if m.is_single_cell:
                continue
            for col in range(m.c1, min(m.c2, m.c1 + 16384) + 1):
                by_col[col].append((m.r1, m.r2, m.c1))
        idx = {}
        for col, items in by_col.items():
            items.sort()
            disjoint = all(items[k][0] > items[k - 1][1] for k in range(1, len(items)))
            idx[col] = ([x[0] for x in items], items, disjoint)
        return idx

    @staticmethod
    def _hidden_by_merge(idx: dict, r: int, c: int) -> bool:
        """Is (r, c) a cell of a merged range other than its top-left one?  Merges do not overlap
        in a valid file, so one bisect per cell; overlapping merges fall back to a scan."""
        e = idx.get(c)
        if e is None:
            return False
        starts, items, disjoint = e
        if disjoint:
            k = bisect_right(starts, r) - 1
            if k < 0:
                return False
            r1, r2, c1 = items[k]
            return r <= r2 and (r, c) != (r1, c1)
        return any(r1 <= r <= r2 and (r, c) != (r1, c1) for r1, r2, c1 in items)

    # -------------------------------------------------------------- untrusted values
    def _untrusted_error(self, sheet: str, r: int, c: int, s: int, source: str) -> GradingError:
        prov = getattr(self.wb, "provenance", None)
        why = (f"writer={prov.writer}, value_path={'given' if prov and prov.value_path else 'none'}"
               if prov else "no provenance")
        st = self._style(s)
        fmt = (f"its number format cannot be read: {st.err}" if st.err is not None else
               f"number format {st.code!r} shows a zero as {st.ztext.strip()!r}")
        return GradingError(f"{self.key}: needs the value of {sheet}!{make_ref(r, c)} (source={source}) but it is "
                            f"untrusted ({why}); a zero there would not pass ({fmt})")

    def _check_untrusted(self, sheet, r, c, s, source, rules, merges, index):
        """Raise for an untrusted formula value unless any zero there would pass anyway."""
        if merges and self._hidden_by_merge(merges, r, c):
            return
        ids = self._cover(rules, r, c, index) if rules else ()
        if self._zero_could_fail(self._style(s), rules, ids, (r, c)):
            raise self._untrusted_error(sheet, r, c, s, source)

    def _check_deferred(self, sheet, rules, merges):
        if not self.deferred:
            return
        if not rules and not merges:              # nothing in the tail can make a zero there pass
            r, c, s, src = self.deferred[0]
            raise self._untrusted_error(sheet, r, c, s, src)
        index: dict = {}
        for r, c, s, src in self.deferred:
            self._check_untrusted(sheet, r, c, s, src, rules, merges, index)

    # -------------------------------------------------------------- array members missing from the file
    def _absent_styles(self, head, r1, c1, r2, c2) -> set:
        """Styles Excel gives to cells of the rectangle that have no <c>: the row's style when
        the row has customFormat, else the column's style, else 0."""
        styles = set()
        n_custom = 0
        for s, rows in self._row_styles.items():
            lo, hi = bisect_left(rows, r1), bisect_right(rows, r2)
            if hi > lo:
                styles.add(s)
                n_custom += hi - lo
        if n_custom < r2 - r1 + 1:
            covered = 0
            for ci in head.cols:
                lo, hi = max(ci.min, c1), min(ci.max, c2)
                if lo <= hi:
                    styles.add(ci.style or 0)
                    covered += hi - lo + 1
            if covered < c2 - c1 + 1:
                styles.add(0)
        return styles

    def _check_absent_members(self, head, rules):
        for s in self._row_styles.values():
            s.sort()
        for anchor, (r1, c1, r2, c2, seen, need, top_left) in self._arrays.items():
            absent = need - seen
            if absent <= 0:
                continue
            self.n_absent_members += absent
            rng = range_to_str(r1, c1, r2, c2)
            if not top_left:
                raise GradingError(f"{self.key}: {head.name}!{rng} is an array formula range anchored at {anchor}, "
                                   f"not at its top-left cell, with {absent} member cell(s) not written in the file; "
                                   f"the check cannot tell how Excel displays them")
            ids = self._cover_box(rules, r1, c1, r2, c2) if rules else ()
            # rows / columns that can hold a missing member (the anchor itself is written)
            rr1 = r1 + 1 if c1 == c2 else r1
            cc1 = c1 + 1 if r1 == r2 else c1
            for s in sorted(self._absent_styles(head, rr1, cc1, r2, c2)):
                st = self._style(s)
                if self._zero_could_fail(st, rules, ids, None):
                    raise GradingError(
                        f"{self.key}: {head.name}!{rng} is an array formula range (anchor {anchor}) with {absent} "
                        f"member cell(s) not written in the file; Excel shows their computed values under number "
                        f"format {st.code!r} (a zero would show {st.ztext.strip()!r}), and their values are not "
                        f"available (no cell in the file; a value copy is not read for missing cells)")

    # -------------------------------------------------------------- sheet end
    def sheet_end(self, head, tail):
        self._outcome_cache = {}                  # keyed by rule indexes: valid for one sheet only
        rules = self._cf_rules(tail) if CF_COUNTS else []
        if rules:
            self.cf_rule_sheets.append(head.name)
        merges = self._merge_index(tail) if (self.cand or rules or self.deferred) else {}
        self.n_deferred_total += self.n_deferred
        self._check_deferred(head.name, rules, merges)
        if self._arrays:
            self._check_absent_members(head, rules)
        hits = self._judge_all(head.name, self.cand, rules, merges)
        cf_second = bool(rules) and self._needs_second_pass(rules)
        overflow = self.n_deferred > len(self.deferred)
        if cf_second or overflow:
            boxes = [b for x in rules for b in x.boxes]
            self.pending[head.name] = {"rules": rules, "merges": merges, "cf": cf_second, "overflow": overflow,
                                       "seen_untrusted": 0,
                                       "done": {(r, c) for r, c, _s, _v in self.cand},
                                       "show_zeros_on": self.show_zeros_on, "hits": hits, "index": {},
                                       "bbox": ((min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                 max(b[2] for b in boxes), max(b[3] for b in boxes))
                                                if boxes else (0, 0, -1, -1))}
            if cf_second:
                self.cf_second_pass.append(head.name)
            if overflow:
                self.deferred_second_pass.append(head.name)
            self.request_second_pass(head.name, None)
        else:
            self._commit(head, hits)
        self.cand = []
        self.deferred = []
        self._arrays = {}
        self._row_styles = defaultdict(list)

    def _judge_all(self, sheet, cands, rules, merges) -> dict:
        hits = {}                                  # (text, code, via_cf, cls) -> [(r, c)]
        index: dict = {}
        for r, c, s, v in cands:
            if merges and self._hidden_by_merge(merges, r, c):
                self.counts["merged_non_anchor"] += 1
                continue
            st = self._style(s)
            ids = self._cover(rules, r, c, index) if rules else ()
            self._judge_one(sheet, r, c, v, st, rules, ids, hits)
        return hits

    def _judge_one(self, sheet, r, c, v, st, rules, ids, hits):
        bad, cls, text, code, via_cf = self._decide(sheet, make_ref(r, c), v, st, rules, ids, r, c)
        if v == 0:
            self.counts[f"judged_{cls}"] += 1
        if bad:
            hits.setdefault((text.strip(), code, via_cf, cls), []).append((r, c))
            if len(self.examples) < MAX_EXAMPLES:
                self.examples.append(f"{location(sheet, make_ref(r, c))} = {v!r} shows {text.strip()!r} "
                                     f"under {code!r}{' (conditional format)' if via_cf else ''}")

    def _needs_second_pass(self, rules) -> bool:
        """Could a CF number format show a zero as a non-pass on a cell whose BASE format shows
        it as a pass (such cells were not read in the first pass)?"""
        for x in rules:
            if x.fmt is None:
                continue
            if x.fmt_err is not None:
                return True
            if DISPLAYED_ZERO_COUNTS:
                return True
            cls = self._here(classify(0.0, x.fmt, self.date1904)[0], x.fmt, via_cf=True)
            if cls == UNCERTAIN or fails(cls):
                if fires(x.rule, 0.0, None) is not False:
                    return True
        return False

    def _commit(self, head, hits: dict):
        hidden = head.state != "visible"
        order = sorted(hits.items(), key=lambda kv: min(kv[1]))
        n_sheet = 0
        for (text, code, via_cf, cls), cells in order:
            n_sheet += len(cells)
            what = "numeric value(s) shown as zero" if DISPLAYED_ZERO_COUNTS else "zero value(s)"
            src = f"conditional-format number format {code!r}" if via_cf else f"number format {code!r}"
            if cls == HIDDEN:                     # only when HIDDEN_ZERO_PASSES is False
                shows = "are hidden by the sheet's hide-zeros option or a hidden-value data bar / icon set"
            elif cls == BLANK:                    # only when BLANK_ZERO_PASSES is False
                shows = f"display nothing ({src})"
            else:
                shows = "display " + repr(text) + f" ({src})"
            self.add_cell_mistakes(head.name, cells,
                                   "{n} " + what + " {range} " + shows + " instead of a "
                                   f"dash{' (hidden sheet)' if hidden else ''}.")
        self.n_failing += n_sheet
        if hidden and n_sheet:
            self.hidden_sheet_failures.append(head.name)

    # -------------------------------------------------------------- second pass
    def second_pass_start(self, head):
        p = self.pending[head.name]
        self._p = p
        self.show_zeros_on = p["show_zeros_on"]
        self._outcome_cache = {}

    def second_pass_cell(self, cell):
        if cell.is_blank:
            return
        if not cell.is_formula_result and cell.t != "n":
            return
        p = self._p
        r, c = cell.row, cell.col
        st = self._style(cell.s)
        if self._needs_here(st) and not DISPLAYED_ZERO_COUNTS:
            # decided in the first pass, except untrusted values beyond the first MAX_DEFERRED
            # (same predicate, same stream order as in cell())
            if p["overflow"] and cell.is_formula_result and not cell.value_trusted:
                p["seen_untrusted"] += 1
                if p["seen_untrusted"] > MAX_DEFERRED:
                    self._check_untrusted(cell.sheet, r, c, cell.s, cell.value_source, p["rules"], p["merges"],
                                          p["index"])
            return
        if not p["cf"]:
            return
        bb = p["bbox"]
        if not (bb[0] <= r <= bb[2] and bb[1] <= c <= bb[3]) or (r, c) in p["done"]:
            return
        rules = p["rules"]
        ids = self._cover(rules, r, c, p["index"])
        if not ids or not any(rules[i].fmt is not None for i in ids):
            return
        if not DISPLAYED_ZERO_COUNTS and not self._zero_could_fail(st, rules, ids, (r, c)):
            return                                # value-independent screen: no value needed
        v = self._numeric(cell, read=True)
        if v is None or (v != 0 and not DISPLAYED_ZERO_COUNTS):
            return
        if st.err is not None:
            raise GradingError(f"{self.key}: {cell.sheet}!{cell.ref} holds the number {v!r} but its number "
                               f"format cannot be read: {st.err}")
        if p["merges"] and self._hidden_by_merge(p["merges"], r, c):
            self.counts["merged_non_anchor"] += 1
            return
        self._judge_one(cell.sheet, r, c, v, st, rules, ids, p["hits"])

    def second_pass_end(self, head, tail):
        self._commit(head, self._p["hits"])
        self._p = None

    # -------------------------------------------------------------- verdict
    def finish(self) -> dict:
        c = self.counts
        stats = {
            "zero_cells_failing": self.n_failing,
            "zero_cells_by_display": {k: v for k, v in sorted(c.items())},
            "formula_values_read": self.n_values_read,
            "untrusted_values_not_needed": self.n_deferred_total,
            "array_members_not_in_file": self.n_absent_members,
            "show_zeros_off_sheets": self.show_zeros_off,
            "cf_rule_sheets": self.cf_rule_sheets,
            "cf_second_pass_sheets": self.cf_second_pass,
            "deferred_second_pass_sheets": self.deferred_second_pass,
            "hidden_sheets_with_failures": self.hidden_sheet_failures,
            "examples": self.examples,
            "policy": {"blank_zero_passes": BLANK_ZERO_PASSES, "hidden_zero_passes": HIDDEN_ZERO_PASSES,
                       "non_dash_text_fails": NON_DASH_TEXT_FAILS, "strict_dash": STRICT_DASH,
                       "date_time_in_scope": DATE_TIME_IN_SCOPE, "displayed_zero_counts": DISPLAYED_ZERO_COUNTS,
                       "skip_merged_non_anchor": SKIP_MERGED_NON_ANCHOR, "cf_counts": CF_COUNTS},
        }
        return self.verdict("Every numeric zero displays as a dash (or is blanked or hidden).",
                            "{n} block(s) of numeric zeros display a digit or text instead of a dash "
                            f"({self.n_failing} cell(s)).", stats)
