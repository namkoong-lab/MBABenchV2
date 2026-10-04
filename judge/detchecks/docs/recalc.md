# Recalculation pipeline (`detchecks/core/recalc.py`)

Where a check's formula VALUES come from. Built 2026-10-04 from Patrick's design (handoff.md,
"Recalculation design"). Structure and styles always come from the delivered file; a copy supplies
values only.

```python
from detchecks.api import grade
from detchecks.core.recalc import RecalcPolicy
verdicts = grade(path, checks=[22, 66], recalc=RecalcPolicy())
verdicts[key]["stats"]["values"]   # writer, source (cache / libreoffice / excel), gaps, timings
```

`value_path=` stays as a manual override (then no pipeline runs and the copy's `#NAME?` / `#VALUE!`
stay untrusted, as before). Without `recalc=` nothing changes either: values come from the file's
caches under the trust policy (reader.md §6).

## Decision

| step | rule |
|---|---|
| 1 | The delivered file was saved by Excel (`values.detect_writer == "excel"`: Excel application string, AppVersion, `fileVersion appName="xl"`, Excel's XML declarations on every part) → **source `cache`**: Excel's own cached values are the display. No recalculation, even with `fullCalcOnLoad` (toy 22/T5 relies on the implicit-intersection detector for that). |
| 2 | Any other writer (openpyxl, XlsxWriter, LibreOffice, ChatGPT / unknown) → **LibreOffice recalculation copy**: `soffice --headless --convert-to xlsx`, private profile per run with threaded calculation OFF, OpenCL OFF, `OOXMLRecalcMode = 0` (always recalculate on load, otherwise LibreOffice keeps the agent's caches), macros off, `--norestore`, one process, `lo_timeout_s` (600 s default; timeout → GradingError). Same sheet names. |
| 3 | **Gap scan** (`find_gaps`): every formula cell whose LibreOffice value is `#NAME?` or `#VALUE!` is classified (`classify_lo_error`, table below). Any gap → the file **needs Excel**. No gap → the copy is used with `errors_vetted=True`: its `#NAME?` / `#VALUE!` are genuine Excel errors and trusted values. |
| 4 | Gaps and `policy.excel_allowed` and Excel available → **Excel recalculation copy** (`excel_recalc`): open read-only, links not updated, alerts off, `CalculateFullRebuild`, save a copy as .xlsx, close without saving the original, quit Excel if it was not running before. → source `excel`. |
| 5 | Gaps and Excel **off** (`excel_allowed=False`, the default since 2026-10-04 evening) → the LibreOffice copy is used **as displayed** (`errors_vetted=True`): the cells LibreOffice could not compute keep their `#NAME?`/`#VALUE!`, value checks see an error value (not a number, not a zero, not a negative) and skip them, exactly as judge v12 effectively did. The gap list and a note stay in `stats["values"]`. Gaps and Excel **on** but not available / failing → `GradingError("Excel recalculation required: ...")`; only value checks fail. |

Cache: `workdir/<sha256 of the delivered file>/{libreoffice,excel}/<name>.xlsx` + `meta.json`, so a
file is recalculated once per pipeline run (a LibreOffice copy with gaps is kept and reused when
Excel becomes available). Default workdir `detchecks/out/recalc_cache/`.

## Gap classification (`classify_lo_error`)

A LibreOffice `#NAME?` / `#VALUE!` on a formula cell is a **gap** (Excel may compute it) when

- the formula calls a function LibreOffice lacks (`LO_UNSUPPORTED_ALWAYS`: LAMBDA MAP REDUCE SCAN
  MAKEARRAY BYROW BYCOL ISOMITTED GROUPBY PIVOTBY PERCENTOF TRIMRANGE IMAGE ANCHORARRAY SINGLE
  STOCKHISTORY FIELDVALUE ARRAYTOTEXT VALUETOTEXT PY; on builds before 24.8 also XLOOKUP XMATCH
  FILTER SORT SORTBY UNIQUE SEQUENCE RANDARRAY LET; before 25.8 also CHOOSECOLS CHOOSEROWS DROP
  EXPAND HSTACK VSTACK TAKE TEXTAFTER TEXTBEFORE TEXTSPLIT TOCOL TOROW WRAPCOLS WRAPROWS REGEXTEST
  REGEXEXTRACT REGEXREPLACE), uses a spill reference (`A1#`, `ANCHORARRAY`), calls a defined name
  (a LAMBDA), or uses a defined name whose definition uses such a function;
- `#NAME?` although every called function is a valid Excel function (`EXCEL_FUNCTIONS`, 533 names)
  and every name is defined: LibreOffice does not know it ("unknown function → #NAME?");
- `#VALUE!` on an array formula, or on a plain formula that holds a multi-cell range, a whole
  column/row, a structured reference, a defined name, an array constant or an array-returning
  function (`ARRAY_FUNCS`: SEQUENCE TRANSPOSE MMULT ... OFFSET INDEX INDIRECT): Excel's legacy
  evaluation (implicit intersection / top-left) may give a number where LibreOffice gives
  Err:502/504 (exported as `#VALUE!`);
- the formula text cannot be parsed, or is unavailable (data table).

It is **genuine** (Excel shows the error too) when the formula calls a name that is not an Excel
function (misspelling: `CONUTIFS`, toy 22/T6), references an undefined name, or - for `#VALUE!` -
has only single-cell / literal operands and scalar functions (`"abc"+1`).

Corpus evidence (LibreOffice-written files and copies, 2026-10-04 scan): `#NAME?` on SCAN,
LAMBDA, REDUCE, MAKEARRAY, MAP, BYCOL (LO 7.4 also SEQUENCE, LET, bare `xlookup(`); `#VALUE!` on
spill references (`INDEX(B20#,1,2)`) and on plain-cell array arithmetic (`B8:EJN8-B64:EJN64-MMULT(...)`).

Dependents of a gap cell show the error in LibreOffice too; they need no separate treatment
because one gap already reroutes the whole file. Dependents of a *genuine* root that hold a range
(`SUM(B5:B9)` over a `"abc"+1` cell) are classified as gaps and send the file to Excel needlessly:
a cost, never a wrong verdict.

## Known limits / questions for Patrick

1. **LibreOffice-only codes are folded into Excel codes** by the xlsx export: a circular reference
   with iteration off (Err:522) comes out as `#VALUE!`. Check 22 would then count it (check 100's
   domain). Not seen in the corpus copies; a cycle makes the cell a gap only if its formula holds a
   range. Option: a static cycle detector (the prototype had one) or a CSV export for native codes.
2. **Excel's `#NAME?` for unprefixed future functions** (`xlookup(` written without `_xlfn.` by a
   non-Excel tool): LibreOffice 25.8 computes them, Excel may show `#NAME?` until the cell is
   re-entered (openpyxl/XlsxWriter documentation; not measured). An Excel recalculation of corpus
   attempt 2121 (252 such cells) would settle it; if Excel shows `#NAME?`, a static rule should flag
   them whatever the value source.
3. **Excel-saved files with `fullCalcOnLoad`** keep source `cache` (design step 1). Their caches can
   be stale; only the implicit-intersection detector corrects them today.
4. `#VALUE!` genuineness is static; an `IFERROR`-wrapped gap cell shows the fallback in both engines
   and is never a gap (correct).

## Excel recalculation (`excel_recalc`)

- **macOS**: `osascript` (script in `_APPLESCRIPT`): `open workbook ... read only true update links do
  not update links`, `calculate full rebuild`, `save workbook as ... file format Excel XML file format
  with overwrite`, `close wb saving no`, `quit` only when Excel was not running before (`pgrep`).
  Timeout → osascript killed, Excel killed only if this run launched it. **STATUS 2026-10-04: run twice
  here (388: LibreOffice 2.8 s then Excel 154 s; a 75 KB toy forced: Excel 70 s) and it works, BUT Mac
  Excel's sandbox shows a "grant access" prompt for every file opened from outside its container, so
  it is not usable unattended as written. Patrick's decision: LibreOffice only; Excel step OFF.**
- **Windows**: PowerShell + COM (`_POWERSHELL`: `Workbooks.Open(src, 0, $true)`,
  `CalculateFullRebuild`, `SaveAs(dst, 51)`). **UNTESTED** (no Windows machine).
- Availability probe: macOS `/Applications/Microsoft Excel.app` (or `~/Applications`); Windows
  registry `HKCR\Excel.Application\CLSID` (untested); elsewhere never.
- `RecalcPolicy.excel_allowed` defaults to **False** (Patrick 2026-10-04 evening: LibreOffice only
  for now). With it True, a machine without Excel fails loudly only for files that need it.

## Provenance in verdicts

Every verdict of a check with `needs_values` gets `stats["values"]`: `writer`, `source`
(`cache` / `libreoffice` / `excel` / `value_path`), `value_path`, `value_writer`, and from the
pipeline `n_gaps`, `gap_functions`, `gaps` (first 25: sheet, ref, value, formula, functions, why),
`n_lo_errors`, `timings` (`libreoffice_s`, `gap_scan_s`, `excel_s`, `from_cache`), `lo_version`,
`notes`. `wb.provenance.value_kind` / `.recalc` carry the same inside checks.

## Tests

`detchecks/tests/test_recalc.py` (fake runners: cache path, LibreOffice path with and without
gaps, genuine errors, reroute to Excel, not allowed / not available, caching, engine partial
failure, classification table). No test launches LibreOffice or Excel.
