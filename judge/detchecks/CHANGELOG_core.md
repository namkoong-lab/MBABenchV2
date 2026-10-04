# Core changelog (`detchecks/core/` and shared check modules)

## 2026-10-04 — recalculation pipeline, per-check live switch (for No formula errors (22))

Source: handoff.md rulings of 2026-10-04 ("Recalculation design", "Negatives in parentheses (65)
plumbing"). All changes are backward compatible: without `recalc=` and without `live=False` nothing
behaves differently.

### core/recalc.py (new; docs/recalc.md)
- `RecalcPolicy`, `ensure_values(path, workdir, policy) -> ValuePlan`: Excel-saved file → its
  caches (`source="cache"`); otherwise a LibreOffice copy (headless, private profile, threaded
  calculation and OpenCL off, OOXMLRecalcMode=always, timeout), cached in `workdir/<sha256>/`;
  gap scan (`find_gaps` / `classify_lo_error`: `#NAME?`/`#VALUE!` on functions LibreOffice lacks,
  spill references, LAMBDA names, array-evaluation differences; a misspelled function / undefined
  name / scalar `#VALUE!` is genuine); gaps → `excel_recalc` (macOS osascript, Windows COM) behind
  `policy.excel_allowed` (default True) and an availability probe, else
  `GradingError("Excel recalculation required: ...")` naming the gap cells.
- `EXCEL_FUNCTIONS` (533 names), `LO_UNSUPPORTED_ALWAYS` / `LO_ADDED_24_8` / `LO_ADDED_25_8`,
  `ARRAY_FUNCS`.

### core/values.py
- `ValueSource(value_path, errors_vetted=False)` / `SheetValueCursor(..., errors_vetted)`: with
  `errors_vetted=True` (set by the pipeline when no gap was found, or for an Excel copy) a copy
  value of `#NAME?` / `#VALUE!` is `ok` (trusted) instead of `unmeasured`. Default unchanged.
- `Provenance.value_kind` (`cache` / `libreoffice` / `excel`) and `Provenance.recalc` (the
  `ValuePlan.summary()` dict); both in `as_dict()`.

### api.py
- `grade(..., recalc=RecalcPolicy())` / `Engine(..., recalc=)`: when a selected check needs values
  and no `value_path` is given, the pipeline supplies the value source; its `GradingError` fails
  only the value checks ("values unavailable: ...").
- Every verdict gets `"live": bool` (from `Check.live`); a non-live verdict also gets
  `"live_note": "recorded only; the LLM verdict stands at scoring"`. Verdicts of checks with
  `needs_values` get `stats["values"]` (writer, source, value_path, gaps, timings) when a value
  source was involved.

### checks/base.py, checks/__init__.py
- `Check.live = True` class attribute; `C65.live = False`; `live_numbers()`.

### tools
- `run_toys` grades with the pipeline by default (`--no-recalc` to disable; all toys are
  Excel-saved so nothing is launched); `run_corpus --recalc [--no-excel] [--excel-saved-only]
  [--llm-check N]`; `grade_one --recalc`; new `recalc_one` (pipeline / forced Excel copy of one file).

## 2026-10-03 — Patrick's rulings and Excel-session measurements

Source: handoff.md rulings of 2026-10-03 and `out/excel_session_answers.md`.

### core/styles.py
- `Styles.fill_paint(fill, dxf=True)`: a conditional-format (dxf) **solid fill paints bgColor
  only** (Excel Q2). fgColor is ignored; bgColor indexed 64 and rgb `00000000` resolve to black
  (alpha ignored); no bgColor returns `FillPaint("none", ...)` (no fill). An `auto="1"` bgColor
  is read as black like indexed 64 (not measured on its own; none in the corpus). Before: bgColor,
  falling back to fgColor. `dxf_fill_color` follows (None for an fgColor-only dxf).

### core/numfmt.py
- `select_section`: no automatic minus in a section chosen by its own condition (now certain)
  and none in the fallback section after conditions (was: minus added, uncertain) (Excel Q8).
- Booleans are rendered through the text section like the text `TRUE`/`FALSE` (`kind="bool"`,
  certain; the text section's colour applies). Before: shown as typed, uncertain with a text
  section or empty sections.
- Text under a format without a text section is shown in the font colour, certain (a numeric
  section's colour tag never colours text). Before: uncertain when a one-section format had a
  colour tag.
- Empty and spaces-only format codes are certain (`""` = General, `" "` prints nothing for
  numbers); `NumberFormat.verified` is now always True.
- Elapsed times past 9999-12-31 are certain (`[h]:mm` keeps counting).
- New `zero_hidden_by_show_zeros_off(code)`: whether the sheet option showZeros=0 hides an exact
  zero under a format (True: General / lone `@` / one or two numeric sections; False: an explicit
  zero section; None: conditional format, unmeasured) (Excel Q5).

### checks/_fills.py (shared by 47 and 94)
- `dxf_fill_paints` returns the single Excel paint (or none); the fg/bg alternatives and the
  placeholder logic are removed.

### checks/_cftext.py (new; shared by 66 and 94)
- `text_rule_fires` / `search_finds`: containsText / notContainsText with Excel SEARCH
  wildcards (`*`, `?`, `~` escape), case-insensitive; beginsWith / endsWith literal (Excel Q3).

### checks/colour_rules.py (shared by 49, 50, 51)
- New `is_hyperlink_formula(text)`; `ColourCheck.cell` ignores formula cells whose top-level
  call is HYPERLINK (ruling: any colour; stats `hyperlink_cells_ignored`).
- `class_needed(fam, text)` takes the formula text; the unresolved-colour raise now happens
  after the HYPERLINK skip.
