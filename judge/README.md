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
editable and the `config` module). LibreOffice is only needed for
`--run-calculation`.

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
  `*_csv_cache_v8`, see the last bullet). Each is rendered per sheet:
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
    (character classes of the default font, font size and bold scaling);
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
    formulas on that sheet using ROUND/ROUNDUP/ROUNDDOWN/MROUND. Patrick's
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
    Patrick's ruling 2026-09-19: an input or assumption that is part of the
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

### Latest-prompt guard (2026-09-10)

Both DB drivers refuse to spend on superseded agent prompts. For `--benchmark
v2`, `grade_with_orchestration.py` and `grade_from_db.py --task-ids` keep only
attempts whose `prompt_version` is the pipeline's latest —
`LATEST_PROMPT_VERSION_BY_TYPE` in `utils/misc_utils.py` (gui/excel 205, api
1509, coding_cli 113 as of the House Standards set) — and log what they
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

Results land in `<folder>/judge_results/`: extracted CSVs,
`ai_judgement.json`, `scores.json` (0–100 total and per-category),
`answer_check.json`, and the run log.

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
```
