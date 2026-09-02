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
pins the endpoint (openrouter | gemini | anthropic | openai), the wire model
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
  solution are censused from the extracted CSVs; a workbook at or above
  `judge.uncached_formula_max_ratio` (default 0.5) refuses before any API call.
  Fix with `--run-calculation`, or set `JUDGE_SKIP_FORMULA_CACHE_CHECK=1` to
  grade anyway — the skip and the per-workbook counts are recorded in
  `scored_results.formula_cache`. Enforced in `_prepare_case`, so it applies to
  every mode (classic and agentic alike, and both benchmarks).
- **Runs the score-neutral answer check** (`utils/answer_check.py`): the
  Questions-sheet answers of attempt vs golden solution, compared with
  tolerance `|a-b| <= max(1e-9, 1e-6*max(|a|,|b|))`; full artifact
  `answer_check.json` rides with the raw files, summary in
  `scored_results.answer_check`. Never affects the 0-100 score. Side-by-side
  view: `operation_scripts/report_answer_check.py`.
- **Serves category-keyed context views** (template 5): extraction writes a
  format-stripped `<sheet>_data.csv` beside every `<sheet>_full.csv`;
  `read_file` serves the data view except in Formatting, and attaches
  merged-cells/frozen-panes metadata once per sheet in Formatting and
  Structure. Listings show dimensions only, and the per-category user
  message keeps static blocks first so consecutive categories share a
  prompt-cache prefix. CSV caches live in the `*_csv_cache_v2` generation.

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
  standard. Tolerance = one unit of the requested precision (a penny at two
  decimal places), sign flips accepted only on outflow rows (expense/cost/
  spend/outflow/depreciation/amortization/capex/tax), percent and fraction
  forms equal, values not rendered strings, sentinel synonyms, dates, zero
  forms; unit-scale (x1000) differences are flagged, never accepted.
- **Name-agnostic answer finder**: sheets are validated by the golden
  question TEXTS they contain (named Questions*/Answers* sheets win ties,
  decoys like "Answer Map" lose), rows are paired by text, the answer column
  is the header cell starting with "answer" in the block's header row.
- **Workbook properties block** (`utils/workbook_properties.py`,
  `_workbook_properties.json` beside the CSVs; caches move to
  `*_csv_cache_v3`): true tab order (file listings now follow it), hidden
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
  all) and shares the `*_csv_cache_v2` generation with grade_from_db.

## Grade a local task folder

```bash
python judge/main_scripts/judge.py --benchmark v1 -f judge/scratch/test_cases/Bread_And_Butter
```

Expected layout:

```text
<folder>/
  ai_attempt.xlsx
  solution/<solution>.xlsx
  context.pdf | context.txt         # optional
  rubric.json | rubric_weights.json # optional, falls back to the benchmark's pair
```

Results land in `<folder>/judge_results/`: extracted CSVs, per-category
`judgement_*.json`, `scores.json`, and the run log.

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
```
