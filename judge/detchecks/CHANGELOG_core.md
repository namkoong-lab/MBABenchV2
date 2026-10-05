# Core changelog (`detchecks/core/` and shared check modules)

## 2026-10-04 — No hidden rows/columns (93) and No bright-yellow highlighting (47) read `<col>` the shared way

Source: follow-ups of the overlapping-`<col>` core fix (below) and the judge v13 review. No hidden rows/columns
(93) walked the raw entries and only ever added reasons, so a later visible `<col>` never cleared an earlier hidden
one (`C:XFD hidden` + `D:D visible` failed as `C:XFD`), although 93.md said "the later entry wins"; No bright-yellow
highlighting (47) took its column-style hits (and the column styles it admits for unknown-colour readings) from the
raw entries, so an overridden yellow style still painted.

### checks
- 93: the column loop walks `head.col_segments()` (core.sheet.paint_cols: the later entry - by `min`, then file
  order - wins on the columns it covers, as a whole: hidden flag, width, outline level, collapsed mark).
- 47: column-style hits and the admitted column styles come from `head.col_segments()`; a hit's range is the
  segment's (an entry split by a later one gives one hit per remaining part).
- Only Negatives in parentheses (65) and Zeros as dashes (66) still read `cols` directly (styles of missing array
  members; reader.md section 7).

### tests
- `test_93_overlapping_cols_later_entry_wins` (rowscols) and `test_47_overlapping_col_styles_later_entry_wins`
  (fills); both fail on the previous code.

### Corpus effect (2026-10-04, one guarded job before / after, 47 and 93 only: neither needs values, no LibreOffice)
- 374 files: 347 graded, 27 not graded (over 10 MB and not saved by Excel); before and after: 47 pass 347, 93 pass
  347; **0 changes** in decisions, mistakes (location and description) or the kept stats (`col_style_hits`,
  `row_style_hits`, `bright_cells`, hidden / zero-width / default-zero-width columns, exempt groups).

## 2026-10-04 — the LibreOffice step: encoded paths; a conversion never outlives its grading (judge v13 reviews)

Source: the two judge v13 reviews (ops: LibreOffice aborts on a task folder with a space, task 28
"FruitJuice_3-Statement-Model - v2"; a '%41' sent the profile to a decoded folder; SIGTERM to the grader
orphaned soffice, re-parented to pid 1 with no timeout - probes probe_space.py, probe_lo_paths.py, P20 of
probe_failures.py).

### core/recalc.py
- `file_url(path)` (new): every path handed to soffice - the profile (`-env:UserInstallation`), `--outdir`
  and the source - is `Path(abspath).as_uri()`. Before: `f"file://{profile}"` raw (a space: exit -6, no copy;
  `%41`: profile decoded to another folder) and a plain `--outdir` path (also decoded by LibreOffice: the copy
  went to `PctATask/...`, "produced no copy").
- `libreoffice_recalc`: soffice runs in the grader's process group (no `start_new_session`) under
  `core/lo_watchdog.py`; stdin is /dev/null; the run is registered in `_ACTIVE` while it runs; a timeout or any
  exception (KeyboardInterrupt too) kills the whole tree; every run ends with `reap_profiles(profile)`.
- `install_termination_reaper()` (new): SIGTERM / SIGHUP handler, installed once from the main thread where
  the signal still had its default action; it kills this process's running conversions
  (`kill_active_libreoffice`) and then the process dies of the signal exactly as before.
- `reap_profiles(path, subtree)` (new): kills, as whole trees, the processes whose command line names a
  profile (raw path or file URL); never this process or its ancestors. `_kill_tree` now kills by tree
  (before: `killpg` of soffice's own session).

### core/lo_watchdog.py (new)
- The wrapper (`-I -S`, standard library only): runs soffice as its child, passes its exit code through
  (128 + N and a stderr line when soffice died of signal N), kills soffice's whole tree when the grader is no
  longer its parent (polled every 0.5 s) or when it gets SIGTERM / SIGINT / SIGHUP. `kill_tree` (SIGSTOP the
  tree until no new child appears, then SIGKILL), `process_table`, `descendants`, `ancestors` are shared with
  core/recalc.py.

### tests
- `tests/test_recalc_libreoffice.py` (new): the real LibreOffice on a 3-cell file in a folder named with a space,
  `%41`, `%` and `é` (stale cached 999 recalculated to 95, nothing written outside, no process left); stand-in
  soffice runs: watchdog pass-through, timeout tree kill, SIGTERM and SIGKILL to a stand-in grader, the watchdog
  alone, the handler's rules, the profile sweep. utils/det_checks' reaper uses `reap_profiles` (test_det_checks:
  encoded profile URL, and the reviewer's P20 at adapter level with SIGTERM and SIGKILL).

### Verdict effect
- None: only how LibreOffice is started and stopped changed; the copy's values are the same (a folder with a
  space or `%` used to fail the grading loudly; it is now graded).

## 2026-10-04 — unresolvable colours are an explicit unknown (finding 47-R2-08)

Source: second-review finding 47-R2-08 (an unresolvable fill colour read as "no fill", so No bright-yellow
highlighting (47) and No white-on-white hiding (94) passed silently). Not done by raising in `resolve` (the
review's proposal): that would raise for colours no verdict depends on.

### core/styles.py
- `UnknownColour(ref, why)` (frozen, hashable; `str()` names the reference and why; not a string, so slicing it
  like `'FFRRGGBB'` fails loudly) and `is_unknown(x)`. `Styles.resolve` returns it - never None, black or "no
  fill" - for: an rgb that is not 6 or 8 hex digits (before: None when the last six were not hex, otherwise the
  last six digits were used, e.g. `ZZFF0000` read red), `rgb=""` (was skipped), a theme index outside the 12
  slots or not a number (None), an indexed value outside the palette other than 64 / 65 (None; e.g. 81, the
  tooltip-text colour of comment fonts), an invalid custom `<indexedColors>` entry (was black), a tint that is not
  a number in [-1, 1] (was ignored / clamped). Resolvable colours resolve exactly as before.
- `Color.bad` (an attribute as stored that is not a valid value); `Styles.unknown_indexed`,
  `Styles.indexed_colour(i)`.
- `font_color(s)` returns the `UnknownColour` (was black). `fill_paint`: an unknown colour the paint uses makes
  `effective` that `UnknownColour` (was: no fill for a solid fill, black / white defaults in a hatch blend, a
  dropped gradient stop); one it does not use (a solid cell fill's bgColor, a dxf solid fill's fgColor) changes
  nothing. `fill_color`, `cell_fill`, `dxf_fill_color`, `dxf_font_color` pass it on. Building the style table
  never raises.

### checks
- No bright-yellow highlighting (47): when an examined position (cell, row / column style, style 0, a
  conditional format's painted colour) first uses an unknown fill colour, the check forks into one reading per
  way Excel could paint it - no fill, bright yellow FFFF00, another colour - every combination for several
  distinct ones (at most 4, 81 readings; a fifth raises). All readings agree → that verdict (the no-fill
  reading's mistakes; stats `unresolved_fill_colours`, `unresolved_fill_readings`); otherwise `GradingError`.
  The swatch / legend / WIP logic runs unchanged in every reading.
- No white-on-white hiding (94): `conceals` is three-valued (None = an unknown colour decides); a style with an
  unknown font / fill is risky (values read); unknown rich-run, conditional-format font / fill / colour-scale and
  `[ColorN]` palette colours likewise; such a cell is undecided (raises only when nothing else is certainly
  concealed); a sheet whose CF outcomes meet one always gets the per-cell second pass.
- Black / Green / Red font (49 / 50 / 51, `colour_rules.style_colour`): test `is_unknown` instead of `is None`;
  behaviour unchanged (they already raised only where the cell's class makes its colour decide), but the new
  unknowns (invalid palette entry, odd-length rgb, bad tint) now reach them instead of a guessed colour.

### tests
- `test_unresolvable_colours_are_unknown` (reader; and `test_styles_and_colours` now asserts `is_unknown(...)`
  where it asserted the silent `None` this finding is about), `test_47_unresolvable_fill_colours`,
  `test_94_unresolvable_colours` (fills), `test_unresolvable_colours_2026_10_04` (colours).

### Corpus effect (the runs described in the overlapping `<col>` entry below)
- The only unresolvable references in the corpus's styles.xml, sharedStrings.xml rich runs, conditional formats
  and theme / palette parts are `indexed="81"` font colours of fonts no cellXf uses (44 in 21 files; 212 in 78 toy
  files; `scratch/core_fixes/scan_core.py`). 0 verdict changes for 47, 49, 50, 51 and 94; the wording of 94's undecided
  reason changed in attempts 2924 (raises before and after: a conditional white font, unchanged) and 211 (stats).

## 2026-10-04 — number formats mixing placeholders with unquoted date letters are unverified (finding 66-S3)

Source: review finding 66-S3 (openpyxl writes `0 bps`, `0 days`, `#,##0 d` unquoted; the engine read the unit
words as date codes) and 66.md question 13; until now only Zeros as dashes (66) caught them, in the check.

### core/numfmt.py
- `mixed_date_letters(tokens)` (new): a section mixes a **placeholder** - `0 # ?` except the `0`s of a
  fractional-seconds group (a `.` right after `s` / `ss` / `[s]` / `[ss]`), `General`, `@` - with an
  **unquoted date-time letter** - `y m d h s` (any case), `e g b` (`E` before `+`/`-` is scientific), `AM/PM`,
  `A/P`, `[h] [m] [s]`. `parse_format` then sets `NumberFormat.verified = False` and `unverified_why`.
- `render` under an unverified code: numbers `certain=False` (whatever section they use); text and logicals only
  when the code has a text section (without one they show as typed whatever Excel makes of the code); empty cells
  and errors stay certain. The rendered text is unchanged.

### checks
- Zeros as dashes (66): `mixed_date_section` removed - the core covers it (`classify` returns `uncertain` on
  `certain=False` as before). The core rule is slightly wider (an era letter beside digits, `0 b`; `General` /
  `@` beside date letters; digits after a `.` that follows no seconds code, `d.00`).
- No white-on-white hiding (94): honours `certain=False` (it ignored the flag): "prints nothing" and "prints
  something", in the font colour or any colour tag of the code, are all tried; undecided where they disagree.
- Reasonable column widths (70): an unverified number rendering is undecided like an untrusted value (it was
  measured). This also applies to the engine's older `certain=False` cases (no section applies; a negative in the
  first section when only the second is conditional).
- Negatives in parentheses (65), Sufficient column widths (69): unchanged (they already treat `certain=False` as
  uncertain).

### Corpus effect (the runs described in the overlapping `<col>` entry below)
- 0 of the 392 distinct custom codes of the corpus and toys (cellXfs and dxf `<numFmt>`) and 0 built-ins are
  unverified (`scratch/core_fixes/codes_letters.py`); 0 verdict changes for 65, 66, 69, 70, 73 and 94; nothing
  newly raises, so no Excel check workbook was needed.

## 2026-10-04 — overlapping `<col>` entries: one rule in the reader (Sufficient column widths (69), Reasonable column widths (70), Reasonable row heights (73))

Source: side finding of the Reasonable column widths (70) build; the rule is the one 70 and Reasonable row
heights (73) (73.md question 12) had decided - the later entry wins. No Excel measurement or toy settles what
Excel does (no toy has overlapping entries; docs/excel_measurements.md has nothing on it).

### core/sheet.py
- `paint_cols(cols)` moved here from checks/c70.py (unchanged; c70 re-exports it): disjoint `(lo, hi, ColInfo)`
  runs, each entry - in the reader's order, by `min` then file order - overriding earlier ones on the columns it
  covers, as a whole (width, hidden, style; an entry without a width wins and means the default width).
- `SheetHead.col_segments()` and `SheetHead.col_run(c)` (new); `col_info(c)` / `col_style(c)` follow the same
  rule. Before: the last entry whose `min` ≤ c if it covered c, else None - the sheet default for every column an
  earlier range covers after a later entry starting inside it (`C:XFD 18` + `D:D 3`: F onwards read 8.43).
  Non-overlapping files read exactly as before.

### checks
- Sufficient column widths (69): `_SheetGeo.room` walks `head.col_run` (it copied the old reading);
  `col_pixels` / `col_style` via `col_info`. Reasonable column widths (70): `head.col_segments()` instead of its
  own `paint_cols` call (same rule). Reasonable row heights (73): unchanged code (`col_info`), now later-wins.

### Corpus effect (2026-10-04; one guarded job each before / after, recalc pipeline on, Excel off)
- 347 files graded, 27 not graded (over 10 MB and not saved by Excel, Patrick's rule of 2026-10-04: attempts 1709,
  2279, 2496, 4091; GPT-6 1322, 1335, 1379, 1476, 1545, 1557 (10.1 MB), 1599, 1600, 1601, 1736, 1744, 1749, 1750,
  1767, 1778, 1781, 1810, 1834, 2576, 2603, 2617, 2621, 2681).
- 125 graded files (151 sheets) read some columns differently; 7 have content in such columns, all widened from
  the 8.43 default (`scratch/core_fixes/overlap_effect.py`): attempts 1408 and GPT-6 1408 `Assumption!G:H`
  (17.89), GPT-6 1329 `Assumption!G` (17.89), 2245 `Assumption!G:H` (17.89), 1331 `Questions!E` (8.89), 2004
  `Questions!D:E` (8.89), 2479 `Assumptions!L:S` (10.22).
- 69, 70 and 73: **0 verdict changes**, 0 changes in mistake locations, descriptions or kept stats.

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
