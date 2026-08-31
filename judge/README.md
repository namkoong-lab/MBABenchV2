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
```
