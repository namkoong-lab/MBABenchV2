# Recalculation pipeline (`detchecks/core/recalc.py`)

Where a check's formula VALUES come from. Built 2026-10-04 from Patrick's design (handoff.md,
"Recalculation design"). Structure and styles always come from the delivered file; a copy supplies
values only. (The judge's one exception: a legacy .xls delivery graded with `--run-calculation` is
graded on LibreOffice's .xlsx conversion, as judge v12 grades it - "Legacy .xls deliveries" below.)

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
| 2 | Any other writer (openpyxl, XlsxWriter, LibreOffice, ChatGPT / unknown) → **LibreOffice recalculation copy**, whatever the file's size: `soffice --headless --convert-to xlsx`, private profile per run with threaded calculation OFF, OpenCL OFF, `OOXMLRecalcMode = 0` (always recalculate on load, otherwise LibreOffice keeps the agent's caches), macros off, `--norestore`, one process, `lo_timeout_s` (600 s default), under the memory guard below (one LibreOffice at a time, memory wait, retries with the timeout doubled; then `LibreOfficeUnavailable`). Same sheet names. |
| 3 | **Gap scan** (`find_gaps`): every formula cell whose LibreOffice value is `#NAME?` or `#VALUE!` is classified (`classify_lo_error`, table below). Any gap → the file **needs Excel**. No gap → the copy is used with `errors_vetted=True`: its `#NAME?` / `#VALUE!` are genuine Excel errors and trusted values. |
| 4 | Gaps and `policy.excel_allowed` and Excel available → **Excel recalculation copy** (`excel_recalc`): open read-only, links not updated, alerts off, `CalculateFullRebuild`, save a copy as .xlsx, close without saving the original, quit Excel if it was not running before. → source `excel`. |
| 5 | Gaps and Excel **off** (`excel_allowed=False`, the default since 2026-10-04 evening) → the LibreOffice copy is used **as displayed** (`errors_vetted=True`): the cells LibreOffice could not compute keep their `#NAME?`/`#VALUE!`, value checks see an error value (not a number, not a zero, not a negative) and skip them, exactly as judge v12 effectively did. The gap list and a note stay in `stats["values"]`. Gaps and Excel **on** but not available / failing → `GradingError("Excel recalculation required: ...")`; only value checks fail. |

Cache: `workdir/<sha256 of the delivered file>/{libreoffice,excel}/<name>.xlsx` + `meta.json`, so a
file is recalculated once per pipeline run (a LibreOffice copy with gaps is kept and reused when
Excel becomes available). Default workdir `detchecks/out/recalc_cache/`.

## The LibreOffice process (`libreoffice_recalc`, `core/lo_watchdog.py`; 2026-10-04 review fixes)

- **Paths are encoded file URLs** (`file_url` = `Path(abspath).as_uri()`): the profile
  (`-env:UserInstallation=`), `--outdir` and the source. A raw `file://` + path with a space made
  LibreOffice abort (exit -6, task 28 "FruitJuice_3-Statement-Model - v2"); a `%41` in the profile URL
  or in a plain `--outdir` path is decoded by LibreOffice, so the profile and the copy went to a
  folder named with `A` next to the task folder and the run "produced no copy". Verified with the
  real LibreOffice 25.8 on folders with a space, `%`, `%41`, `%25` and `é` (profile used, stale cached
  value recalculated, nothing written outside the folder).
- **A conversion never outlives its grading**: soffice runs in the grader's process group (no new
  session), so a signal to the grader's group (Ctrl-C, `heavy_run.py`'s group kill) reaches it; it runs
  under `core/lo_watchdog.py` (`python -I -S lo_watchdog.py <grader pid> 0.5 -- soffice ...`), which
  passes soffice's exit code through and, when the grader disappears (SIGKILL, a crash: anything that
  runs no clean-up) or the watchdog itself gets SIGTERM / SIGINT / SIGHUP, kills soffice and everything
  below it; `install_termination_reaper` (main thread, only where the signal still had its default
  action) makes SIGTERM / SIGHUP kill this process's running conversions before the process dies of the
  signal as before; a timeout or any exception (KeyboardInterrupt too) kills the run's whole tree
  (`lo_watchdog.kill_tree`: SIGSTOP the tree until no new child appears, then SIGKILL), and every run
  ends with a sweep of its private profile (`reap_profiles`, raw path or file URL in a command line).
  The profile folder is private to one grading, so other jobs' LibreOffice is never touched.

## Memory safety (`core/lo_guard.py`; Patrick 2026-10-05)

"For production runs, every attempt must be graded. Doesn't matter what size."  No file is refused for
its size (the judge's `det_checks.libreoffice_max_mb` is 0 = no limit; a positive value is a test-run
setting).  Instead `libreoffice_recalc` - and, in the judge, the answer check's recalculation and
`--run-calculation` (`utils.det_checks.run_libreoffice`) - go through `lo_guard.run_guarded`:

| step | rule | setting (RecalcPolicy / judge `det_checks.*`) |
|---|---|---|
| lock | ONE LibreOffice at a time on the machine: `fcntl.flock` on the lock file plus a process-wide `threading.Lock` (worker threads); re-entrant within a thread; the descriptor is close-on-exec (LibreOffice never holds it) and the kernel releases it when a grader dies; a waiting grader logs who holds it (pid, host, file) at most once a minute | `lo_lock_path` / `libreoffice_lock_path`, default `/tmp/mbabench_libreoffice.lock` (`$DETCHECKS_LO_LOCK` for tools) |
| lock wait | the wait for that lock is capped (Patrick 2026-10-05: every attempt graded - one stuck file cannot stall a run indefinitely); after it the run is not started: `LibreOfficeUnavailable` (`retry_later`), the attempt is re-run later | `lo_max_lock_wait_s` / `libreoffice_max_lock_wait_minutes` = 10800 s / 180 min |
| memory | holding the lock, wait until the machine's free memory (macOS `memory_pressure -Q` "System-wide memory free percentage", Linux `MemAvailable / MemTotal`) is at least the threshold; logged while waiting; unreadable → no wait (logged once) | `lo_min_free_pct` / `libreoffice_min_free_pct` = 25 |
| max wait | after this long the run is not started: `LibreOfficeUnavailable` (`retry_later`) - the attempt is re-run later | `lo_max_wait_s` / `libreoffice_max_wait_minutes` = 3600 s / 60 min |
| retries | a try that fails (crash, no copy, timeout) is retried, the timeout doubled each time (600, 1200, 2400, 4800 s), each retry taking the lock and waiting for memory again; a missing binary is not retried | `lo_retries` / `libreoffice_retries` = 3 |
| failure | after the last try: `LibreOfficeUnavailable` listing every try's error; through `grade()` the value checks fail with `values unavailable: ...` and the `GradingError` carries `retry_later`, which `utils/det_checks` passes on (`DetChecksError.retry_later`, `det_checks.json` `retry_later`); the judge's drivers list those attempts at the end of the run to be re-run when the machine has memory to spare | - |

The watchdog, the termination handler, the timeout tree-kill and the profile sweeps below are unchanged
and apply to every try.  Tests: `tests/test_lo_guard.py` (lock across two processes and threads, the
memory wait, the maximum wait, the retries) and `tests/test_recalc_libreoffice.py` (a stand-in soffice
that crashes twice then converts; one that always fails; two grader processes sharing the lock).

## Legacy .xls deliveries (the judge's adapter, `utils/det_checks.py`; Patrick 2026-10-05)

"Just keep doing whatever v12 did or does."  The judge stages the first .xlsx / .xlsm / .xls delivery as
`ai_attempt.xlsx` without converting it; `grade()` refuses .xls bytes for every check but File extension
(.xlsx) (77) (`Package.is_spreadsheetml` is False).  judge v12 fails such an attempt without
`--run-calculation` (openpyxl cannot open it) and with it grades LibreOffice's re-saved .xlsx copy.  The
adapter does the same - nothing here in `core/` changes:

- `legacy_xls(path)`: the bytes are an OLE2 compound file (`Package.open`: format `xls`, not an encrypted
  package) whose directory lists a `Workbook` (BIFF8) or `Book` (BIFF5) stream, whatever the name.  Any
  other file - every zip package, an OLE2 file without a workbook stream or with an unreadable directory -
  is graded (or refused) as before.
- with `--run-calculation` (`run_det_checks(..., run_calculation=True)`): the pipeline's own LibreOffice
  step (`RecalcPolicy.lo_runner`, i.e. `libreoffice_recalc` under the memory guard, private profile and
  watchdog; the judge's test-run size limit applies) converts the delivered file to
  `<workdir>/xls_converted/ai_attempt.xlsx` (`--convert-to xlsx`; LibreOffice detects the format from the
  content, the .xlsx name does not matter).  `grade()` then grades that copy for every check but 77, with a
  policy whose `lo_runner` returns the copy itself: `ensure_values` sees a LibreOffice-written file, takes
  the values from the copy (no second LibreOffice run), runs the gap scan and uses the gaps as displayed
  exactly as for any LibreOffice copy.  The values are LibreOffice's own: LibreOffice recalculates every
  formula of an .xls it loads, whatever `OOXMLRecalcMode` says (verified 2026-10-05 with LibreOffice
  25.8.7: an .xls whose BIFF FORMULA record caches a stale 999 for `=B1-B2` converts to 95).  77 grades the
  delivered file with the delivered name, and fails.  Recorded: `det_checks.json` `xls_conversion` (copy
  path, bytes, sha256, `conversion_s`, which checks graded which file), `stats.graded_on`
  (`libreoffice_xlsx_conversion` / `delivered_file`) on every verdict, and in `stats["values"]` a note and
  `timings.libreoffice_s` = the conversion's time.  The copy is deleted with `det_checks_recalc/`.  A failed
  conversion fails the checks graded on the copy (`retry_later` when LibreOffice could not run now).
- without `--run-calculation`: `grade()` runs on the delivered file as before and every check but 77 fails
  loudly; the error adds that an .xls delivery is graded only with `--run-calculation`.

Tests: `tests_offline/test_det_checks_xls.py` (the real LibreOffice; run it through `heavy_run.py`).

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
failure, classification table). No test there launches LibreOffice or Excel.

`detchecks/tests/test_recalc_libreoffice.py`: ONE test runs the real LibreOffice on a 3-cell file in a
folder named with a space, `%41`, `%` and `é` (skipped, reported SKIP, where LibreOffice is absent;
run the module through `heavy_run.py`); the others use stand-in soffice scripts that keep a tagged
child: the watchdog's normal and failing runs, the timeout tree kill, SIGTERM and SIGKILL to a stand-in
grader (nothing survives), the watchdog alone when its parent is SIGKILLed, the handler's rules, and the
profile sweep (URL and raw forms, never a sibling folder).
