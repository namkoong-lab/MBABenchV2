# detchecks reader and engine: API reference

This is the reference for writing a deterministic check. You should not need to read the code first. Everything lives in `/Users/patrick/MBABench-deterministic-checks/detchecks/`, a Python package. Run all commands from `/Users/patrick/MBABench-deterministic-checks` with `/Users/patrick/MBABenchV2/.venv/bin/python` (3.12; stdlib plus openpyxl).

## 0. Ground rules

- **No fallback.** If a check cannot grade a file, it raises `detchecks.errors.GradingError` with a clear message. Examples: an unreadable format, a value it needs that is missing or untrusted, an internal error. A check never quietly passes, fails, or defers to the LLM. The engine turns any other exception inside a check into a `GradingError` that names the check and the file.
- **Whole delivered workbook.** Every sheet counts, including tabs inherited from the starting file and hidden sheets. There is no starting-file diff.
- **Streaming only.** Check code never calls `openpyxl.load_workbook`. The engine streams each sheet once, and checks see it through hooks. Buffer candidates, not every cell; some toys have 10 million cells.
- **Validation.** Use the toys and hand-inspection of real attempts only. Never use the starting files or goldens in `corpus/tasks/`.
- **The guard.** Anything that opens more than one workbook, or any workbook over 5 MB, runs through `python3 heavy_run.py -- <python> ...` (one job at a time).

## 1. Commands

| What | Command |
|---|---|
| Grade one file (prints verdict JSON; exit 2 on GradingError) | `.venv/bin/python -m detchecks.tools.grade_one <path> [--checks 92,74] [--value-path copy.xlsx] [--recalc [--no-excel]]` |
| Toy gate (matrix, JSON in `detchecks/out/toys_<checks>.json`, exit 1 on mismatch or error; pipeline on by default, `--no-recalc` off) | `python3 heavy_run.py -- <python> -m detchecks.tools.run_toys --checks 92,74` |
| Sanity run over delivered attempts (`--touch` also streams every cell; `--recalc` runs the pipeline: LibreOffice / Excel, one at a time; `--excel-saved-only`; `--llm-check N`) | `python3 heavy_run.py --max-min 240 -- <python> -m detchecks.tools.run_corpus --checks 92,74 [--touch] [--recalc]` |
| Recalc pipeline on one file (prints the plan; `--force-excel` tests the Excel step) | `python3 heavy_run.py -- <python> -m detchecks.tools.recalc_one <path> [--no-excel] [--force-excel]` |
| Reader tests | `<python> -m detchecks.tests.test_reader` |
| Performance probe | `python3 heavy_run.py -- /usr/bin/time -l <python> -m detchecks.tools.bench <path> --full` |

In this table, `<python>` is `/Users/patrick/MBABenchV2/.venv/bin/python`.

`run_toys` has two more behaviours:
- It finds a check's folder by its number prefix (`62_*`) and grades every file in `Pass/` and `Fail/` with that check only. It skips `~$` lock files and accepts any extension.
- Its expectation is Pass → pass and Fail → fail, unless `detchecks/tools/expected.json` overrides it. An override needs a reason, and only Patrick's rulings belong there; today the four 62 Pass toys are expected to fail (strict A1).
- An entry may also carry `failing_sheets`: the exact set of sheets the check must flag. The sheets named by the verdict's mistake locations must then equal that set, otherwise the toy is a MISMATCH (a truncated mistake list is a MISMATCH too). All eight 62 toys carry one, so a 62 that fails every workbook cannot pass its gate.

## 2. Python API

```python
from detchecks.api import grade
from detchecks.core.recalc import RecalcPolicy
verdicts = grade(path, checks=[92, 74], value_path=None, task_meta=None, recalc=None)
# {"Potential Dangers/No hidden sheets": verdict, "Formatting/No merged cells": verdict}
verdicts = grade(path, checks=[22], recalc=RecalcPolicy())   # value checks: the recalc pipeline decides
```

`recalc=RecalcPolicy(...)` (2026-10-04, docs/recalc.md): when a selected check needs values and no `value_path` is given, the pipeline supplies them — an Excel-saved file's own caches; otherwise a LibreOffice copy, rerouted to a real-Excel copy when LibreOffice has gaps; `GradingError` (value checks only) when Excel is needed and not allowed / not available. `value_path` stays a manual override. Production should pass a policy; the toy gate does by default.

**Verdict contract.** It is asserted for every check:

```python
{"engine": "harness", "decision": "pass" | "fail", "summary": str,
 "mistakes": [{"location": str, "description": str, "severity": "major" | "minor"}],
 "stats": {...}}           # always includes n_mistakes, n_mistakes_listed, mistakes_truncated
```

- The result is binary: pass ⇔ `mistakes == []`.
- Mistake lists are capped at 25 entries. The total count goes in `stats["n_mistakes"]` and in the summary.
- **Live switch (2026-10-04).** Every verdict also carries `"live": bool`, from the check's class attribute `live` (default True). All checks run and record verdicts; a verdict with `"live": false` also carries `"live_note": "recorded only; the LLM verdict stands at scoring"` and must not replace the LLM's verdict. Today only Negatives in parentheses (65) is not live (Patrick: OFF until v3). `detchecks.checks.live_numbers()` lists the live checks.
- **Values provenance.** A verdict of a check with `needs_values` carries `stats["values"]` (`writer`, `source` = `cache` / `libreoffice` / `excel` / `value_path`, `value_path`, gaps, timings) whenever a value source was involved (§6, docs/recalc.md).
- Keys are `"<Category>/<check name>"`, exactly as in `rubric_9.json`.
- `location` is either `"'Sheet Name'!A1:C3"` (use `detchecks.core.refs.location(sheet, ref)`) or a bare sheet name for sheet-level findings.

**Errors.** `grade()` raises `GradingError` when any selected check fails. It never returns a partial result. The exception carries:
- `e.failures`: `{key: message}` for each failing check. When the whole file fails before any check runs (missing, unreadable), every selected check is listed.
- `e.check`: the first failing key.
- `e.verdicts`: the verdicts of the checks that did finish, for inspection only.
- `e.path`

`grade(path, checks=[])` and a check number selected twice (`checks=[74, 74]`) raise `GradingError` too.

**Non-SpreadsheetML input.** `.xls`, `.xlsb`, `.ods`, csv, corrupt zips and encrypted files raise `GradingError` for every check except those with `accepts_unparsed = True` (only check 77 should set it). So do zips that are not workbooks at all (a Word or PowerPoint package renamed `.xlsx`), a main part that is not a SpreadsheetML `<workbook>`, and a workbook that lists no sheets (Excel cannot open it). `.xlsm` and `.xltx` are SpreadsheetML and are graded normally.

## 3. Writing a check

Create `detchecks/checks/cNN.py`, subclass `Check`, and add the class to `_CLASSES` in `detchecks/checks/__init__.py`.

### 3.1 Class attributes

| attribute | default | meaning |
|---|---|---|
| `number` | 0 | rubric_9 number (1..132 in file order); `test_rubric_keys` checks it against `key` |
| `key` | "" | `"<Category>/<check name>"` |
| `needs_cells` | False | `cell()` is called for every `<c>` of each streamed sheet |
| `needs_rows` | False | `row()` is called for every `<row>` |
| `needs_values` | False | the check reads `cell.value` of formula results; provenance applies (§6). Without it, `cell.value` of a formula result raises `GradingError` |
| `sheet_kinds` | `("worksheet",)` | sheet kinds whose hooks run: `worksheet`, `chartsheet`, `dialogsheet`, `macrosheet` |
| `accepts_unparsed` | False | grade files that are not SpreadsheetML (77 only) |
| `max_mistakes` | 25 | cap on listed mistakes |
| `live` | True | False: the verdict is recorded only (`"live": false` + `live_note`); the LLM verdict stands at scoring. Only 65 today |

### 3.2 Hook order

```
start(wb)                                   wb: Package (workbook-level data ready)
for each sheet in tab order:
    wants_sheet(info) -> bool               info: SheetInfo; called before the part is opened
    sheet_start(head) -> None | False       head: SheetHead (sheetPr, views, format, cols)
                                            return False = skip this sheet's rows/cells
    row(row), cell(cell), cell(cell) ...    document order; row() precedes its cells
    sheet_end(head, tail)                   tail: SheetTail (merges, CF, DV, hyperlinks ...)
[second pass, only for sheets requested via request_second_pass()]
    second_pass_start(head); second_pass_cell(cell)...; second_pass_end(head, tail)
finish() -> verdict
```

How the engine streams each sheet:
- If no selected check wants rows or cells for a sheet, the sheet data is skipped at byte level and only the head and tail are parsed. This is fast even on 140 MB parts.
- Otherwise the sheet is streamed once for all checks together.
- `sheet_end` is called even when `sheet_start` returned False.

**Buffering pattern.** Keep per-sheet candidate cells (as `(row, col)` plus whatever you need) in `cell()`. Decide in `sheet_end()` once the tail is known: merges, conditional formats and data validations come after `<sheetData>` in the XML. Clear the buffer at `sheet_end`.

**Second pass.** Use it only when unavoidable, for example when a decision on sheet B needs cells of an earlier sheet A that you did not buffer.
- Call `self.request_second_pass("A", cells={(r, c), ...})` during the first pass or in `sheet_end`. Pass `cells=None` for all cells. The name is resolved like `wb.sheet(name)` (exact, then case-insensitive); requests for the same sheet under different spellings are merged. A name that matches no sheet raises `GradingError` for that check.
- After the first pass, the engine streams each requested sheet once more and calls `second_pass_start`, then `second_pass_cell` for the requested cells, then `second_pass_end`. `finish()` comes after that.
- Prefer buffering a small amount in the first pass. A second pass re-reads the whole sheet part.

### 3.3 Helpers on `Check`

- `self.wb` is the `Package` (set in `start`; call `super().start(wb)` if you override it).
- `self.add_mistake(location, description, severity="major")`
- `self.add_cell_mistakes(sheet, cells, description, severity="major")` groups `(row, col)` cells into rectangles and adds one mistake per rectangle. In `description`, the literal texts `{range}` and `{n}` are replaced by the rectangle and its cell count.
- `self.require_value(cell)` returns `cell.value`, or raises `GradingError` naming the check, writer and source if the value is untrusted (§6). Reading `cell.value` directly raises too; `require_value` only gives the better message.
- `self.verdict(pass_summary, fail_summary, stats)` builds and validates the verdict from `self.mistakes`. The literal text `{n}` in `fail_summary` becomes the total count. If the list was truncated, `(first 25 of N listed)` is appended. `self.stats` is merged into the stats.
- Module helpers in `detchecks.checks.base`: `Mistakes`, `cells_to_ranges(cells) -> ["A1:B2", ...]`, `make_verdict`, `validate_verdict`.
- `detchecks.core.refs`: `col_to_index`, `index_to_col`, `split_ref`, `make_ref`, `parse_range` (handles `A:A` and `3:7`), `parse_sqref`, `range_to_str`, `in_bounds`, `quote_sheet`, `location`, `group_cells`, `group_cells_str`.

### 3.4 Complete minimal example

The example flags formula cells whose font is bright red, unless a conditional format covers them or they sit in a merged block. It is for illustration only; it is not a rubric check.

```python
from detchecks.checks.base import Check
from detchecks.core.refs import in_bounds
from detchecks.errors import GradingError


class CExample(Check):
    number = 9999
    key = "Example/No red formulas"
    needs_cells = True

    def start(self, wb):
        super().start(wb)
        self.st = wb.styles
        self.n_checked = 0

    def sheet_start(self, head):
        if head.state == "veryHidden":
            return False                      # (example) no cells needed; sheet_end still runs
        self.cand = []

    def cell(self, cell):
        if not cell.has_formula:              # own formula; spill members have cell.array instead
            return
        self.n_checked += 1
        if self.st.font_color(cell.s) == "FFFF0000":
            self.cand.append((cell.row, cell.col))

    def sheet_end(self, head, tail):
        cand = getattr(self, "cand", [])
        covered = [r for cf in tail.conditional_formats for r in cf.ranges]
        merged = [(m.r1, m.c1, m.r2, m.c2) for m in tail.merges]
        bad = [(r, c) for r, c in cand
               if not any(in_bounds(r, c, b) for b in covered + merged)]
        self.add_cell_mistakes(head.name, bad, "Formula cells {range} ({n}) are red.")
        self.cand = []

    def finish(self):
        if self.n_checked == 0 and not self.wb.sheets:
            raise GradingError(f"{self.key}: workbook has no sheets")   # loud, no fallback
        return self.verdict("No red formula cells.", "{n} red formula block(s).",
                            {"formula_cells_checked": self.n_checked})
```

Register it: add `CExample` to `_CLASSES` in `detchecks/checks/__init__.py`. Then add toys and run `run_toys --checks NN`.

## 4. Workbook level: `Package` (the `wb` passed to `start`)

`from detchecks.core.package import Package`. Use `Package.open(path)`; it never raises for a bad format, only for a missing or unreadable path. A bad format shows as `is_spreadsheetml == False` with the reason in `unsupported_reason`.

### Format and package

| field | meaning |
|---|---|
| `path`, `file_name`, `extension` (lower-case, e.g. `.xlsx`), `size` | the file |
| `container` | `zip` / `ole2` / `other` / `empty` |
| `format` | `xlsx` `xlsm` `xltx` `xltm` `xlam` (SpreadsheetML); `xlsb`, `xls`, `encrypted` (OLE2 EncryptedPackage), `ods`, `csv`, `corrupt`, `unknown` (also: a zip whose main part has a non-spreadsheet content type, such as a renamed `.docx`). With no or a generic main content type, the extension decides and the main part must then be a SpreadsheetML `<workbook>` |
| `is_spreadsheetml` | True when this reader can parse the workbook and its sheets: a SpreadsheetML format, a `<workbook>` root in the Transitional or Strict main namespace, and at least one `<sheet>` |
| `unsupported_reason` | why not (None when it can) |
| `main_part`, `main_content_type` | the workbook part and its content type |
| `has_vba`, `vba_parts` | a VBA project (or XLM macro sheet) related from the workbook part, and present |
| `vba_file_present` | any `*vbaProject.bin` member, related or not |
| `ole_streams` | OLE2 directory entry names (for `.xls` / encrypted files) |
| `conformance` | `transitional` / `strict` |
| `content_types_default`, `content_types_override`, `content_type_of(part)` | `[Content_Types].xml` |
| `names` | zip member list |
| `member(part)`, `has(part)`, `read(part)`, `open_part(part)`, `xml(part)`, `part_size(part)` | case-insensitive part access |
| `rels(part)` | `[Rel(id, type, type_short, target, external, part)]`; `part` is the resolved member name. `rels("")` gives the package root |
| `rel_by_id(part, rid)`, `rels_of_type(part, *type_short)`, `reachable_parts()` | relationship lookups; `reachable_parts()` is a BFS from the root and returns `{part: {types}}` |

### workbook.xml

| field | meaning |
|---|---|
| `sheets` | `[SheetInfo(index, name, sheet_id, state, rid, part, kind, rel_type)]` in tab order. `state` is `visible` / `hidden` / `veryHidden`; `kind` is `worksheet` / `chartsheet` / `dialogsheet` / `macrosheet` (relationship type matched case-insensitively) or `unknown` (missing relationship, or a type that is none of these; `rel_type` holds the full type URI); `info.hidden` |
| `sheet(name)` | SheetInfo by name: exact match first, then case-insensitive |
| `defined_names` | `[DefinedName(name, text, local_sheet_id, scope, hidden, builtin, function, vb_procedure, xlm, comment, attrs, scope_error)]`. `text` is the raw definition (no `=`); `scope` is the sheet name or None (workbook scope); `builtin` means the name starts `_xlnm.`. **`scope` raises `GradingError`** when `localSheetId` is present but names no sheet (out of range, or not an integer); `scope_error` says why. Such a name is neither global nor local, so a check that uses names cannot guess |
| `calc_pr`, `workbook_pr`, `file_version`, `workbook_protection` | raw attribute dicts |
| `book_views` | `[dict]` of `workbookView` attributes |
| `active_tab` | `activeTab` of the first workbookView (int) |
| `date1904` | `workbookPr date1904` |
| `external_links` | see below |
| `pivot_cache_refs` | raw `(cacheId, rid)` pairs; resolved list in `pivot_caches` (below) |

Each `ExternalLink` has these fields:
- `index`: the `n` in `[n]Sheet!A1` formula prefixes, 1-based in workbook order.
- `rid`, `part`
- `kind`: `externalBook` / `ddeLink` / `oleLink`
- `target`: path or URL from the part's relationship
- `target_external`
- `sheet_names`: the cached sheet names
- `defined_names`
- `has_cached_data`

### Document properties

- `app` is `AppProps(application, app_version, company, raw)` from `docProps/app.xml`.
- `core` is `CoreProps(creator, last_modified_by, created, modified, title, raw)`.

### Lazy side parts

All are parsed on first access.

| accessor | returns |
|---|---|
| `styles` | `Styles` (§5) |
| `shared_strings` | list of plain text |
| `shared_string_runs` | `{index: [Run]}` for rich strings |
| `charts` | `[Chart(sheet, part, formulas)]`; `formulas` is every `<*:f>` text in the chart part (series, categories, titles, labels), reached via sheet → drawing → chart, chartsheets included |
| `drawings` | `[Drawing(sheet, part, chart_parts, textlinks)]`; `textlinks` are shape `textlink="..."` formulas |
| `pivot_caches` | `[PivotCache(cache_id, part, source_type, source_ref, source_sheet, source_name, source_target)]` |
| `tables(info)` | `[Table(sheet, part, name, display_name, ref, header_row_count, totals_row_count, columns)]`; each column is `{name, calculated, totals_formula, totals_function}` |
| `comments(info)` | `[Comment(ref, author, text, threaded)]` (legacy notes and threaded comments) |
| `sheet_rels(info)` | relationships of a sheet part |

### Other attributes

- `provenance`: see §6.
- `task_meta`: the dict passed to `grade()`.

## 5. Styles and colours: `wb.styles`

Lists are indexed exactly as in `styles.xml`. A lookup with an out-of-range index returns index 0 or the default, as Excel tolerates.

### Raw lists and dataclasses

| field | content |
|---|---|
| `num_fmts` | `{id: formatCode}` (custom formats only) |
| `fonts` | `[Font]`: `name`, `sz`, `b`, `i`, `u`, `strike`, `color` (`Color` or None), `vert_align`, `family`, `scheme`, `specified` |
| `fills` | `[Fill]`: `kind` (`none` / `pattern` / `gradient`), `pattern`, `fg`, `bg`, `gradient_type`, `stops` (`((position, Color), ...)`) |
| `borders` | `[Border]`: `left` / `right` / `top` / `bottom` / `diagonal`, each `BorderEdge(style, color)` |
| `cell_xfs`, `cell_style_xfs` | `[Xf]`: `num_fmt_id`, `font_id`, `fill_id`, `border_id`, `xf_id`, `alignment`, `locked`, `hidden`, `quote_prefix`, `apply` |
| `cell_styles` | `[CellStyle(name, xf_id, builtin_id)]` |
| `dxfs` | `[Dxf(font, fill, num_fmt_id, num_fmt_code, border)]`; a dxf font is partial (see `Font.specified`) |
| `indexed_palette` | 64 RRGGBB entries, with any custom `<indexedColors>` applied (`custom_indexed`); an invalid custom entry holds a placeholder and is listed in `unknown_indexed` (`{index: rgb as stored}`) |
| `theme_colors` | 12 RRGGBB entries in cell theme-index order (`theme_present`) |

`Alignment` has `horizontal`, `vertical`, `wrap_text`, `shrink_to_fit`, `indent` and `text_rotation`. Center Across Selection is `horizontal == "centerContinuous"`.

### Per style index `s`

`s` is `cell.s`, `row.style` or `head.col_style(c)`.

| method | returns |
|---|---|
| `xf(s)`, `font(s)`, `fill(s)`, `border(s)` | the raw objects |
| `num_fmt_id(s)` | the number-format id |
| `num_fmt_code(s)` | the custom code, else the en-US built-in table, else `General`. Built-in 44 is correct here. `detchecks.core.numfmt` is the authority for rendering |
| `style_name(s)` | cell style name (`Normal`, `Note`, ...) or None |
| `font_color(s)` | `'FFRRGGBB'`: the rendered font colour (before CF and number-format colours), or an `UnknownColour` |
| `fill_color(s)` | `'FFRRGGBB'`, None (no fill), or an `UnknownColour` |
| `cell_fill(s)` | `FillPaint(kind, pattern, fg, bg, effective, stops)` (`effective` / `stops` may hold an `UnknownColour`) |

### Colour resolution

`styles.resolve(color, role)` returns `'FFRRGGBB'`, None (fill role, no colour element), or an **`UnknownColour`** (below). `role` is `font`, `fill` or `border`. The alpha byte is always FF, because Excel ignores the stored alpha.

| stored | resolves to |
|---|---|
| `rgb` (6 or 8 hex digits) | that colour (alpha dropped: openpyxl's `00FF0000` is red) |
| `indexed` 0..63 | the workbook's `<indexedColors>` if present, else the legacy palette. **indexed 13 = FFFF00 (bright yellow)** |
| `indexed` 64 | system foreground: black |
| `indexed` 65 | system background: white |
| `theme` 0..11 | theme part slot. Index order is 0 = lt1, 1 = dk1, 2 = lt2, 3 = dk2, 4..9 = accent1..6, 10 = hlink, 11 = folHlink (as `judge/utils/theme_palette.py`). **Theme 0 is white and theme 1 is black** in the Office theme |
| `auto` or no `<color>` | font / border: black; fill: white for auto (window background), None for no colour element. Exception: a pattern fill's auto `fgColor` is Excel's "Automatic" pattern colour, black |
| `tint` in [-1, 1] | applied with **Excel's integer HLS arithmetic** (`excel_tint`, HLSMAX = 240) |
| anything else | an **`UnknownColour(ref, why)`**: an rgb that is not 6 or 8 hex digits (`FFGGFF00`, `""`, `0FF0000`), a theme index ≥ 12 or not a number, an indexed value outside the palette other than 64 / 65 (e.g. 81, the system tooltip-text colour Excel writes for comment fonts) or not a number, an invalid entry of a custom `<indexedColors>`, a tint that is not a number in [-1, 1] |

**Unresolvable colours (finding 47-R2-08, 2026-10-04).** An `UnknownColour` is never None, black,
white or "no fill" - until 2026-10-04 `resolve` returned None for these and the checks read it as
"no fill" / automatic, so 47 and 94 passed such files silently (and an invalid custom palette
entry read as black, a 7-digit rgb as its last six digits). It is not a string: code that slices
it like `'FFRRGGBB'` fails loudly. It is hashable (`ref` = `Color.describe()`, `why`), and equal
stored references compare equal. `styles.is_unknown(x)` tests for one; `styles.indexed_colour(i)`
gives a palette entry as a `[ColorN]` tag uses it (an `UnknownColour` for an invalid custom entry).
Building the style table never raises for one. **A check that meets one decides whether its
verdict depends on the colour and raises `GradingError` only then** - never for a style no cell
uses: 49 / 50 / 51 raise where the cell's class makes its colour decide (`colour_rules`), 47 grades
every reading of an unknown fill (no fill / bright yellow / another colour) and raises when they
disagree, 94 makes the cell undecided (raised only when nothing else is certainly concealed). In
`fill_paint` an unknown colour the paint does not use (a solid cell fill's bgColor, a dxf solid
fill's fgColor) changes nothing; one it uses makes `effective` that `UnknownColour` (a gradient:
the unknown stop, kept in `stops`).

The tint algorithm reproduces Excel's rendered colour exactly on 46 Office-theme swatches. For example, theme 1 + tint 0.49998 → `808080`, and accent4 + 0.8 → `FFF2CC`. The float version in `theme_palette.py` is off by one in about half of those cases (it gives `7F7F7F` for the first).

### Fills

- A solid cell fill paints `fgColor`. A solid fill without `fgColor` paints black.
- **dxf fills (conditional formats): Excel paints `bgColor` ONLY** (measured by Patrick in Excel 2026-10-03, `out/excel_session_answers.md` Q2). `fgColor` is ignored; `bgColor` indexed 64 and rgb `00000000` paint black (alpha ignored); no `bgColor` → no fill (`FillPaint.kind == "none"`, `dxf_fill_color` None). Use `styles.dxf_fill_color(dxf_id)`, or `styles.fill_paint(dxf.fill, dxf=True)`. An `auto="1"` bgColor is read as black like indexed 64 (not measured on its own; none in the corpus).
- A cell `patternFill` without `patternType` paints nothing. In a dxf it means solid.
- For hatch patterns, `effective` is fg blended over bg by pattern density.
- For gradients, `effective` is the average of the stops.

### Conditional formats

- `styles.dxf_font_color(dxf_id)` returns the font colour a dxf sets, or None.
- x14 rules carry their dxf inline: `rule.dxf`, a `Dxf`.

### Empty positions

A position with no `<c>` element takes its style from the row style (`row.style`, i.e. `s` when `customFormat=1`). Without one, it takes the column style (`head.col_style(col)`), else 0. Use `Styles.effective_style(cell_s_or_None, row_style, col_style)`. This is how a check sees "the whole row or column filled yellow" when no cells exist there.

## 6. Values and provenance

`wb.provenance` is a `Provenance` with these fields:
- `writer`: `excel` / `xlsxwriter` / `openpyxl` / `libreoffice` / `unknown`
- `evidence`
- `formula_caches_trusted`
- `full_calc_on_load`
- `application`, `app_version`
- `value_path`, `value_writer`, `value_sheets`
- `as_dict()`

### How the writer is detected (`values.detect_writer`)

| writer | signature |
|---|---|
| openpyxl | `Application` contains "openpyxl" (`Microsoft Excel Compatible / Openpyxl 3.1.5`), or `dc:creator` is openpyxl with calcId 124519 |
| libreoffice | `Application` starts "LibreOffice", or `<fileVersion appName="Calc">` |
| xlsxwriter | `Application` "Microsoft Excel" with fileVersion `rupBuild="4505"`, or AppVersion 12.0000 + calcId 124519 + fullCalcOnLoad |
| excel | `Microsoft Excel` / `Microsoft Excel Online` / `Microsoft Macintosh Excel`, **and** evidence that Excel wrote the parts: `AppVersion` present, `<fileVersion appName="xl">`, and Excel's double-quoted XML declaration on the workbook part, `app.xml` and every sheet part |
| unknown | everything else: ChatGPT's spreadsheet writer (no docProps, `x:`-prefixed XML, caches present), Google, .NET libraries, and Excel-labelled files that fail the evidence above |

Why the evidence: agents' Python tools keep the starting file's `app.xml` and `fileVersion` but re-serialise the parts they touch (single-quoted or no XML declaration). Of 303 GPT-6 files, 9 carried an Excel `app.xml`; 8 of them were rewritten by Python (6 throughout, for example `gpt6/chatgpt_gpt_6_pro/2290.xlsx`, creator "GPT-6 Astra Pro", no `AppVersion`; in 2 only an added sheet). They are `unknown` now, so their caches still count as trusted (as for any unknown writer) but are not taken for Excel's own. All 179 Excel-saved toys, the 6 Excel-saved corpus attempts and the one remaining GPT-6 file keep `excel`.

### Per cell

| property | meaning |
|---|---|
| `cell.value` | decoded value (below). For a **formula result** it raises `GradingError` when the value is untrusted, or when no check on this stream declared `needs_values = True` |
| `cell.value_source` | `constant` (no formula, not in an array range), `cached` (the file's own `<v>`), or `recalc` (from `value_path`) |
| `cell.value_trusted` | whether the value can be relied on |
| `cell.cached_value` | the file's own stored value, regardless of `value_path` and trust |
| `cell.unverified_value` | the value from its source (recalc copy or cache) with no trust or `needs_values` check. Explicit opt-in for diagnostics, such as quoting a placeholder in a message; never decide on it |

Trust rules:
- Constants are always trusted.
- A formula's cached value is **untrusted** when there is no `<v>` (or an empty `<v>` on a non-string formula).
- It is also untrusted when the writer is openpyxl or XlsxWriter (all-zero placeholder caches), and when a LibreOffice-written cache is `#NAME?` or `#VALUE!` (counted as unmeasured). openpyxl itself never writes a cache, but 12 of the 26 openpyxl-labelled corpus attempts carry full caches injected by agent tooling; they stay untrusted (**ruled 2026-10-03: agent-tool caches are never trusted**), so value checks raise on them unless a `value_path` copy is given.
- Otherwise it is trusted. That covers excel, libreoffice, and unknown writers with a cache.

### `value_path` (a LibreOffice recalculation copy, same sheet names)

**Values policy (Patrick's ruling 2026-10-03, extended 2026-10-04).** Production supplies a recalculation copy for **every file not saved by Excel** (openpyxl-labelled, XlsxWriter, LibreOffice, uncached, unknown writer); the recalc pipeline (`core/recalc.py`, docs/recalc.md) does this when `grade(..., recalc=RecalcPolicy())` is used. Value-reading checks (No formula errors (22), Negatives in parentheses (65), Zeros as dashes (66), Reasonable row heights (73), No white-on-white hiding (94)) then take formula values from the copy; structure and styles still come from the delivered file. Agent-tool caches are never trusted. A LibreOffice `#NAME?` / `#VALUE!` in a manually supplied `value_path` stays a loud error; in a pipeline copy it is trusted once the gap scan has shown it is genuine (`ValueSource(errors_vetted=True)`), and a file with LibreOffice gaps is rerouted to a real-Excel copy (or fails loudly when Excel is not allowed / available). Regression tests: `test_values_policy_ruling_2026_10_03` (test_reader), `test_recalc`.

- Formula cells and array members take their value from the copy (`value_source == "recalc"`).
- The copy's sheet is streamed in lockstep with the delivered one, as a merge-join on `(row, col)`. Only values are read from it, never styles or structure: the copy invents merges, rewrites zoom and selection, and writes explicit row heights.
- A copy value of `#NAME?` or `#VALUE!` (unless the copy was opened with `errors_vetted=True` by the pipeline), a missing cell, or a missing sheet makes the value untrusted.
- The copy is opened only if a selected check has `needs_values = True`.
- `wb.provenance.value_kind` (`cache` / `libreoffice` / `excel`) and `wb.provenance.recalc` (gaps, timings) describe what the pipeline did.

### Rules for checks

Set `needs_values = True`, and then do one of two things:
- check `cell.value_trusted` before using `cell.value`;
- or call `self.require_value(cell)`, which raises `GradingError` with the writer and source.

The reader enforces this (no fallback): `cell.value` of an untrusted formula result raises, and so does `cell.value` of any formula result when the stream was not opened for a check with `needs_values` (a forgotten flag would otherwise read caches while a `value_path` copy was supplied). Constants are always readable. The engine runs each check alone in the toy gate, so a missing flag shows up there. To fail fast for a whole workbook, test `wb.provenance.formula_caches_trusted` and `wb.provenance.value_path` in `start()`, and raise.

### Decoded types

| `t` | value |
|---|---|
| `n` | `float`; an empty `<v>` is None |
| `s` | shared-string text |
| `str` / `inlineStr` | `str` |
| `b` | `bool` |
| `e` | `ExcelError` (a `str` subclass: `isinstance(v, ExcelError)` separates `#N/A` errors from text) |
| `d` | the ISO-8601 text as stored |

Number formats are not applied. Dates stay serial floats; `wb.date1904` tells you the epoch.

## 7. Sheet level

### `SheetHead`

Valid from `sheet_start` on. Fields come from everything before `<sheetData>`.

| field | content |
|---|---|
| `info` | SheetInfo; also `name`, `state`, `kind`, `index` |
| `sheet_pr` | `SheetPr(tab_color: Color, code_name, filter_mode, summary_below, summary_right, apply_styles, show_outline_symbols, fit_to_page, attrs)` |
| `dimension` | `<dimension ref>` |
| `views` | `[SheetView]` |
| `view` | the view of workbook window 0 (else the first; None if none) |
| `format` | `SheetFormat(default_row_height, custom_height, zero_height, default_col_width, base_col_width, outline_level_row, outline_level_col, thick_top, thick_bottom)`. `zero_height` means rows are hidden unless a `<row>` says otherwise |
| `cols` | `[ColInfo(min, max, width, hidden, custom_width, best_fit, outline_level, collapsed, style)]` sorted by `min` (file order among equal mins) |
| `col_info(c)` | the `<col>` entry that applies to column `c`, or None (sheet defaults). **Overlapping entries: the later one wins** (below) |
| `col_style(c)` | that entry's `style`, or None |
| `col_segments()` | the disjoint, sorted `(lo, hi, ColInfo)` runs the entries cover, the later entry winning where they overlap (`core.sheet.paint_cols`) |
| `col_run(c)` | `(lo, hi, ColInfo or None)`: the maximal run of columns around `c` sharing one applying entry (None = defaults), for walking a range run by run |
| `has_sheet_data` | False for chartsheets and parts without `<sheetData>` |

**Overlapping `<col>` entries** (invalid, but written by GPT-6 tooling in 187 of 374 corpus files,
e.g. `F:G 9.0` followed by `F:F 44.0`, or `C:XFD 18` followed by `D:D 3`): one rule for every
check (2026-10-04) - **the later entry wins** on the columns it covers, as a whole (width, hidden,
style, ...; an entry without a width still wins and means the default width), "later" in the
reader's order of `cols` (by `min`, then file order). It is the rule Reasonable column widths (70)
and Reasonable row heights (73) had decided; no Excel measurement or toy settles what Excel itself
does (no toy has overlapping entries), and on every corpus file "later in file order" gives the same
widths. Before 2026-10-04 `col_info` returned None - the sheet default - for every column an earlier
range covers after a later entry that starts inside it (`C:XFD 18` + `D:D 3`: F onwards read 8.43);
that hit 130 corpus files, through Sufficient column widths (69) and Reasonable row heights (73).
Checks that read `cols` directly (47 column styles, 65 / 66 missing array members, 93 hidden
columns) apply their own reading.

`SheetView` fields:
- Window and zoom: `workbook_view_id`, `tab_selected`, `zoom_scale` (as stored; None means absent), `zoom_scale_normal`, `zoom_scale_page_layout_view`, `zoom_scale_sheet_layout_view`, `view` (`normal` / `pageBreakPreview` / `pageLayout`), `top_left_cell`
- Display flags: `show_grid_lines`, `show_zeros`, `show_formulas`, `show_row_col_headers`, `show_outline_symbols`, `right_to_left`
- Panes and selections: `pane: Pane(x_split, y_split, top_left_cell, active_pane, state)` and `selections: [Selection(pane, active_cell, active_cell_id, sqref)]` in file order
- Raw: `attrs`

`SheetView` helpers:
- `zoom`: `zoomScale` or 100.
- `active_pane`: `topLeft` when there is no pane.
- **`active_selection`**: the selection of the **active pane**, not simply the first `<selection>` (62 toys T2/T3 trap).
- `active_cell`: that selection's `activeCell`, or `A1` when the active pane has no selection.

### `Row`

Delivered to `row()` before its cells. Fields: `r`, `ht`, `custom_height`, `hidden`, `outline_level`, `collapsed`, `s`, `custom_format`, `spans`, `thick_top`, `thick_bot`, `attrs`. `style` is `s` if `customFormat=1`, else None.

Rows without an `r` attribute get the previous row + 1. Rows absent from the XML are at the defaults: `head.format.default_row_height`, or hidden when `head.format.zero_height`.

### `Cell`

One per `<c>` element, including styled empty cells. Positions are inferred when `r` is omitted.

| field / property | meaning |
|---|---|
| `ref`, `row`, `col` | `'B7'` (no `$`), 7, 2 |
| `t` | type as stored (`n` when absent) |
| `s` | style index (0 when absent) |
| `raw` | `<v>` text (`""` for an empty `<v/>`); concatenated `<is>` text for inline strings; None without a value element |
| `formula` | `Formula` or None |
| `array` | an `ArrayRange` when the cell is a **non-anchor member** of an array, dynamic-array spill or data-table range: `r1`, `c1`, `r2`, `c2`, `anchor_ref`, `formula` (the anchor's), `ref` |
| `cm`, `vm` | cell and value metadata indexes. `cm` marks dynamic arrays; `vm` marks rich values such as cached `#SPILL!`, which is stored as `#VALUE!` with `vm` |
| `has_formula` | the cell has its own formula: normal, shared master or child, array or dataTable anchor. False for spill members and empty `<f/>` markers |
| `is_formula_result` | own formula, an empty `<f/>`, or array membership |
| `formula_text` | the formula that applies to this cell, expanded for shared children; None without one |
| `has_value` | a value element is present and non-empty; an empty-string `t="str"` result counts |
| `is_blank` | no formula, no value, not an array member |
| `value`, `value_trusted`, `value_source`, `cached_value` | §6 |
| `rich_runs` | `[Run(text, font)]` for rich text (inline or shared), else None. `font` is a partial `Font` or None (inherits the cell font). Phonetic runs are dropped |
| `sheet` | sheet name |

`Formula` fields:
- `text`: as stored, without `=`. It is None for shared children, for `<f ca="1"/>` spill markers, and for dataTable anchors (their parameters are in `attrs`: `r1`, `r2`, `dt2D`, `dtr`, ...).
- `kind`: `normal` / `shared` / `array` / `dataTable`.
- `ref`, `si`, `ca`, `aca`, `attrs`.

`Formula` properties:
- `is_shared_master`, `is_shared_child`, `is_empty_marker`.
- `master_ref`
- **`expanded`**: the stored text, or for a shared child the master's text shifted by the offset with `detchecks.core.formula.shift` (the code behind `formula.translate`, so `formula_text` and `formula.translate` always agree). It shifts the left end of `B5:INDEX(...)` ranges and turns references pushed off the grid into `#REF!`, which openpyxl's `Translator` gets wrong. It raises `GradingError` when the group has no master before it, and `FormulaError` (a `GradingError`) when the master text cannot be lexed.

Excel stores a formula only once (master or anchor). Array and spill members carry no `<f>`: use `cell.array.formula`. Shared children carry no text: use `formula_text`.

### `SheetTail`

Valid in `sheet_end`. Fields come from everything after `</sheetData>`.

| field | content |
|---|---|
| `merges` | `[Merge(ref, r1, c1, r2, c2)]`, `$` removed; `is_single_cell`. A `mergeCell` ref that is unreadable or off the grid (`A1:B2:C3`, `A0:B1`, `XFE1`) raises `GradingError` instead of being dropped |
| `conditional_formats` | `[ConditionalFormat(sqref, ranges, rules, source, pivot)]`. `source` is `main` or `x14` (from `extLst`, sqref in `<xm:sqref>`) |
| `data_validations` | `[DataValidation(sqref, ranges, type, operator, formula1, formula2, allow_blank, show_error_message, show_input_message, error_style, source, attrs)]`. x14 DVs are included, with their `<xm:f>` formulas |
| `hyperlinks` | `[Hyperlink(ref, rid, target, external, location, display, tooltip)]`. `target` is resolved through the sheet's rels; `location` is the in-workbook target |
| `ignored_errors` | `[IgnoredError(sqref, ranges, flags)]`, e.g. `{"numberStoredAsText": True}` |
| `protection`, `is_protected` | `sheetProtection` attributes |
| `auto_filter` | `{ref, filter_columns}` |
| `drawing_rid`, `legacy_drawing_rid` (VML: notes or form controls), `legacy_drawing_hf_rid`, `picture_rid` | relationship ids |
| `table_part_rids` | relationship ids |
| `has_comments`, `has_threaded_comments` | from the sheet's rels; text via `wb.comments(info)` |
| `has_ole_objects`, `has_controls` | flags |
| `header_footer` | `{"oddHeader": text, ...}` |
| `page_setup` | raw attributes of `pageSetup`, `pageMargins`, `printOptions` |
| `sparkline_groups` | `[SparklineGroup(type, date_range, sparklines, attrs)]` from `extLst` (x14); each sparkline is `Sparkline(sqref, formula)` (host cell, data range as stored). `group.formulas` lists every formula text of the group. Sparkline formulas reference cells, names and other workbooks like any formula (95, 87, 29) |
| `other` | local names of elements not interpreted; uninterpreted `extLst` content as `extLst/<name>` (e.g. `extLst/slicerList`) |

`CfRule` fields:
- Core: `type`, `priority`, `dxf_id` (main rules; resolve it via `wb.styles.dxf(...)`), `dxf` (inline x14 dxf), `operator`, `formulas` (`<formula>` or `<xm:f>` texts), `stop_if_true`, `text`, `time_period`, `rank`, `percent`, `bottom`, `above_average`, `equal_average`, `std_dev`
- Visuals: `colors` (colorScale / dataBar `Color`s), `cfvos` (`[(type, val)]`), `show_value` (dataBar / iconSet; False means the value is hidden), `icon_set`
- Link and raw: `id` (an x14 rule id, or a main rule's link to its x14 twin), `attrs`

All formula texts (CF, DV, defined names, charts) are kept **exactly as stored**: no leading `=` (some writers do store one), with `_xlfn.` prefixes intact. Tokenising is `detchecks.core.formula`'s job, which is built by a parallel agent; see its own docs.

### Matching and streaming details

- Elements and attributes are matched by **local name** everywhere. Prefixed parts (`<x:c>`, 51 of 60 sampled ChatGPT-GUI files) and Strict OOXML (`purl.oclc.org` namespaces) read exactly like Transitional ones.
- Chartsheets have a head (views, tab colour) and a tail (drawing) but no cells. Opt in with `sheet_kinds`.
- A sheet whose relationship is broken (`part is None`) or of an unrecognised type (`kind == "unknown"`) is still handed to every check that streams sheets. Reading it raises `GradingError`, so it is never silently skipped.
- The root element is found after the prolog (XML declaration, processing instructions, comments, DOCTYPE), so a `<tool>` inside a leading comment is not taken for the root.
- An array or data-table formula whose `ref` is unreadable or off the grid raises `GradingError`.

## 8. Lower-level reading (tests and debugging)

```python
from detchecks.core.package import Package
from detchecks.core.sheet import SheetStream, load_sheet
from detchecks.core.values import detect_provenance, make_context

pkg = Package.open(path); detect_provenance(pkg)
head, rows, cells, tail = load_sheet(pkg, "Sheet1")     # SMALL sheets only; trusted formula values readable
with SheetStream(pkg, pkg.sheet("Big")) as ss:          # streaming
    head = ss.read_head()
    tail = ss.skip_body()                               # or ss.read_body(on_row, on_cell,
                                                        #      make_context(pkg, info, None, needs_values=False))
```

## 9. Performance

Measured 2026-10-02 through `heavy_run`, with `/usr/bin/time -l`:

| workbook | registered checks (92, 74; skeleton mode) | full stream touching every cell (`bench --full`) | peak RSS |
|---|---|---|---|
| 50 T2 TAM (31 MB zip, 240 MB XML, 35 sheets, 10.0M `<c>`) | 0.2 s | 16.7 s | 85 MB |
| 87 T6 TwoNOne (27 MB zip, 2 × 140 MB sheet parts, 2.16M cells, 1.37M shared children expanded) | 0.2 s | 14.8 s (11.2 s with openpyxl's Translator, which mis-expands some formulas) | 96 MB |
| all 71 delivered corpus attempts, one process (`run_corpus --touch`) | 1.1 s total | 30 s total | 82 MB |

Memory stays flat because rows and cells are cleared as they are streamed. Your buffers are the main memory risk: keep candidates, not cells.

Per-cell Python work dominates, so keep `cell()` cheap:
- Look up `font_color` and `fill_color` by `cell.s`; they are cached per style index.
- Touch `formula_text` only when needed; expanding a shared child costs about 10 µs.

## 10. Known limits

- **Formats.** `.xlsb`, `.xls`, `.ods`, csv and encrypted workbooks are classified but not parsed. Every check except 77 raises `GradingError` on them.
- **Rendering.** No number-format rendering here; use `detchecks.core.numfmt`. No CF evaluation: rules are exposed, and the check decides what they paint. No font metrics.
- **Theme.** Only the scheme colours of the theme part are read; theme fonts are not.
- **Tint.** The tint algorithm is verified on Office-theme swatches. Colours that are not theme colours but carry a tint use the same algorithm.
- **Data tables.** `<f t="dataTable">` anchors have no formula text (`text` is None); see `formula.attrs`.
- **Values.** `t="d"` cell values are returned as their ISO text. Dates stored as numbers are serial floats; formats are not applied.
- **Second pass.** It re-reads the whole sheet part, so it is O(sheet size) again.
- **Not read.** VML form-control formulas (`x:FmlaLink`), ActiveX, slicers (listed in `tail.other` as `extLst/slicerList`), query tables, Power Query, threaded-comment authors (person ids are returned instead), and custom sheet views (`customSheetViews` are ignored: they are not the live view).
- **Shared-formula expansion** uses `formula.shift`. It differs from openpyxl's `Translator` only where the Translator is wrong (`B5:INDEX(` ranges, off-grid references); no difference on 11.4M shared children of 294 real files.
- **UTF-16 sheet parts** are transcoded in memory. Rare, but such a sheet is not streamed.
- **Writer detection** relies on the signatures in §6. A tool that copies Excel's `app.xml` AND writes Excel-style double-quoted declarations on every part would still pass as `excel`; `provenance.evidence` says what was seen.
