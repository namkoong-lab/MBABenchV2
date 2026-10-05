# Judge — Quickstart

LLM-based grader for Excel-task attempts. Runs against either benchmark;
`--benchmark v1|v2` is the only switch.

## Configuration

Two files, nothing to copy or edit per run:

- **`<MBABenchV2>/config/config.yaml`** (gitignored, shared by every harness):
  `database.v1_url` / `database.v2_url`, `aws.*` (S3 bucket + credentials),
  `keys.openrouter_api_key` / `gemini_api_key` / `anthropic_api_key` /
  `openai_api_key`. Environment variables (`OPENROUTER_API_KEY`, …) win
  over `keys.*`; `DATABASE_URL` is only a fallback when the config has no
  URL for the benchmark.
- **[project_configs.yaml](project_configs.yaml)** (tracked, secret-free):
  benchmark-agnostic judge settings — model, prompt template, char limits,
  agentic limits, LibreOffice path. Loaded into `BIZBENCHJUDGE_*` env vars
  by `utils/misc_utils.load_project_configs()`.

`--benchmark` selects the rest from `BENCHMARKS` in
[utils/misc_utils.py](utils/misc_utils.py): the database, the S3 grading
root (`BizbenchV1/grading` vs `MBABenchV2/grading`), and the rubric pair +
category order (v1 = rubric_8 / rubric_6_weights, 3 categories; v2 =
rubric_9 / rubric_9_weights, 12 categories). A URL naming the other
benchmark's database is refused at startup (`JUDGE_SKIP_BENCHMARK_GUARD=1`
bypasses, for one-off experiments only).

## Install

From the repo root, `./setup.sh` (uv workspace; installs `excel_judge`
editable and the `config` module). LibreOffice is needed for
`--run-calculation` and, from judge v13, for the deterministic checks on
attempts not saved by Excel (`paths.libreoffice_path`).

## Grade attempts from the database

```bash
python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 2 3
python judge/main_scripts/grade_from_db.py --benchmark v2 --agentic --task-ids 4 5
# judge v4 experiment: all checks in ONE conversation (implies agentic)
python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --attempt-ids 6
```

v2 must be graded with `--agentic`: the standard judge's
`prompts/judge_template_7_0.yaml` hardcodes one stage per v1 category.
(TODO: a template whose stages are generated from `JUDGE_CHECK_ORDER`
would lift this.)

Useful flags: `--dry-run`, `--no-db-write`, `--no-s3-upload`, `--nocall`,
`--model <slug>`, `--reasoning-effort {none,minimal,low,medium,high}`.
`--model` takes a grader label registered in `judge_identities.yaml`, which
pins the endpoint (openrouter | gemini | anthropic | openai | tensorblock), the wire model
id, and the default reasoning effort. An unregistered label refuses to run
and prints the stanza to add.

### v2 agentic regime (judge_version 3, 2026-08)

Since the 2026-08 update (rubric_9 revised in place from the canonical
checklist xlsx via `operation_scripts/build_rubric_9_from_xlsx.py`; weights
adopted from the same sheet), a v2 agentic grading additionally:

- **Gates checks by per-task suitability** (`utils/rubric_suitability.py`):
  the latest complete julian annotation from
  `s3://<bucket>/MBABenchV2/rubric_suitability/` is fetched by grade_from_db,
  staged as `<task folder>/rubric_suitability.json`, and validated against
  the rubric; `not_applicable` checks are never prompted or scored, weights
  renormalize within category, CategoryWeights stay fixed. A v2 grading
  without an annotation refuses (`JUDGE_SKIP_SUITABILITY=1` grades ungated);
  provenance lands in `scored_results.rubric_suitability`.
- **Refuses workbooks whose formulas were never calculated**
  (`utils/formula_cache.py`): the judge reads *cached* formula results, so a
  workbook saved without calculation reaches it as formulas with no values and
  Accuracy cannot be graded from evidence. Both the attempt and the golden
  solution are censused — from the staged workbook's XML when it is on disk
  (since 2026-09-03: a formula whose calculated result is the empty string is
  stored as `t="str"` with an empty value and counts as cached; only an
  untyped empty/absent value is uncached, which is what openpyxl writes for a
  never-calculated cell), falling back to the extracted CSVs when no workbook
  is available; the deciding basis, both censuses and the empty-string count
  are recorded. A workbook at or above `judge.uncached_formula_max_ratio`
  (default 0.5) refuses before any API call. Fix with `--run-calculation`, or
  set `JUDGE_SKIP_FORMULA_CACHE_CHECK=1` to grade anyway — the skip and the
  per-workbook counts are recorded in `scored_results.formula_cache`. Enforced
  in `_prepare_case`, so it applies to every mode (classic and agentic alike,
  and both benchmarks).
- **Runs the score-neutral answer check** (`utils/answer_check.py`): the
  Questions-sheet answers of attempt vs golden solution, compared with
  tolerance `|a-b| <= max(1e-9, 1e-6*max(|a|,|b|))`; full artifact
  `answer_check.json` rides with the raw files, summary in
  `scored_results.answer_check`. Never affects the 0-100 score. Side-by-side
  view: `operation_scripts/report_accuracy_engine.py`.
- **Serves category-keyed context views** (template 5): extraction writes a
  format-stripped `<sheet>_data.csv` beside every `<sheet>_full.csv`;
  `read_file` serves the data view except in Formatting, and attaches
  merged-cells/frozen-panes metadata once per sheet in Formatting and
  Structure. Listings show dimensions only, and the per-category user
  message keeps static blocks first so consecutive categories share a
  prompt-cache prefix. CSV caches live in the `*_csv_cache_v2` generation (now `_v6`, see judge v7).

### judge_version 4 / single-pass 5 (2026-09)

The 2026-09 update layers three things onto the v2 agentic regime (grades
are NOT comparable to judge_version 3 rows):

- **Grading guidance** (`prompts/rubrics/rubric_9_guidance.yaml`, loader
  `utils/rubric_guidance.py`): judge-only scope rules — a general
  don't-penalize-inherited-content rule, category notes, and per-check
  notes — rendered under the affected checks in BOTH modes. Validated
  against rubric_9.json at load (a renamed check refuses to grade). Never
  fold these into rubric_9.json (regenerated) or the agent prompts.
- **The starting workbook as a third readable source**: grade_from_db
  stages the task's starting xlsx as `starting/starting_workbook.xlsx`;
  `read_file` serves it as `source='starting'` so inherited-vs-agent-authored
  questions are checked, not guessed. Cached per task in
  `starting_csv_cache_v2`.
- **Single-pass mode** (`--single-pass` on grade_from_db and
  grade_with_orchestration — the judge v4 experiment): one conversation
  over every applicable check (globally numbered 1..132 in the rubric's
  flattened order — the suitability annotations' numbering; gating leaves
  gaps, never renumbers) instead of 12 per-category loops. Template
  `agentic_judge_template_7.yaml`; `read_file` gains a `view` parameter
  (`data`/`formatting`/`structure`) replacing the category key; rows record
  `single_pass.version` (5) / prompt_version 7, so they never mix with
  12-category rows (version 4 / template_6 / prompt_version 6) in the dedup
  key. Round budget `single_pass.max_rounds` (500 — effectively unbound for
  canaries; set the production value from measured usage).

### judge v6 — single-pass 6 / template_8 (2026-09-02)

The pipeline update after the v4/v5 canaries (single-pass only; the
12-category path is frozen at version 4). Rows record `judge_version` 6 /
`prompt_version` 8 and are not comparable to version 5 rows.

- **Harness-decided answer accuracy** (`utils/answer_check.py`, rulebook
  `utils/answer_rules.py`): the Questions-sheet answers are checked
  deterministically BEFORE the judge runs, and the checker's verdict on
  `Accuracy / Final calculation accuracy` (and the zero-answers case of
  `Deliverable completeness`) is overlaid onto the judge's at the scoring
  layer. `--accuracy-check harness|llm` (default `harness`, both drivers)
  picks which engine's decision lands in the recorded total; BOTH are
  always scored — `scored_results.accuracy_engine` carries
  `total_score_llm`, `total_score_harness`, and per-check provenance
  (engine, decision, rule fired, fallback reason, agreement with the LLM),
  and `ai_judgement_harness.json` sits beside the pure-LLM
  `ai_judgement.json`. The harness fails closed to the LLM verdict wherever
  it cannot measure (no answer sheet, layout not trusted), with the reason
  recorded. A numeric answer typed as a constant where the golden uses a
  formula is `hardcoded` and counts as a mistake
  (`single_pass.hardcoded_counts`).
- **Answer-equivalence rulebook** — one module, two consumers: the checker
  applies it and template_8 renders it verbatim under the Accuracy category
  standard (`RULES_VERSION`, recorded per grading). Rules v6.5 (2026-09-10):
  loan-schedule "payment" / "principal" rows join the extended outflow
  lexicon (House Standards attempts sign them negative, goldens are
  positive; "balance" rows stay guarded). Rules v6.4 (2026-09-02,
  after a $0 sweep of the checker over all 454 v2 attempts): an attempt is
  THE SAME number when it ROUNDS TO the golden — half a unit of the last
  decimal the golden carries, whether or not the agent rounded (the old
  "unrounded attempt gets only the noise band" clause failed 22 correct
  attempts on presentation alone); a full unit only when both sides are
  rounded figures. The decimals come from the golden's own `ROUND(...,n)`
  when present — read from openpyxl ArrayFormula objects, which every golden
  answer formula is (before v6.4 no golden ever counted as rounded) — else
  from the header phrase. Sign flips accepted on outflow rows: the core
  lexicon (expense/cost/spend/outflow/depreciation/amortization/capex/tax)
  unconditionally, an extended P&L list (energy, raw materials, SG&A/G&A,
  wages, R&D, marketing, interest, repayments, ...) unless an inflow/net/
  change/balance word marks the row. Percent and fraction forms equal, values
  not rendered strings, sentinel synonyms, dates, zero forms; unit-scale
  (x1000) differences are flagged, never accepted.
- **Rounding compliance is its own harness verdict** (v6.4): when the
  Questions sheet states a precision, an answer whose STORED value carries
  more decimals than asked (a display format alone does not round) fails
  `Rounding / Rounded outputs` — overlaid like the Accuracy checks, with
  `n_unrounded` and the directive recorded in `scored_results.accuracy_engine`.
  It never touches Final calculation accuracy.
- **Name-agnostic answer finder**: sheets are validated by the golden
  question TEXTS they contain (named Questions*/Answers* sheets win ties,
  decoys like "Answer Map" lose), rows are paired by text, the answer column
  is the header cell starting with "answer" in the block's header row.
- **Workbook properties block** (`utils/workbook_properties.py`,
  `_workbook_properties.json` beside the CSVs; caches moved to
  `*_csv_cache_v3`, now `_v4`): true tab order (file listings now follow it), hidden
  sheets/rows/cols, data validation, column widths / row heights, comments,
  conditional formats, hyperlinks, defined names, calc mode, print setup —
  rendered for attempt / solution / starting workbooks in the seed.
- **Standards + strictness** (template_8): guidance notes render as
  `Standard:` lines that are part of each check's definition; absence of
  evidence is not a pass; genuinely undecided after inspection = fail with
  the ambiguity described.
- **Native Anthropic path** (`utils/anthropic_native.py`): provider
  `anthropic` graders use the Messages API directly — real `effort` tiers
  (the OpenAI-compat endpoint ignores `reasoning_effort`), prompt caching
  (system-prefix breakpoint + automatic tail marker), thinking-block replay.
  Cached input is priced on every path (`token_tracking.total_cached_tokens`
  / `cache_savings`; OpenAI reads at 25%, Anthropic reads 10% / writes 125%).

  `grade_with_orchestration` also stages suitability annotations itself now
  (before 2026-09 it never passed them through, so it could not grade v2 at
  all) and shares the CSV cache generation with grade_from_db (`_v6` today).

### judge v7 — single-pass 7 / template_8 (2026-09-09)

Rows record `judge_version` 7 / `prompt_version` 8 (template unchanged) and are
not comparable to version 6 rows. Frozen 2026-09-14 when version 8 was cut.

- **Evidence the rubric grades on is now served** (2026-09-09, caches move
  to `*_csv_cache_v4`, properties schema 2). Properties block: cell
  hyperlinks (check 14 — `ws._hyperlinks` is empty after a load, links live
  on `cell.hyperlink`), manual page breaks (76), row/column outline groups
  with hidden ranges marked `(grouped)` (93), the style each
  conditional-format rule applies (32, 57), `(hidden)` defined names (29),
  VBA detected from the zip listing (96, 97), and the attempt's delivered
  filename from the `_attempt_origin.json` sidecar `setup_task_folder`
  writes (77). Cell extractor: dates/times rendered like Excel under their
  number format (45 — `2027-01-01`, `Jan-27`, `2:07 PM`, never a spurious
  timestamp), accounting padding `_x` / `*x` / `?` so zeros read `-` and
  negatives `(12,346)` (66, 65), populated cells blanked by their format
  served as `[ref]<raw> [FORMAT:<pattern>] [HIDDEN BY FORMAT]` for numbers
  and text (94), a formula cell always carries its `[ref]` even with an
  empty display (uncached or returning `""`), what-if data tables tagged
  on every member cell as `[DATA TABLE ref: {=TABLE(r,c)} anchored at X]`
  (90, 91, 99), and `wrap` restored in the formatting view (70). Test:
  `tests_offline/test_judge_v7_evidence.py`.
- **Tier 2 evidence sweep** (2026-09-10, caches move to `*_csv_cache_v5`, then `_v6` the same day when the canaries showed light yellows (`FFFFCC`) named `olive` at the 60° hue boundary — now `light_yellow`;
  properties schema 3; same test file). Cell extractor: multi-cell array /
  dynamic-array spills tagged on the anchor as `[SPILL C6:C1025]` and on
  every filled cell as `[SPILLED FROM C6]` — those cells used to read as
  hardcodes, or as `FORMULA:=` where Excel wrote `<f ca="1"/>` (41, 48-50,
  81, 83, 85-86, 129, 21, 27); the standard `[Red]` / `[$$-409]` number
  formats render like any other (zero `-`, negative `(1,235)`) instead of
  punting the whole format (45, 46, 65-67); theme-palette colours are
  resolved from the workbook's own `theme1.xml` (`utils/theme_palette.py`,
  tint applied in HLS) and emitted as ordinary `textcolor:` / `bgcolor:`
  tokens — before this every theme-coloured cell read as default black on
  no fill (43, 47-56, 59); the default text slot (theme 1, no tint) stays
  untokenised, matching the "missing key = Excel default" convention; blue
  hues 200-260° are named `blue` / `light_blue` / `muted_blue` /
  `dark_blue` instead of `pale_blue` / `muted_purple` / `slate_blue` (48,
  52, 55). Properties block: `styled empty cells in used range: N (e.g. …)`
  per sheet (28; left out while 28 is retired, see judge v9/v11); hidden defined names leave the listed set for the
  footnote `[+N add-in/system, +M hidden names not listed]` (9, 29); the
  true `active cell` per sheet, read from the selection of the view's
  active pane on frozen sheets (62); `N spill/array ranges` in each sheet's
  header and a bare `=` no longer counted as a formula. Numbers stored as
  text: a typed constant that reads like a number (`2024`, `1,234.50`,
  `(1,234)`, `45%`) is served as `[A4]2024 [TEXT]` (23, 63, 64) — a fact,
  not a verdict: version labels and list numbers are text on purpose, and
  the judge decides from context; formula results are never marked (the
  formula is visible). Measured cost: 13 marked cells across 6 sample
  attempts, 0 across 4 goldens.
- **Cover sheet is graded content** (2026-09-09): `--ignore-sheets` now
  defaults to nothing on both drivers. The port's `["cover"]` default deleted
  the `Cover` sheet's CSV before grading while rubric_9 grades cover content
  directly (cover sheet first, version history, master error flag, and any
  glossary / how-to / purpose / scope / design notes placed there); 669 of
  784 attempts name that sheet exactly `Cover`. The production orchestration
  runs (2026-09-05/07) were launched with `--no-ignore-sheets` and saw the
  cover; the 2026-09-08 rubric-effect `grade_from_db` run (attempts
  1175-1236) was not and judged those checks with no evidence — each grade
  log's parameter header records `"ignore_sheets"`. Ignoring is opt-in
  (`--ignore-sheets NAME ...`); `--no-ignore-sheets` is a kept no-op.

### judge v8 — single-pass 8 / template_8 (2026-09-14)

Rows record `judge_version` 8 / `prompt_version` 8 (template unchanged) and are
not comparable to version 7 rows. Cut from the toy-reliability run-1 walkthrough
(19 misses on 18 checks); every further judge change lands here until version 9.

- **Harness verdict for Rounding / Rounded outputs retired** (rulebook v6.6).
  The check covers every final output a reader sees, not only the Questions
  answers, and a number format now counts as rounding, so the judge decides
  it; the harness could reverse a correct judge fail (toy 104). The rounding
  statistics (`n_unrounded`, `rounding_directive`, per-question
  `attempt_rounded`) are still measured and recorded in `answer_check.json`
  and `scored_results.answer_check` for audit. Final calculation accuracy and
  Deliverable completeness keep their harness handling.
- **OVERSIZED RANGE tag** (check 25): the properties block's per-sheet line
  flags a sheet declared at Excel's full width or height whose content ends
  far earlier. Extreme-only by design — a ratio rule flagged 59 of 471 cached
  real workbooks, goldens included. Rendered from stored properties; no cache
  generation bump. Test: `tests_offline/test_oversized_range.py`.
- **Guidance 27 → 36 notes**: edits to checks 16, 49, 55, 64, 82; new notes
  for 2, 11, 25, 37, 65, 66, 67, 104, 107.

### judge v9 — single-pass 9 / template_8 (2026-09-16)

Rows record `judge_version` 9 / `prompt_version` 8 (template unchanged) and are
not comparable to version 8 rows. Cut from the toy-reliability runs 2-3
walkthrough (decisions log `JUDGE_DECISIONS_2026-09-16.md`, implementation
brief `JUDGE_IMPLEMENTATION_BRIEF_2026-09-16.md`, both outside the repo).

- **Checks 37 and 101 retired by rule** (`judge.retired_checks: "37,101"` in
  `project_configs.yaml`; `utils/rubric_suitability.py`). The rubric keeps its
  132-position numbering — toys, suitability annotations and every grading to
  date are keyed by position — so nothing is deleted or renumbered: the two
  checks are forced `not_applicable` on every task (with or without an
  annotation, and under `JUDGE_SKIP_SUITABILITY=1`), never prompted, never
  scored, and their categories rescale around them exactly as suitability
  gating does. `RETIRED_CHECK_NAMES` pins the (category, name) each number must
  carry; a regenerated rubric that moved them refuses to grade. Recorded per
  grading in `scored_results.rubric_suitability.retired_checks`. `grade_toy.py`
  skips toys for retired checks. Effective rubric: 130 items.
  - **Check 28 No unused formatting retired the same way** (judge v11,
    2026-09-19, folded into 11 because nothing had been graded under it):
    `judge.retired_checks: "28,37,101"`, effective rubric 129 items. The
    colleague's review of the 12 jv9 GUI gradings showed the check cannot be
    graded as served: Excel's Check Performance counts formatting on empty
    cells beyond the last row/column of content, the judge's `styled empty
    cells in used range` count looks only inside the content rectangle, and
    the LeaseorKeys / NestQuest goldens carry ~69k trailing formatted cells
    themselves. 28 is 3.28% of Error Checks (0.43% of the total); the other
    14 Error Checks items rescale by 1.034, the category stays at 13%. The
    suitability annotations in S3 are not edited for a retirement (28 is
    `applicable` in all 101, as 101 still is): the rule overrides them at
    load time, and an annotation that is not an exact 132/132 match refuses
    to grade.
  - **Reversible by config alone.** 28 is meant to return once redefined
    (rubric 10 queue below). Taking a number off `judge.retired_checks`
    un-retires it: its `RETIRED_CHECK_NAMES` pin stays (a pin permits, the
    list decides), and evidence that exists for that check alone is rendered
    again — `workbook_properties.STYLED_EMPTY_CHECK` leaves the `styled empty
    cells in used range` line out of all three properties blocks only while
    28 is on the list (`render_properties_text(..., retired_checks=…)`,
    passed from the grading's provenance). Render-time only: the properties
    JSON always keeps the count, so neither direction needs a schema or CSV
    cache bump. Test: `test_retired_check_evidence_line_follows_the_config`.
- **Evidence flags in the properties block** (`utils/workbook_properties.py`,
  schema 4; CSV caches move to `*_csv_cache_v7`; judge v11 adds schema 5 and
  `*_csv_cache_v8`, see the last bullet; judge v12 schema 6 and `_v9`). Each is rendered per sheet:
  - `WIDE OUTLIER` (check 70): a run of ≥ 2 equal-width columns ≥ 2.5× wider
    than the nearest equal-width run on each side (length ≥ 2, width ≥ 4 so
    spacers do not count). Lone columns are never compared; Instructions /
    Questions sheets are skipped. Render-time from stored widths.
  - `period series` (checks 108/123/124/125): every run of ≥ 3 period labels
    (years, dates, `Qn YYYY`, `Mon-YY`, `FYyyyy`; plain numeric years only as
    a monotonic run) in a row, with `PERIOD SERIES OUT OF ORDER`, `unlabeled
    gap at …` (blank header over a value-bearing column inside the run),
    `VERTICAL PERIOD SERIES` (down rows on a sheet that has a horizontal
    timeline, only when the cells beside are formulas — typed-input registers
    stay silent), and `content continues N rows past the "End Sheet" marker`.
    Extraction-time; needs the data-only workbook.
  - `content fit` (check 69): from the display strings the extractor already
    renders — `NUMERIC exceeds width (would render ###)` is the one problem
    label (number at least 1.5 characters wider than its column; the golden
    sweep put two goldens' borderline cells inside that band). Text wider
    than its column beside a non-empty neighbour is cut off on screen and is
    served as information with examples — 43 of 98 goldens carry such
    header labels, so the rubric decides, not the flag. Text overflowing
    into an empty neighbour is counted as normal. Width estimate is coarse
    (character classes of the default font, font size and bold scaling;
    numbers are measured in their own face since judge v12);
    wrapped, shrink-to-fit, centre-across-selection, merged and
    General-format cells are excluded.
  - **Column-width bug fixed in the extractor** while calibrating: a `<col
    min=9 max=308>` run wider than 200 columns was collapsed to its first
    column, so every timeline column of a wide model was served at the
    default width (8.43 instead of, say, 12.9). Widths served since judge v6
    were wrong for those sheets; v7 caches carry the corrected runs.
  - `formulas with a typed date-like string literal` (checks 2/10/81):
    `"12.12.2028"`, `"2028-12-12"`, `"12/12/2028"` inside formula text.
  - `IMPLICIT INTERSECTION` (check 22; judge v11, 2026-09-18,
    `utils/implicit_intersection.py`): plain formulas (no `t="array"`
    marker) that use a multi-cell range as a single value — operand of an
    operator or argument of a single-value function such as ABS/ROUND —
    from a cell outside that range. Excel evaluates them by implicit
    intersection and shows #VALUE!; LibreOffice and the Python engines that
    write the cached values do block arithmetic instead, so the served
    number looks fine (grading 1075, `Sens_Engine!D448`, cached 4.9e-08).
    The walker's context rules were probed against Excel for Mac 16.112 on
    166 formulas (0 false flags; the probe set is the test fixture). Unprobed
    constructs (OFFSET, CHOOSE, TRANSPOSE, names, tables, IFERROR-wrapped
    expressions, dynamic-array functions) are never flagged. Goldens: 0 of
    101 flagged; the 12 jv9 GUI gradings: only 1075/D448. Guidance note on
    check 22 tells the judge to fail on the listed cells only.
    Test: `tests_offline/test_implicit_intersection.py`.
  - `data validation` line (check 44; judge v11): every validation now
    renders its full rule and error-alert state — `D7 whole between 1 and
    50 — alert OFF (any entry accepted)` — under a per-sheet tally ("13
    (error alert OFF for ALL …)"). Before, the line showed only the cell,
    the type and the first bound, and the alert flag was never extracted;
    8 of the 12 jv9 GUI attempts had every validation alert-off (both
    vendors) and the judge passed 7 of them. Guidance note on 44: alert-off
    equals absent; key inputs are drivers, not data blocks. Goldens: 4 of
    101 carry validation, all alert-on. Rubric 10 queue: add "with the
    error alert enabled" to the check text.
    Test: `tests_offline/test_data_validation_alert.py`.
  - `[actual …]` tag in the cell views (check 66; judge v11,
    `excel_utils._actual_value_tag`): a cell that displays like zero (0,
    0.00, (0.00), -0.0, 0.00%) while its value is not zero is served as
    `0.00 [actual 3.64e-12]` in both the full and the data view — when the
    value is a floating-point leftover (below 1e-6) or the cell carries a
    dash format (a true zero would have shown the dash). Ordinary small
    numbers rounded away by the display (0.0025 as 0.00 under #,##0.00)
    are not tagged: they would have added 102k tags across 36 goldens with
    nothing to decide. The judge
    was shown the same "0.00" for a floating-point leftover under a correct
    dash format as for a true zero and failed check 66 on 5 of the 12 jv9
    GUI gradings for leftovers (1075, 1078, 1083, 1084, 1085). Exact zeros
    are untouched; the tag is added after the width-fit measurement and
    the hidden-by-format test, so no other evidence changes. Guidance 66
    extended: a tagged cell is never a zero. The four reviewed goldens use
    no dash format at all (thousands of true zeros shown as 0.00), which
    the rubric as written fails — task-creator item.
    Test: `tests_offline/test_actual_value_tag.py`.
  - `rounding statements` line (check 105; judge v11): each sheet's typed
    rounding statements ("USD, rounded to $0.01") beside the number of
    formulas on that sheet using ROUND/ROUNDUP/ROUNDDOWN/MROUND. the maintainer's
    ruling 2026-09-18: a "rounded to" label over figures that are only
    displayed to that precision misdescribes the model (the colleague's
    point on 1084 `Owning model!B3` and 1085 `Owning!B4`); "shown to" /
    "displayed to" or "carried unrounded" is the accurate wording. Display
    precision stays acceptable for Rounded outputs (104). The House
    Standards prescribed the "rounded to" wording until the same day: v1
    was amended in place, version unchanged (`house_standards/README.md`,
    Amendments), so attempts built under the earlier text fail 105 for
    following it. Rubric 10 queue: 105 reads "rounded or shown to", label
    must match what the model does.
    Test: `tests_offline/test_rounding_statements.py`.
  - `freeze panes` extent (check 122; judge v11): the detail line now reads
    `freeze panes: A35 (34 rows ≈ 660 pt frozen, 0 columns) EXCESSIVE: …`.
    The judge was shown only the cell and never cited an excessive freeze:
    of the 12 jv9 GUI attempts four lock 306-660 pt of rows (1085 Checks
    A35, 1078 Checks D25, 1086 Assumption A26, 1083 Owning/Renting E18)
    and all passed or failed for missing panes only. Thresholds
    (`FREEZE_MAX_ROWS_PT` 300, `FREEZE_MAX_COLS_CHARS` 200; the column
    threshold was 130 until the golden sweep found three goldens freezing
    141-156 characters of label columns) apply at render
    time from the stored extent; hidden rows are not counted. Guidance note
    on 122: an EXCESSIVE tag fails the check; no tag, no fail for excess.
    Test: `tests_offline/test_freeze_extent.py`.
  - Check 50 note extended (judge v11, text only): grading 1084 failed
    Consistent color coding (52) on `Checks!D6, D20:D29` — black where the
    identical formulas beside them are green — and passed Green font for
    cross-sheet links (50) one tool call earlier. The judge had read the
    cells; it booked the slip once. The note now keeps the two checks in
    step. Across the 12 jv9 GUI attempts only 1084 has a black cell whose
    identical neighbouring formula is green; larger black blocks elsewhere
    are calculations those models colour black on purpose. A stricter rule
    (any black formula naming another sheet fails 50) was considered and
    shelved: it fails every golden and needs the House Standards colour
    line to say so first.
  - Check 88 note (judge v11, text only): grading 1084 passed a "sense
    check" (`Checks!B38:F42`) whose three benchmarks are links to the very
    assumptions that drive the compared figures (3.0% appreciation vs the
    3% growth input, to 1e-8), while 1085 — same agent, same pattern,
    "no external market benchmark is assumed" — and 1086 were failed.
    the maintainer's ruling 2026-09-19: an input or assumption that is part of the
    model is never a valid benchmark, and one isolated outside comparison
    among unchecked key outputs does not pass. 1075 (typed EV/EBITDA, P/E,
    WACC ranges) and 1076 (peer percentiles) are the passing shape. The
    four reviewed goldens carry no sense-check section at all.
  Golden/toy-Pass sweep counts are in the session notes for 2026-09-16.
  Tests: `tests_offline/test_evidence_flags_v9.py`.
- **Guidance 36 → 42 notes**: replaced 50, 70, 126; extended 2, 4, 55, 66,
  82; new 7, 33, 108, 112, 115, 129; general block gains "similarity to the
  solution is never evidence".
- Rubric 10 queue (not implemented): 115 and 129 rewritten so clearly
  distinguishable sections / a visible single-formula roll-forward are
  acceptable; 70's "Bad" line led by "wider than content requires"; 28 No
  unused formatting redefined before it returns (what counts as unused —
  Excel's Check Performance looks beyond the last content row/column, net of
  the starting file — with evidence to match; the agent-facing build prompts
  still carry the item, as they do 37 and 101).

### judge v12 — single-pass 12 / template_8 (2026-09-20)

Rows record `judge_version` 12 / `prompt_version` 8 (template unchanged) and are
not comparable to version 11 rows. Cut from the colleague's second review: he
re-annotated four of the twelve jv11 GUI gradings (1087 LeaseorKeys, 1091 Bosch,
1092 ApfelInc, 1097 NestQuest) and disagreed on 11 decisions. the maintainer ruled on
each; twelve rows exist under 11, so everything lands here.

- **Numbers are measured in their own font** (check 69; properties schema 6,
  CSV caches `*_csv_cache_v9`). `display_width` measured every face as Calibri
  scaled by size/11, so Arial 10 — whose digits are 7.4 px, the same as
  Calibri 11 — came out 9% narrow: grading 1092's `WACC!C31:C39`
  ("3,276,619.94", column 9.0, `###` in Excel) needed "9.55" characters against
  a 9.0 + 1.5 threshold, nothing was listed, and the note on 69 then forces a
  pass. `workbook_properties.numeric_display_width` sums the glyph widths of
  the cell's own face (`_FACE_EM`, read from the font files with fontTools;
  unknown faces are measured as Calibri, the narrowest common one, so they
  miss rather than flag falsely) and divides by the column-width unit, the
  Normal font's digit in whole pixels (`normal_font`, `column_unit_px`: 7 px
  for Calibri 11, Aptos Narrow 11 and Arial 10 alike). Bold leaves the digits
  of most faces unchanged. The rule and its 1.5-character band are unchanged,
  and text is still measured by `display_width` (that class is not rendered).
  The rebuilt scan lists exactly `C31:C39` on 1092 (`C30` fits).
  Golden sweep: 2 of 101 listed, both on case-given sheets the agents inherit
  (CashNiagara `Assumptions!AN18/BP18`, ### confirmed in Excel; Volkswagen
  `Comp!H11/K11`, 11 characters in a default-width column) — task-creator items.
  Tests: `test_numeric_width_uses_the_cell_font`,
  `test_numeric_fit_end_to_end_arial_10`.
- **IMPLICIT INTERSECTION lists who references the cell** (check 32;
  `implicit_intersection.find_dependents`, one workbook-wide pass that runs
  only when something was flagged). The cached value of a flagged cell looks
  fine, so everything downstream looked fine too: on 1092
  `Sens_Engine!D448` feeds `Checks!D19/F19`, the roll-up
  `Checks!D46 =COUNTIF(F6:F42,FALSE)` skips the erroring row and
  `Summary!D3` stays "OK". The line now ends "referenced by Sensitivity!D94,
  Checks!D19, Checks!F19: these cells show the error in Excel too". Only
  single-cell operands are listed; a range that merely contains the cell is
  not, because whether it passes the error on depends on the function around
  it. New note on 32: a master flag that stays OK while a check row is in
  error fails, and only then (8 of the 10 roll-ups among the twelve attempts
  count failures and would skip an error; a by-design rule was not adopted).
  the maintainer's ruling: no cascade. One formula costs 22 and 32; 23 (No unresolved
  cell warnings) and 31 (Error-check sheet) stand as graded.
  Test: `test_dependents_of_a_flagged_cell_are_listed`.
- **Print estimate per sheet** (check 76; `_print_estimate`, stored as
  `print_estimate`, rendered under the page-breaks line). Excel's automatic
  page breaks are stored nowhere, so the judge had to guess which sheets run
  past a page: 76 flipped on 5 of the 12 attempts between jv9 and jv11, every
  one a workbook with breaks on some long sheets and none on others (1092:
  `Summary`, 133 rows, fit to one page wide, no breaks; the colleague's note
  is the jv9 judge's own fail text). The line gives pages tall and wide — the
  print area, else the used range, from row heights and column widths against
  paper, orientation, margins and the fit or scale settings, with print-title
  rows repeated — and the manual breaks inside it, tagged `MULTI-PAGE, NO
  MANUAL BREAKS` above `PRINT_MULTI_PAGE_MIN` (1.3 pages, the estimate's error
  band). The case's Instructions / Questions sheets are skipped. On the twelve
  attempts the tag falls on exactly the six Fable workbooks and none of the
  six Astra ones, which settles all five flips. New note on 76: decide per
  sheet from the tag; breaks elsewhere do not cover a tagged sheet; inherited
  sheets are not the agent's print setup; no tag, no fail for missing breaks.
  Goldens: 100 of 101 carry the tag, none has a manual break and one has any
  print setup at all — they fail 76 as written (task-creator item).
  Test: `test_print_estimate_tags_multi_page_sheets_without_breaks`.
- **Guidance 49 → 53 notes.** New 76, above. New 111 Best-fit structure: brute-force
  replication of one calculation block where a data table, iterative
  calculation or one parameterised block would do is the articulable better
  alternative (grading 1092: `Sens_Engine!B78:Y445`, ten stacked 33x10 blocks,
  7,639 formulas; the golden needs 54 with iteration and one data table; jv9
  failed it in those words, jv11 passed it, no note existed). New 63 Text
  left-aligned: text with no `halign`
  token IS left-aligned, and a FORMAT field with no `[ref]` is an empty cell
  (grading 1087 booked the `halign:right` of the empty `Assumptions!E26:E28`
  to the text in `D26:D28`); the same `[ref]` sentence joins 64. New 32, above.
  Extended 99: a plausible change scales or shifts a driver; zero, negative,
  blank or out-of-list values of an input that must be positive, and a
  perpetuity growth rate at or above the discount rate, are invalid inputs for
  data validation, not stress cases (five of the ten 99 fails across the 24
  GUI gradings rested on one; explicit-period growth above the discount rate
  stays an ordinary stress). Extended 81: constants that reconcile a labelling
  convention are technical remnants (grading 1097,
  `Assumptions!H45:H61 =(F45-0.01)*$D$83`, the cent between "12,348.01 –
  14,000" labels). Extended 108: a monthly or daily engine of hundreds of
  periods is conventionally vertical and passes either way when consistent
  (same workbooks, same note: jv9 passed all four such attempts, jv11 failed
  three; the goldens of tasks 19, 32 and 38 are vertical).
  Reworded 88: the jv11 note ("the model's own inputs are never a benchmark",
  "a typed bound whose basis is stated") took 88 from 2 to 11 fails of 12, and
  failed 1092 — the workbook this README names as the passing shape — by
  reading the benchmark bounds typed in `Inputs!D56:D63` (source "Judgment")
  as model inputs. the maintainer's ruling: a bound typed for the sense check is an
  outside comparison wherever it sits, and the modeller's judgment is basis
  enough (the rubric says "against intuition"); only an input that DRIVES the
  model is never a benchmark.
- Noise floor measured on the way: of 1,380 paired jv9/jv11 decisions on the
  twelve attempts 99 flipped (7.2%), 53 of them on checks no judge change
  touched (3.8%) — 99 (6 of 12), 76 (5), 26 and 115 (4 each).

### judge v13 — single-pass 13 / template_8 (2026-10-04)

Rows record `judge_version` 13 / `prompt_version` 8 (template unchanged) and are
not comparable to version 12 rows. Cut for the deterministic rubric checks
(the maintainer's rulings of 2026-10-02/04): Python grades the delivered workbook
before the LLM runs and decides 19 checks at scoring. Existing gradings are not
re-scored, so v12 rows keep the LLM's verdicts on those checks.

- **Python-decided checks** (`utils/det_checks.py` runs the graders in
  `judge/detchecks/`, documented in `detchecks/docs/`): Clean Name Manager (29),
  No bright-yellow highlighting (47), Black font for calculations (49), Green
  font for cross-sheet links (50), Red font for external links (51), Consistent
  zoom level (61), Active cell reset to A1 on all sheets (62), Zeros as dashes
  (66), Sufficient column widths (69), Reasonable column widths (70), Reasonable
  row heights (73), No merged cells (74), File extension (.xlsx) (77), Avoid
  volatile functions (80), Avoid whole-column references (87), No hidden sheets
  (92), No hidden rows/columns (93), No white-on-white hiding (94), No external
  links (95). Their verdicts join `harness_verdicts` beside the answer check's
  (`family: "det_checks"`) and are overlaid at the scoring layer exactly like
  Final calculation accuracy since v6: the LLM's verdict stays on the item as
  `llm_*` in `ai_judgement_harness.json`, and `ai_judgement.json` stays the pure
  LLM judgement. The LLM still grades every check, blind to Python (prompt,
  evidence and CSV caches `*_csv_cache_v9` unchanged), so agreement stays
  measurable: `scored_results.accuracy_engine.checks["<Category>/<name>"]`
  carries `engine`, the Python `decision`, `llm_decision`, `agreed`, `live`,
  `counted`, `check_no`, `n_mistakes` and the check's `stats`. A check the LLM
  never recorded is inserted under its rubric number.
- **Recorded only**: No formula errors (22) and Negatives in parentheses (65)
  (the "v3 bucket"; detchecks marks both `Check.live = False`) run and are
  recorded the same way with `engine: "llm"`, `live: false` and
  `fallback_reason: "recorded only; the LLM verdict stands at scoring"`, so the
  LLM's verdict counts. No unresolved cell warnings (23) and Automatic
  calculation mode (100) stay LLM-only (not built).
- **Which checks run**: `det_checks.live` / `det_checks.recorded_only` in
  `project_configs.yaml`, rubric numbers pinned by name in
  `utils/det_checks.DET_CHECK_NAMES` and checked against the rubric and the
  detchecks registry like `judge.retired_checks` (a mismatch refuses to grade),
  then gated per task exactly as the LLM is: rubric suitability (retired checks
  never run) and the effective weights. Red font for external links (51) is
  not applicable to any task today, so it is configured live but graded nowhere
  yet.
- **No fallback**: a check that cannot grade the file raises
  (`detchecks.errors.GradingError`, re-raised as `utils.det_checks.DetChecksError`
  naming every failing check by title and the file). The checks run after the
  answer check, outside its score-neutral `try` and before the LLM, so the
  attempt fails the way the formula-cache refusal does: `grade_from_db` logs
  `FAILED`, returns `success: False`, writes no DB row and the batch continues,
  with no API spend. `det_checks.json` (status `error`, the failures) stays in
  the task folder. `judge.py --single-pass` raises.
- **Recalculation**: structure and styles come from the delivered
  `ai_attempt.xlsx` (never `temp_recalculated/`); formula values from an
  Excel-saved file's own caches, otherwise from a LibreOffice recalculation of
  the delivered file (`paths.libreoffice_path`, private profile, threaded
  calculation off, `det_checks.libreoffice_timeout_seconds` 600) written to
  `<task folder>/det_checks_recalc/` and deleted by `prune_workbook_copies`.
  Excel recalculation is off (`det_checks.excel_recalc: false`); cells
  LibreOffice cannot compute are used as displayed. LibreOffice never runs on a
  file over `det_checks.libreoffice_max_mb` (10 MB, memory): such a file, when it
  was not saved by Excel and a value check applies, fails the grading before the
  LLM call ("not graded: too large"). Excel-saved files of any size are graded.
  A LibreOffice process left on the attempt's private profile is killed when the
  checks return or raise. Cost: no API spend; the Python pass took ~3 s median
  (38 s worst) per attempt in the 374-file sanity run, plus the LibreOffice run
  (seconds to minutes) for a file not saved by Excel.
- **Task metadata**: File extension (.xlsx) (77) judges the delivered file name
  from the `_attempt_origin.json` sidecar (`original_filename`); without it the
  check raises, so a local folder needs the sidecar. No external links (95) runs
  with `requires_external_links: false` (no task requires them).
- **Switches**: `--det-checks harness|llm|off` on `grade_from_db`,
  `grade_with_orchestration`, `grade_toy` and `judge.py --single-pass`; the
  default comes from `det_checks.enabled` (true = `harness`). `harness`: the
  live verdicts count. `llm`: everything runs and is recorded, the LLM's
  verdicts count (a shadow run; `total_score_harness` still shows the v13
  total). `off`: nothing runs. A check that cannot grade fails the attempt in
  `harness` and `llm` alike. `--accuracy-check` keeps deciding the answer check
  alone. `accuracy_engine.effective` is `harness` when every measured live
  verdict counted (`total_score == total_score_harness`), `llm` when none did
  (`total_score == total_score_llm`) and `mixed` when only one family did;
  `accuracy_engine.det_checks_mode` records the switch and each check's
  `counted` whether its Python verdict is in the recorded total. The DB drivers
  refuse to start when the `det_checks` config does not match the rubric
  (`startup_check`), before any download.
- **Artefacts**: `det_checks.json` in the bundle (config, gate, task metadata,
  the full verdicts with the recalculation plan, `code_sha` = a fingerprint of
  the grading code) and `scored_results.det_checks` (status, mode, graded and
  not-applicable numbers, per-check engine / decision / live / n_mistakes); a
  compact copy in `_metadata.json`.
- Not changed: the paper scripts still read `judge_version >= 12`
  (`MIN_JUDGE_VERSION`) and `pass_rule.DETERMINISTIC_CHECKS` lists Final
  calculation accuracy only, so a cohort mixing v12 and v13 rows mixes LLM and
  Python verdicts on these checks.
  Tests: `test_det_checks.py` (the adapter on openpyxl workbooks: live and
  recorded-only verdicts, the gate, the delivered name, JSON safety, the
  LibreOffice policy and size limit, the scoring switches),
  `test_det_checks_single_pass.py` (`grade_single_attempt` end to end on a real
  Excel-saved attempt with a stub LLM, all three switch values, and the
  failure path that stops before the LLM call; the end-to-end case is skipped
  where the corpus attempt is absent, `DETCHECKS_E2E_ATTEMPT` points it at
  another Excel-saved file under 1 MB).

### Latest-prompt guard (2026-09-10)

Both DB drivers refuse to spend on superseded agent prompts. For `--benchmark
v2`, `grade_with_orchestration.py` and `grade_from_db.py --task-ids` keep only
attempts whose `prompt_version` is the pipeline's latest —
`LATEST_PROMPT_VERSION_BY_TYPE` in `utils/misc_utils.py` (gui/excel 205, api
1609, coding_cli 113 as of the House Standards set) — and log what they
dropped. `--all-prompt-versions` grades everything; `--attempt-ids` is always
explicit and never filtered. `scripts/export_good_attempts.py` carries the same
numbers (`LATEST_PV`) and an offline test keeps the two tables in agreement.
Bump the table whenever a pipeline cuts a new prompt version.

## Grade a local task folder (no database, no S3)

This is the path for grading attempts produced outside the MBABench
infrastructure — your own tasks, run through any of the agent pipelines in
local mode. Assemble one folder per attempt:

```text
<folder>/
  ai_attempt.xlsx                   # the agent's workbook, renamed
  _attempt_origin.json              # {"original_filename": "<name as delivered>"}
  solution/<golden>.xlsx            # your golden solution
  starting/starting_workbook.xlsx   # optional: what the agent was given
  context.pdf | context.txt         # optional: case text
  rubric.json | rubric_weights.json # optional, falls back to the benchmark's pair
```

Where each pipeline leaves the agent's workbook in local mode:

| Pipeline | Local output |
|---|---|
| gui-agents (`sink.kind: local`) | `<paths.scratch_dir>/attempts/<ts>_<task>_p<pid>/solutions/*.xlsx` |
| cli-agents (`local_mode: true`) | `<results_dir>/<task folder name>/solution.xlsx` |
| coding-agents (`mode: external`) | `<results_dir>/<task>_<ts>_<pid>/solution.xlsx` |
| excel-agents (`sink.kind: local`) | `<paths.scratch_dir>/attempts/<ts>_<task>/solutions/*.xlsx` |

Then grade it with the same judge the v2 benchmark uses (single-pass,
harness answer check, OpenAI grader called directly — no OpenRouter):

```bash
export OPENAI_API_KEY=sk-...
JUDGE_SKIP_SUITABILITY=1 python judge/main_scripts/judge.py \
    --benchmark v2 --single-pass --model openai/gpt-5.6-sol -f /path/to/<folder>
```

- `--single-pass` is the production v2 judge (one conversation over all 132
  checks). `--agentic` alone is the older 12-category judge (one conversation
  per category); scores from the two are not comparable.
- `JUDGE_SKIP_SUITABILITY=1` is required for tasks outside the MBABench task
  pool: v2 grading otherwise expects a per-task rubric-suitability annotation
  (fetched from S3 by the DB drivers, or placed in the folder as
  `rubric_suitability.json`). Skipping grades every check ungated and records
  that in `scores.json`.
- `--model` takes any label in `judge_identities.yaml`; the provider and
  reasoning effort are pinned there. Add `--reasoning-effort` to override the
  pin, `--nocall` to test extraction without spending, `--run-calculation` to
  recalculate formulas in LibreOffice first (needed when the attempt's cached
  values are missing; the formula-cache gate refuses such workbooks).
- Attempt workbooks must carry cached formula values. Workbooks saved by
  openpyxl without a recalculation step have none — run with
  `--run-calculation` or recalculate them yourself.
- The deterministic checks (judge v13) grade the folder before the LLM:
  `_attempt_origin.json` must name the delivered file (File extension (.xlsx)
  (77) fails the run without it), LibreOffice must be installed for workbooks
  not saved by Excel (at most 10 MB), and `--det-checks llm` / `off` keeps the
  LLM's verdicts counting / skips them.

Results land in `<folder>/judge_results/`: extracted CSVs,
`ai_judgement.json`, `scores.json` (0–100 total and per-category),
`det_checks.json`, and the run log (`answer_check.json` stays in the folder).

## Operation scripts

Every script under `operation_scripts/` that touches the DB or S3 takes
`--benchmark` too, e.g.

```bash
python judge/operation_scripts/get_tasks.py --benchmark v2 11 12
python judge/operation_scripts/list_agent_models.py --benchmark v1
```

## Offline tests

```bash
cd judge
python tests_offline/test_benchmark_presets.py
python tests_offline/test_rubric9_consistency.py
python tests_offline/test_formula_cache.py
python tests_offline/test_single_pass.py
python tests_offline/test_oversized_range.py
python tests_offline/test_det_checks.py               # judge v13 adapter + scoring switches
python tests_offline/test_det_checks_single_pass.py   # judge v13 end to end, stub LLM
for t in detchecks/tests/test_*.py; do python -m detchecks.tests.$(basename $t .py); done
```
