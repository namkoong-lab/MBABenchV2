# detchecks documentation

The deterministic rubric checks (`judge/detchecks/`) grade a delivered workbook
on 19 rubric items before the LLM judge runs; `judge/utils/det_checks.py` is the
adapter that calls them and overlays their verdicts at scoring. These documents
are the reference for check authors and reviewers.

| document | what it is |
|---|---|
| [reader.md](reader.md) | API reference: the reader and engine, how to write a check, the `Package` the checks stream |
| [formula.md](formula.md) | `core/formula.py`: formula parsing for check authors, names, shared formulas |
| [numfmt.md](numfmt.md) | `core/numfmt.py`: the Excel number-format engine the value checks render with |
| [recalc.md](recalc.md) | `core/recalc.py`: when formula values come from the file and when from a LibreOffice recalculation; the memory guard; legacy `.xls` |
| [excel_measurements.md](excel_measurements.md) | behaviour measured by hand in Excel that the rules rest on |
| [checks/NN.md](checks/) | one note per check, by rubric number: the rule, the maintainer's rulings, the toy gate and corpus results at the time the check was built |
| [../CHANGELOG_core.md](../CHANGELOG_core.md) | what changed in the shared core and across checks, newest first |

The per-check notes are working records: they quote attempts of the
maintainers' database by id and refer to working folders (`scratch/`, `out/`,
`corpus/`, the toy workbooks) that are not in the repository. The tools under
`detchecks/tools/` take those folders as arguments (`run_toys.py --toys-root`,
`run_corpus.py --root`); `grade_one.py` and `recalc_one.py` work on any single
workbook.
