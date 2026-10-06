# detchecks.core.numfmt — Excel number-format engine for check authors

Pure functions: a value plus a format code (or numFmtId) in, what Excel displays out.
No I/O. Used by checks 66 (zeros as dashes), 94 (formats that print nothing) and 73
(approximate display length for row heights). Priorities, in order: blank detection,
dash detection, minus versus parentheses, approximate length; exact digits follow Excel
en-US but are secondary.

```python
from detchecks.core import numfmt as N

r = N.render(0, 43)                     # built-in accounting id
r.text            # ' -   '
r.zero_as_dash    # True
N.render(1568455.52, ";;;").is_blank                     # True  (94/T3 Fail)
N.render(0, 0).zero_as_dash                              # False (General shows '0', 66/T2 Fail)
N.render(-5, "#,##0;[Red](#,##0)").parens                # True, color_rgb 'FF0000'
N.render(0, 8, custom_formats={8: '#,##0.00\\ "€";[Red]\\-#,##0.00\\ "€"'}).text   # '0.00 €'
```

## Errors — no fallback

`NumFmtError` (a `detchecks.errors.GradingError`) is raised for: a numFmtId with no
`<numFmt>` definition and no built-in meaning (23–26, 63–66, ≥ 82 undefined), malformed
codes (unterminated `"` or `[`, more than 4 sections), non-finite numbers, a numeric cell
value that is not a number, an unknown `value_type`. Do not catch it to skip a cell.
Since 2026-10-06 the checks that render cell values (65, 66, 69, 70, 94) validate a cell's
format once per style (`resolve_format` + `parse_format`) and read an unreadable one as
**General**, recorded per cell under `stats.defaults.unreadable_number_format` (Patrick,
attempt 2777 measured in Excel: Excel repairs the file by dropping the format). A
conditional-format (dxf) number format that cannot be read still gives way to the cell's own
format in 65 / 66 and raises elsewhere.

Where Excel's behaviour is **not verified**, `render` still answers but sets
`certain=False` (see Known limits). A check that would decide pass/fail on such a cell
should raise `GradingError` (or follow an explicit ruling), not guess.

## Format resolution

* `resolve_format(num_fmt_id, custom_formats=None) -> str`: the workbook's own `<numFmt>`
  wins — also for ids below 164 (toy 94/T5 redefines id 8) — then `BUILTIN_FORMATS`.
  `custom_formats` keys may be int or str.
* `BUILTIN_FORMATS`: ids 0–22, 27–49 and 50–81 as Excel en-US displays them. Id 44 is the
  corrected 4-section code (`_("$"* #,##0.00_);_("$"* \(#,##0.00\);_("$"* "-"??_);_(@_)`;
  openpyxl's copy lacks the `;`). 41–44 have a dash zero section; 37–40 do not.
* `LOCALE_DEPENDENT_IDS`: 5–8 (currency), 14–17, 22 (date order), 27–36 and 50–81
  (East Asian / Thai). Rendered en-US style (`$`, m/d/yyyy; CJK as zh-CN date codes,
  Thai as Western digits) with `Rendered.locale_dependent=True` (zero/blank/dash
  behaviour does not depend on the locale for these ids).
* `builtin_format(id) -> str | None`.

## render(value, code_or_id, *, custom_formats=None, value_type=None, date1904=False, indexed_palette=None) -> Rendered

`value`: int/float (dates are serials), str (text), bool, `None` (empty cell),
`ErrorValue("#N/A")`. `value_type` is the OOXML cell `t` attribute and wins when given:
`'n'` (str numbers are converted), `'s'`/`'str'`/`'inlineStr'` (text), `'b'` (`"1"`/`"0"`
accepted), `'e'` (error), `'d'` (ISO 8601 date). `code_or_id`: a format code (str; `None`
means General) or a numFmtId (int). An empty code `""` renders as General and a code of
spaces only (`" "`) as a literal section (blank); both are **certain** since Patrick's Excel
session (2026-10-03: 123 under an empty code shows 123 with no repair prompt; under a
one-space code it is blank). `indexed_palette`: the workbook's custom
`<indexedColors>` list (RRGGBB or ARGB strings) for `[ColorN]`.

`Rendered` (frozen):

| field | meaning |
|---|---|
| `text` | display at natural width: `_x` → one space, `?` placeholders → spaces, `*x` fill omitted |
| `is_blank` | nothing visible: empty, or only spaces/padding/space-fill (also an empty text or `"   "`) |
| `zero_as_dash` | the value is a numeric 0 and shows a dash and no digit (`-`, `–`, `—`, `−`; currency allowed: `' $-   '`) |
| `shows_dash` | same test on any value (e.g. a 2.33E-10 residue under `…;\-` shows `'-'`) |
| `minus` / `parens` | a NEGATIVE value shown with a minus/dash before its first digit (automatic or literal) / the number inside literal parentheses (numeric kinds only; dashes in dates, `000-00-0000`, and any dash before a value ≥ 0 such as `"FY"-0` or `"Year -"0` are not signs) |
| `color_tag`, `color_rgb` | `[Red]` → `'Red'`, `'FF0000'`; `[Color10]` → `'008000'` (default palette, index N+7) |
| `section_index` | section used (0–3); `None` for General-without-sections, text without a text section, booleans, errors |
| `kind` | `general number scientific fraction date elapsed text literal empty bool error hash` |
| `is_hash` | Excel shows `#####` (negative date/time, value beyond 9999-12-31, no section for the value) |
| `fill_char` | the `*x` character (a non-space fill counts as visible) |
| `certain` | `False` where Excel's behaviour is not verified |
| `locale_dependent`, `format_code` | as resolved |
| `visible_text`, `display_length` | stripped text; `len(text)` (padding and `?` blanks count, fill does not) |

## Rules implemented

* Sections: 1 → all numbers (negatives get an automatic `-`); 2 → `>=0 ; <0`; 3 → `>0 ; <0 ; =0`;
  4th → text. A last section containing `@` (fewer than 4 sections) is the text section and
  the others are numeric (`0.00;@`: one numeric section; `0;-0;@`: zero uses the first);
  a lone `@` shows numbers as General. Sections 2/3 never add an automatic minus.
* An empty section prints nothing (`;;;`, `0;-0;;@` for 0, `#,##0;(#,##0);` for 0); a
  section with only tags (`[Red]`, `[Blue][>100]`) shows General in that colour.
* Text: no text section → shown as typed (so `;;` hides numbers but not text); text section →
  `@` replaced by the text, literals/padding kept (`;;;` hides text; `0;0;0;"x"` shows `x`).
* Conditions `[<]`, `[<=]`, `[>]`, `[>=]`, `[=]`, `[<>]` with E-notation thresholds (`[>=1E11]`):
  first matching of the first two conditional sections, else the remaining section
  (2 conditions → 3rd section; 1 condition → 2nd); none → `#####`.
* Placeholders `0 # ?`, thousands separator, trailing-comma scaling (`#,##0,` ÷1000,
  `0.0,,"M"` ÷10⁶), `%` (×100 each; `\%` is a literal and does not scale), interleaved
  literals (`000-00-0000`), `.00` (integer part still shown), `0.#` → `5.`,
  `E+`/`E-` scientific (engineering when the integer part is `##0`), fractions (`# ?/?`,
  `# ??/??`, `?/?`, `# ?/8`, `# ??/100`), General inside a section (`"Total "General`,
  `General;(General)`), literals `"…"`, `\x`, unquoted `$ - + ( ) : / space` and other
  characters, `[$€-407]` currency tags (symbol shown), `[$-409]`/`[DBNum1]`/`[NatNum1]` ignored.
* Rounding: half away from zero on Excel's 15 significant digits (2.675 → 2.68, 1.005 → 1.01);
  a negative that rounds to zero keeps its sign under a 1-section format (`-0`). Any finite
  magnitude renders: 15 significant digits, then zeros (`1E+30` under `#,##0` →
  `1,000,000,000,000,000,000,000,000,000,000`); the decimal precision is sized to the value,
  and `%` scaling that overflows a float is done exactly.
* General: up to 10 significant digits within 11 characters; scientific with 6 significant
  digits for |v| ≥ 1E11 or |v| < 1E-4 (`1E-05`, `2.33E-10`, `1.23457E+11`).
* Dates (1900 system incl. the fictitious 1900-02-29 = serial 60 and serial 0 = 1/0/1900;
  `date1904=True` supported): `y yy yyyy`, `m mm mmm mmmm mmmmm` (minutes after `h`/before
  `s`), `d dd ddd dddd`, `h hh`, `s ss`, fractional seconds `.0`–`.000`, `AM/PM`, `A/P`,
  elapsed `[h] [mm] [ss]`; times are rounded to the displayed precision.

## Conveniences

`parse_format(code) -> NumberFormat` (cached: `sections`, `numeric`, `text_index`,
`general_numbers`, `has_conditions`, `is_date`, `verified`, `unverified_why`), `Section` (`raw tokens color condition kind
has_at locale_tags`), `mixed_date_letters(tokens) -> str | None` (see Known limits), `split_sections(code)`, `select_section(fmt, value) -> (index|None,
auto_minus, certain)`, `general_text(v)`, `serial_to_datetime(serial, date1904)`,
`color_tag_rgb(tag, palette)`, `is_date_format(code_or_id, custom)`,
`zero_display(code_or_id, custom) -> 'dash'|'blank'|'digit'|'other'|'hash'`.

## Recipes

* 66 (exact numeric zeros): `N.render(0.0, num_fmt_id, custom_formats=styles_numfmts).zero_as_dash`.
  The format engine does not know the sheet option `showZeros="0"` or conditional
  formatting that applies a number format; those are the check's business. For the sheet
  option use `zero_hidden_by_show_zeros_off(code)`: True (hidden: General, lone `@`, one or two
  numeric sections), False (an explicit zero section, printed), None (conditional format,
  unmeasured). Measured in Excel 2026-10-03 for General, single-section and three-section
  formats; two-section formats are inferred.
* 94 (populated cells only): `r = N.render(value, fid, custom_formats=..., value_type=t)`;
  `r.is_blank` means the format prints nothing; `r.color_rgb` is the format's own colour
  (compare it with the fill like a font colour; a firing conditional-format font colour beats
  it - Excel 2026-10-03); if `not r.certain` and it would change the verdict, raise
  `GradingError`. Errors (`t="e"`) are always visible and never take a colour tag (Excel
  2026-10-03).
* 73: `len(r.text)` / `r.display_length` as the one-line text length of a value.

## Known limits

* Column width is unknown here: a number too wide for its column (`#####`) and General's
  width-dependent shortening are not modelled.
* Measured in Excel by Patrick 2026-10-03 (`out/excel_session_answers.md` Q4, Q8) and now
  **certain**:
  - elapsed formats keep counting past 9999-12-31 (50000 under `[h]:mm` → `1200000:00`);
  - an empty code is General; a spaces-only code prints nothing;
  - **no automatic minus** in a section chosen by its own condition (`[<0]0;0` on −5 → `5`)
    **nor in the fallback section** after conditions (`[>0]0;0` on −5 → `5`; before the
    session the fallback section added a minus);
  - **booleans go through the text section** like the text `TRUE` / `FALSE` (`TRUE` under
    `0;-0;0;"txt"` → `txt`, under `;;;` → blank, under `0;0;0;[Blue]@` → `TRUE` in blue; no text
    section → shown as typed, uncoloured); `Rendered.kind == "bool"`;
  - **a numeric section's colour tag never colours text or booleans** (`abc` under `[Red]0` →
    the font colour; `color_rgb` None);
  - errors under a coloured format show unformatted, in the font colour;
  - a conditional-format font colour beats a number-format colour tag (applied by check 94).
* Still `certain=False` (unmeasured): a negative number in the first section when only the
  SECOND section has a condition, and `#####` when no section applies to the value.
* **Placeholders mixed with unquoted date-time letters → unverified** (finding 66-S3, in the core
  since 2026-10-04; `mixed_date_letters(section.tokens)`). openpyxl writes unit words unquoted
  (`0 bps`, `0 days`, `0 yrs`, `#,##0 d`); the engine would read the letters as date codes and
  render garbage dates, and Excel's own reading is unmeasured (its Format Cells dialog rejects such
  codes). A code is **unverified** (`NumberFormat.verified = False`, the reason in
  `NumberFormat.unverified_why`) when ONE of its sections holds both
  - a **placeholder**: a digit placeholder `0` `#` `?` - except the `0`s of a fractional-seconds
    group, a `.` directly after a seconds code `s` / `ss` / `[s]` / `[ss]` (`h:mm:ss.000`, `mm:ss.0`,
    `[ss].00`) - or `General`, or the text placeholder `@`; and
  - an **unquoted, unescaped date-time letter outside brackets**: `y` `m` `d` `h` `s` (date / time
    codes, any case), `e` `g` `b` (era / Buddhist-year codes; `E` followed by `+` or `-` is
    scientific notation, not a letter), `AM/PM` or `A/P`, or an elapsed bracket `[h]` `[m]` `[s]`.

  Under an unverified code every **number** renders `certain=False` (the rendering itself is
  unchanged), whichever section the value uses - Excel may reject the whole code (`0;0 days` on 5
  is uncertain too). Displays that do not depend on the code stay certain: an empty cell, an
  error value (always shown unformatted), and text / a logical when the code has **no text
  section** (shown as typed whatever Excel makes of the code; with a text section, `0 days;@`,
  text is uncertain too). Not affected: genuine date / time codes (no placeholder), scientific
  notation, quoted or escaped units (`0" bps"`, `0 "days"`, `0\x`, `"FY"0"E"`) and unquoted letters
  that are no date code (`0.0x` keeps rendering `x` as a literal). No corpus or toy code is
  unverified (392 distinct corpus codes + the toy codes + all built-ins checked, 2026-10-04).
  Checks: 66 and 65 make such a value `uncertain` (GradingError where it decides), 69 and 70 leave
  the cell undecided, 94 tries both blank and shown.
* `_x` padding is one space whatever x is; `*x` fill renders as nothing (min width).
* East Asian era codes (`g`, `e`) and Buddhist years (`b`) are approximated; Thai digits are
  rendered as Western digits; `[DBNum]`/`[NatNum]` are ignored.
* Weekday names for serials below 61 follow the real calendar, not Excel's 1900 calendar
  (off by one day there).

## Validation (2026-10-02)

Review fixes (huge magnitudes, subnormal General, `minus` on positive values, empty/space
codes) were checked old vs new on 255,852 renders (every toy and real custom code, all
built-ins, 927 values; `detchecks/scratch/review_fix/nf_diff.py`): the only differences are
`minus` now False for values ≥ 0 under `"FY"-0` / `-0;0`, and `certain=False` (plus
exact fractional seconds) for elapsed values past 9999-12-31.


`python -m detchecks.tests.test_numfmt`: every custom `<numFmt>` found in the styles.xml
of all 22 toy folders (40 codes) and every built-in id they use (0 1 3 4 9 10 14 16 17 40 43
44 49) is rendered for 0, ±1234.5678, 0.5, −0.004, 1E9, 45000 and text; the 15 toy codes
with a `\-`/`"-"` zero section show a dash at 0, `;;;` is blank, all others show a digit.
Toy traps covered: 66/T1 conditional dash format and 2.33E-10 residues, 66/T2 General zero,
66/T3 built-in 43/44 versus 40, 94/T3 `;;;`, 94/T5 id 8 redefinition. ~4–8 µs per render.
