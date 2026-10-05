# detchecks.core.formula — formula parsing for check authors

Pure functions over formula **text as stored in the file** (cell `<f>`, `<definedName>`,
CF `<formula>`, DV `<formula1>` / x14 `<xm:f>`, chart `<c:f>`), without the leading `=`
(one leading `=` is tolerated). Nothing here opens a file or evaluates anything.
Stdlib + `openpyxl.formula.translate.Translator` (row/column arithmetic only).

Used by checks 29, 49, 50, 51, 80, 87, 95.

```python
from detchecks.core import formula as F

f = F.parse("SUM('My Sheet'!A:A)+Rate*_xlfn.XLOOKUP(A1,T[Key],T[Val])")
[c.name for c in f.functions]          # ['SUM', 'XLOOKUP']
[(o.kind, o.raw) for o in f.operands]  # [('whole_column', "'My Sheet'!A:A"), ('name', 'Rate'),
                                       #  ('cell', 'A1'), ('structured', 'T[Key]'), ('structured', 'T[Val]')]
```

## Errors — no fallback

* `FormulaError` (a `detchecks.errors.GradingError`) is raised for text Excel could not have
  stored: unbalanced `(` `)` `{` `}` `[` `]`, unterminated `"…"` or `'…'`, unknown `#ERR`,
  an operand that is neither a reference nor a valid name (`A1|B1`, `Sheet1!`), `;` outside
  an array, two operands with no operator between them (`#REF!A1`, `"a"B1`, `(A1)B1` —
  toy 22/T4: Excel rejects `#REF!A1`; `)(` of an invoked `LAMBDA(…)(…)` is fine), a row
  number that is not paired with another row (`A:1`, `A1:1`), a defined name whose
  definition cannot be parsed (during `expand`).
* Also raised for inputs the module will not guess about: a structured reference `Tbl[…]`
  when `other_sheets`/`references_other_sheet` get no `table_sheets=` (or the table is not
  in it), and `NameTable` entries it cannot read (see Names).
* Real attempts do contain text Excel could not store (agent-written `SUM(C16:C22` with no
  `)`, `INDEX(B20#,1,2))`): such a workbook raises in every check that parses all formulas.
  Whether that should instead be graded as a defect is an open ruling for Patrick.
* `AmbiguousReferenceError` (a `FormulaError`): `Other.xlsx!Rate` may be another workbook's
  name or a local sheet called "Other.xlsx". Pass `sheet_names=` (helpers) or build the
  `NameTable` with `sheet_names=` and it is decided; without them the helper raises.

Never catch these to "skip" a formula; let them surface (Patrick's NO FALLBACK ruling).

## parse(text) -> Formula   (lru-cached, 4,096 entries ≈ 150 MB worst case for long LET formulas; results are shared — do not mutate)

| attribute | meaning |
|---|---|
| `text`, `body` | input; body = text without a leading `=` (offsets below index into `body`) |
| `functions` | tuple of `FunctionCall`, in source order |
| `operands` | tuple of `Operand`, in source order |
| `strings` | unescaped string-literal values (incl. those inside array constants) |
| `params` | lower-case LET/LAMBDA parameter names declared in the formula |
| `tokens` | lexer output (re-lexed on demand): `(kind, text, start, end)`; kinds `ws str num err operand func ( ) { } , ; op spill` |

Methods:

| method | returns |
|---|---|
| `function_names()` | set of normalised names of non-local calls (`{'SUM','OFFSET'}`) |
| `calls(*names)` | `FunctionCall`s with those names |
| `arg_text(call, i)` / `operands_in_arg(call, i)` | text / operands of argument i (call = object or index) |
| `references()` | operands whose kind is a reference kind (below) |
| `names_referenced()` | `NameRef`s (syntactic, see Names) |
| `ref_errors()` | operands carrying `#REF!` (`#REF!`, `Sheet!#REF!`, `A1:#REF!`) |
| `whole_column_or_row_refs(include_sheet_edge=False)` | operands of kind `whole_column`/`whole_row`; with the flag also bounded ranges touching row 1048576 / column XFD |
| `other_sheets(own_sheet, sheet_names=None, table_sheets=None)` | set of sheet names (as written, unquoted) referenced other than `own_sheet` (case-insensitive); a table reference `Tbl[…]` is placed with `table_sheets` (`{table name: sheet}`), and raises `FormulaError` without it; `[@Col]` is the own sheet |
| `references_other_sheet(own_sheet, sheet_names=None, table_sheets=None)` | `bool(other_sheets(...))` |
| `references_external_workbook(sheet_names=None)` / `external_references(...)` | another workbook is referenced / which operands |
| `pure_reference(transparent=("ANCHORARRAY","SINGLE"))` | the single reference/name operand when the whole formula is just that operand (optionally in parentheses, unary `+`/`-`, `@`, `#`, or the listed one-argument wrappers), else `None` |

### FunctionCall

`name` (upper case, prefixes `_xlfn.` `_xlws.` `_xll.` `_xludf.` `_xleta.` `_xlpm.` stripped:
`_xlfn._xlws.SORT` → `SORT`, `_xlfn._TRO_TRAILING` → `_TRO_TRAILING`), `raw`, `prefix`,
`start`, `end`, `depth` (0 = outermost), `parent` (index into `functions`), `parent_arg`,
`arg_spans` (offsets per argument; `TODAY()` has 0 args, `ROUNDDOWN(x,)` has 2), `n_args`,
`qualifier` (`Sheet1!MyFn(` → `'Sheet1'`),
`builtin` (Excel function: prefixed `_xlfn.`/`_xlws.`, eta, or an unprefixed name in
`BUILTIN_FUNCTIONS`; `_xludf.`/`_xll.` and unknown names are not),
`local` (call of a LET/LAMBDA parameter: `_xlpm.f(…)`, or a plain `f(…)` inside the scope of a
plain declaration `LET(f,…)`; see the scope rules under Names),
`eta` (`_xleta.SUM` passed as a value, e.g. `BYROW(r,_xleta.SUM)` — counted as a call of SUM),
`implicit` (`@FUNC(…)`), `spilled` (`FUNC(…)#`).

### Operand

| field | meaning |
|---|---|
| `kind` | `cell` `range` `whole_column` `whole_row` `trimmed_range` `spill` `structured` `name` `param` `error` `number` `string` `bool` `array` |
| `raw`, `start`, `end` | text as written (the `#` of a spill is not part of `raw`) |
| `sheet`, `sheet_end` | unquoted sheet name (`'It''s'` → `It's`); `sheet_end` only for 3-D (`Jan:Dec!A1`); `None` = no qualifier |
| `external` | `None` = this workbook; else the book index (`'1'` from `[1]…`) or file/path (`'Book.xlsx'`, `'C:\dir\Book.xlsx'`, URL) |
| `workbook_scoped` | `[0]!Name` (this workbook, global name) |
| `ambiguous_book` | name/table body behind a qualifier that looks like a file (`Other.xlsx!Rate`) |
| `qualifier`, `body` | raw text before / after the first `!` |
| `name` | kind `name`/`param`: the identifier (param without `_xlpm.`) |
| `table` | kind `structured`: table name (`None` for `[@Col]`) |
| `shape`, `bounds` | geometry `cell/range/column/row/sheet`, `(r1, c1, r2, c2)` 1-based (whole column → rows 1..1048576) |
| `trim` | `trailing` (`A:.A` / `_TRO_TRAILING`), `leading`, `all`, `trimrange` (`TRIMRANGE(…)`) |
| `implicit` | `@` or argument of `_xlfn.SINGLE` |
| `spill` | `A1#`, `Sheet!A1#`, sole argument of `_xlfn.ANCHORARRAY` |
| `r1c1` | token like `R2C3`, `R`, `C`, `RC` (a reference, never a name) |
| `func_path`, `arg_index`, `call_index` | enclosing calls (outermost first), argument position and index of the innermost call |
| `parts` | `RefPart(kind, text, row, col, row_abs, col_abs)` endpoints (`cell col row name r1c1 error`) |
| `value` | literals: float, str, bool, `'#N/A'`, array rows `((1.0, 2.0), ('a', True))` |
| `is_reference`, `is_literal`, `names`, `reaches_sheet_edge` | convenience properties |

Classification rules worth knowing:
* `A1:A1048576` is `whole_column` (Excel shows it as `A:A`); `A2:A1048576` is a `range` with
  `reaches_sheet_edge`. `1:1048576`/`A:XFD` are `whole_row`/`whole_column` with shape `sheet`
  (`A1:XFD1048576` is `whole_row`).
* Areas are a cell, `COL:COL` or `ROW:ROW`, paired left to right; a chain takes the bounding
  box: `A:A:B5` is `whole_column` with bounds `(1, 1, 1048576, 2)`. A column-shaped endpoint
  with no column partner is a defined name: `Tax:Rate` and `A1:B` are `range`s whose `names`
  are `('Tax', 'Rate')` / `('B',)`; `Tax:Rev` (both valid columns) is whole columns TAX:REV.
* Trimmed forms (`A:.A`, `A.:A`, `A.:.A`, `_xlfn._TRO_*(A:A)`, `TRIMRANGE(A:A)`, `TRIMRANGE((A:A))`)
  are `trimmed_range`, never `whole_column`.
* `Lim1`, `TAX2024`, `XFD1048576` are cells; `XFE1`, `A1048577`, `Tax` are names.
* String literals are only ever `string` operands: `"1:1"`, `"OFFSET(A1)"`, `"Rate"`,
  `"[1]Sheet1!A1"` are never references, calls or names.
* Range-ending calls are split: `A1:OFFSET(A1,1,1)` → operand `A1`, op `:`, call `OFFSET`.

## Module-level helpers (accept text or a `Formula`)

```python
F.function_calls(text)                        # list[FunctionCall]
F.names_referenced(text)                      # list[NameRef]
F.whole_column_or_row_refs(text, names=None, scope_sheet=None, include_sheet_edge=False)
F.references_other_sheet(text, own_sheet, names=None, scope_sheet=None, sheet_names=None, table_sheets=None)
F.references_external_workbook(text, names=None, scope_sheet=None, sheet_names=None)
```
With `names` (a `NameTable` or list of tuples) the defined names the formula uses are
expanded transitively and their operands count too (`MEDIAN(Peer_EV)` with
`Peer_EV = Comp!$H:$H` → `['Comp!$H:$H']`). `scope_sheet` = the sheet the formula lives
on (defaults to `own_sheet` for `references_other_sheet`). A qualified **name**
(`Inputs!Rate`) does not count as another sheet until resolved (use `names=`).
Unqualified references inside a name are relative to the using sheet (so not "other").

## Names (checks 29, 80, 87, 95, 49–51)

```python
tab = F.NameTable([(name, scope_sheet_or_None, text, hidden), ...], sheet_names=[...])
tab = F.NameTable(pkg.defined_names, sheet_names=[...])   # the reader's DefinedName objects work too
# scope must be a sheet NAME or None (a localSheetId int raises); hidden may be a bool, 0/1,
# None or an XML boolean string ("0" -> False); bad entries raise FormulaError
tab.resolve(ident, scope_sheet=None, qualifier_sheet=None, workbook_scoped=False)
F.names_used_by(text, scope_sheet, tab, include_indirect_literals=False, sheet_names=None)
F.expand(text, scope_sheet, tab, sheet_names=None)   # -> Reach (transitive)
F.functions_reached(text, scope_sheet, tab) # -> frozenset of built-in names
```
* Scope (Excel): unqualified `Rate` on sheet S → local `Rate` of S, else global. `S!Rate` →
  local of S, else global. `[0]!Rate` → global. A global name's definition resolves globally
  (`scope_sheet=None`); a local name's definition with its own sheet. Case-insensitive.
* Counted as use: name operands, both ends of `N1:N2`, `Name#`, and calls `MyFn(…)` of a
  non-built-in function (LAMBDA names). An Excel built-in called as a function
  (`YEAR(…)` with a defined name `Year`) is the built-in, unless stored as `_xludf.YEAR(`.
* Never counted: LET/LAMBDA parameters, external names (`[1]!Rate`), string literals.
* LET/LAMBDA scope (as Excel binds it): a LET name is bound only after its value, so in
  `LET(wacc,WACC,A1/wacc)` the second `WACC` is the defined name and in
  `_xlfn.LET(_xlpm.today,TODAY(),…)` `TODAY()` is the volatile built-in; a LAMBDA
  parameter is bound in the LAMBDA's body. A parameter declared `_xlpm.x` (how Excel stores
  it) is referenced as `_xlpm.x`, so a plain `x`/`x(…)` inside its scope is the defined name
  or built-in (`_xlfn.LET(_xlpm.rate,0.05,_xlpm.rate*Rate)` uses the name `Rate`;
  `OFFSET(…)` stays the volatile built-in). Only a plain declaration (`LET(x,1,x+1)`, as
  some libraries write it) binds plain `x`. `include_indirect_literals=True` also counts a literal
  first argument of `INDIRECT("Rate")` / `HYPERLINK("#Rate")` (incl. `"Ra"&"te"`).
* `DefinedName`: `name scope text hidden index`, `.builtin` (`_xlnm.`), `.validity_problem`
  (`LIM1`, `R2C3`, `R`, `C` are not usable names), `.formula()`, `.is_lambda`.
  Broken definition: `tab.get(n).formula().ref_errors()`.
* `Reach`: `functions` (built-in names), `calls` / `operands` as `(via, obj)` with `via` the
  tuple of `DefinedName`s walked, `names` reached, `cycle` (bool), `calls_any(*names)`.
  Cycle-safe. An unparsable definition raises `FormulaError`.

## Shared formulas

```python
F.translate("AAK$169+AAK$170", "AAK171", "ABC171")   # 'ABC$169+ABC$170'
F.shift(text, drow, dcol)
```
Relative parts move, `$` parts do not; whole columns move only horizontally, whole rows
only vertically; names (also as range endpoints: `Tax:Rate`, the `B` of `A1:B`), structured
references, strings, errors and R1C1 tokens are kept byte-for-byte, as is all whitespace. A reference pushed off the grid becomes `#REF!`
(qualifier kept: `Sheet1!#REF!`). Uses openpyxl's `Translator.translate_row/col`
arithmetic but this module's lexer, so `A1#`, TAB/CR, `A1:OFFSET(`, `A:.A`, quoted /
external / 3-D qualifiers work. Formula *properties* (functions called, operand kinds,
sheets, external books) are the same for every child of a shared group, so checks that
only need those can parse the master once.

## Pre-filters for very large workbooks

`quick_may_have_whole_refs(text)` and `quick_may_call(text, names)` are conservative:
`False` guarantees no direct whole-column/row reference / no direct call of those
functions (names are not expanded). `mask_strings(text)` blanks string literals; quoted
sheet names (`'Q"1'!A:A` — a sheet name may hold `"`) and `[…]` groups are skipped, not
blanked. The pre-filters do not validate: `False` on malformed text (`SUM(C16:C22`) means
"nothing found", not "parses" — a check that skips `parse` on `False` will not raise for it.

## Name validity / A1 helpers

`is_cell_reference`, `is_r1c1_reference`, `name_validity_problem`, `is_valid_name`,
`col_index('XFD') == 16384`, `col_letters(16384) == 'XFD'`, `parse_qualifier(raw) -> Qualifier`.
Constants: `MAX_ROW`, `MAX_COL`, `BUILTIN_FUNCTIONS` (533 names), `VOLATILE_FUNCTIONS`
(INDIRECT OFFSET TODAY NOW RAND RANDBETWEEN INFO CELL RANDARRAY — RANDARRAY is outside the
rubric's list; checks decide), `TRIM_FUNCTIONS`, `ERROR_LITERALS`, `REFERENCE_KINDS`.

## Recipes

* 80 volatile: `F.expand(text, sheet, tab).functions & F.VOLATILE_FUNCTIONS` for cells, CF,
  DV and (separately) every name; date stamp = `F.parse(t).body.strip().upper() in ("TODAY()","NOW()")`.
* 87: `F.whole_column_or_row_refs(text, names=tab, scope_sheet=sheet)`.
* 95 / 51: `F.references_external_workbook(text, names=tab, scope_sheet=sheet, sheet_names=sheets)`.
* 49 / 50: `f.references_other_sheet(own, table_sheets=tables)` with
  `tables = {t.display_name: t.sheet for si in pkg.sheets for t in pkg.tables(si)}`,
  `f.pure_reference(transparent=(...))`; shared children: classify the master (sheets do not
  change with translation).
* 29: union of `F.names_used_by(...)` over every formula site, plus names reached through
  used names (`F.expand`); visible = `not n.hidden and not n.builtin`.

## Known limits

* No evaluation: `INDIRECT`/`OFFSET` targets are unknown (only literal INDIRECT text, opt-in).
* Sheet names are not checked against the workbook (`Missing!A1` is just sheet "Missing").
* `A1:Sheet1!B5` (qualifier only on the second endpoint) reads the first `!` as the qualifier.
* The intersection (space) and union (`,` inside parentheses) operators are not modelled;
  their operands are simply listed.
* Not a full grammar: operand adjacency and area pairing are checked, but operator placement
  is not (`5%A1`, `A1+*B1` parse).
* An unprefixed call of a function newer than `BUILTIN_FUNCTIONS` is treated as a possible
  name call; this only matters if a defined name with that spelling exists.
* `parse` is ~60 µs per formula on average over the toy corpus (long LET formulas), ~30 µs
  for typical model formulas; 607k distinct formulas (TwoNOne) take ~40 s — parse shared
  masters once and use the pre-filters.

## Validation (2026-10-02)

Review fixes (LET/LAMBDA scope, `_xlpm.` binding, adjacency/pairing errors, area pairing,
`mask_strings`, table sheets, NameTable validation), re-checked old vs new parser
(`detchecks/scratch/review_fix/formula_corpus_diff.py`, `real_diff.py`, via `heavy_run.py`):
all 1,623,195 distinct toy formulas — 0 parse errors, 0 changes in operands, calls, names or
params, 0 pre-filter misses (both filters); 6,168,637 distinct formulas of 101 real attempts —
no new parse errors, one changed formula (attempt 2487 A33: plain `me(` inside an
`_xlpm.me` LAMBDA is now a name call; no name `me` exists, cached `#VALUE!`), 0 pre-filter misses.

Original validation:

All 1,623,195 distinct formulas of the 29/49/50/51/66/73/80/87/94/95 toys
(`detchecks/scratch/diff_corpus.py`, via `heavy_run.py`): 0 parse errors; function names
and operand texts identical to openpyxl's Tokenizer on every formula (after accounting for
its known `A1:FUNC(` glue); `translate` identical to openpyxl's Translator on 200,000
formulas it handles; on 202,900 of them, translating to three targets kept every
function name and operand kind (sheets, external books) unchanged; the quick filter never missed. Every `name` operand found in the
corpus is a real defined name (mtok, MonthsperYear, WACC_rate, …) — no LET/LAMBDA
parameter leaked. Unit tests: `python -m detchecks.tests.test_formula` (every toy trap
for 29, 49, 50, 51, 80, 87, 95).
