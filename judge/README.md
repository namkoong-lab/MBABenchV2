# Judge

The grader for Excel-task attempts. It stages an attempt's workbook, the task's
golden solution and starting workbook, runs deterministic Python checks, then
holds one LLM conversation over every applicable check of the rubric and writes
a scored `gradings` row with the full evidence bundle. It grades either
benchmark; `--benchmark v1|v2` is the only switch.

Current production judge: **judge v14** (`single_pass.version` in
[project_configs.yaml](project_configs.yaml)), single-pass mode, template
`prompts/agentic_judge_template_8.yaml`, rubric `prompts/rubrics/rubric_9.json`.
What changed in every earlier version is in [HISTORY.md](HISTORY.md).

## Layout

```text
main_scripts/
  grade_from_db.py             Grade attempts by id or by task from the database (the production driver).
  grade_with_orchestration.py  Same judge over a (tasks x cohorts) grid, N workers, one DB writer.
  judge.py                     The judge itself; also a CLI for grading a local folder with no database.
utils/
  judge_identity.py            --model labels -> provider, wire model id, effort (judge_identities.yaml)
  misc_utils.py                BENCHMARKS (db name, S3 root, rubric pair), project_configs -> env vars
  answer_check.py, answer_rules.py   deterministic Questions-sheet answer check + equivalence rulebook
  det_checks.py                adapter running judge/detchecks/ before the LLM; scoring overlay
  excel_utils.py, workbook_properties.py, theme_palette.py, sheet_extent.py, ...
                               workbook -> the CSV and properties evidence the LLM reads
  formula_cache.py             refuses workbooks whose formulas were never calculated
  rubric_suitability.py        per-task applicability gating + retired checks
  rubric_guidance.py           judge-only scope notes rendered under each check
  anthropic_native.py, openai_responses.py, llm_utils.py   provider clients, caching, cost
  trajectory.py                per-call request/response capture uploaded with the grading
detchecks/                     the deterministic rubric checks (own docs/, tests/, CHANGELOG_core.md)
prompts/                       judge templates and rubrics (append-only; see "Prompts and rubrics")
alternate_answers.yaml         second accepted answers for some Questions-sheet questions (judge v14)
judge_identities.yaml          the grader registry
project_configs.yaml           judge settings: versions, limits, deterministic-check switches
operation_scripts/             DB/S3 utilities (download tasks, list cohorts, reports)
tests_offline/                 offline tests (each file runs on its own: python tests_offline/<file>.py)
```

## Configuration

Two files, nothing to copy or edit per run:

- **`<repo>/config/config.yaml`** (gitignored, shared by every pipeline):
  `database.v1_url` / `database.v2_url`, `aws.*` (S3 bucket and credentials),
  `keys.openai_api_key` / `anthropic_api_key` / `openrouter_api_key` /
  `gemini_api_key` / `forge_api_key`, and optionally `libreoffice_path`.
  Environment variables (`OPENAI_API_KEY`, ...) win over `keys.*`;
  `DATABASE_URL` is only a fallback when the config has no URL for the benchmark.
- **[project_configs.yaml](project_configs.yaml)** (tracked, secret-free):
  benchmark-agnostic judge settings. `utils/misc_utils.load_project_configs()`
  loads every key into `BIZBENCHJUDGE_<SECTION>_<KEY>` environment variables at
  startup (the file always wins; set values in the file, not in the shell).
  - `judge.*`: the classic 3-stage judge (v1), the formula-cache threshold,
    `retired_checks`, the default grader label, context limits.
  - `agentic_judge.*`: the 12-category agentic judge (frozen at version 4).
  - `single_pass.*`: the production judge (version 14, template 8).
  - `det_checks.*`: which rubric numbers Python decides (`live`) or only
    records (`recorded_only`), and the LibreOffice memory guard.
  - `paths.libreoffice_path`: `null` = find `soffice` (LIBREOFFICE_PATH, then
    `libreoffice_path` in `config/config.yaml`, then PATH, then LibreOffice.app).

`--benchmark` selects the rest from `BENCHMARKS` in
[utils/misc_utils.py](utils/misc_utils.py): the database, the S3 grading root
(`BizbenchV1/grading` vs `MBABenchV2/grading`) and the rubric pair plus category
order (v1 = rubric_8 / rubric_6_weights, 3 categories; v2 = rubric_9 /
rubric_9_weights, 12 categories). A URL naming the other benchmark's database is
refused at startup (`JUDGE_SKIP_BENCHMARK_GUARD=1` bypasses, for one-off
experiments only).

**Grader labels.** `--model` takes a label from
[judge_identities.yaml](judge_identities.yaml), which pins the endpoint
(`openai` | `anthropic` | `openrouter` | `gemini` | `tensorblock`), the wire
model id and the default reasoning effort; the label is stored verbatim in
`gradings.grader_model`. An unregistered label refuses to run and prints the
stanza to add. Add a stanza, never edit one that has graded rows. The v2
leaderboard was graded with `openai/gpt-5.6-sol`; the config default
(`judge.default_grader`) is `google/gemini-2.5-pro`, so pass `--model`
explicitly for production runs. `--reasoning-effort` overrides the pinned effort.

## Install

From the repo root, `./setup.sh` (uv workspace; installs the judge's
dependencies and the shared `config` module). LibreOffice is needed for
`--run-calculation`, for the answer check's recalculation and for the
deterministic checks on attempts not saved by Excel.

## Grade attempts from the database

```bash
# v2, the production judge: deterministic checks, harness answer check, one LLM conversation
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --attempt-ids 123 124 --model openai/gpt-5.6-sol

# every valid attempt of some tasks (latest prompt version only)
uv run python judge/main_scripts/grade_from_db.py --benchmark v2 --single-pass --task-ids 4 5 --model openai/gpt-5.6-sol

# v1: the classic 3-stage judge
uv run python judge/main_scripts/grade_from_db.py --benchmark v1 --attempt-ids 1 2 3

# a (tasks x cohorts) grid, four workers
uv run python judge/main_scripts/grade_with_orchestration.py --benchmark v2 --single-pass --all-tasks \
    --models claude_fable_5_1_cowork_max chatgpt_gpt_6_astra_work_ultra --workers 4 --model openai/gpt-5.6-sol
```

v2 must be graded in single-pass mode (`--single-pass`; the older `--agentic`
12-category judge is frozen at version 4 and the classic judge's template
hardcodes the three v1 categories).

Flags shared by both drivers:

| flag | meaning |
|---|---|
| `--benchmark v1\|v2` | database, S3 root and rubric pair |
| `--model LABEL` | grader label from `judge_identities.yaml` |
| `--single-pass` / `--agentic` / `--no-agentic` | the production v2 judge / the frozen 12-category judge / the classic v1 judge |
| `--run-calculation` | recalculate the attempt in LibreOffice before extracting evidence (needed for workbooks saved without cached values, and for legacy `.xls` deliveries) |
| `--det-checks harness\|llm\|off` | the deterministic checks decide / run but only record / do not run (default from `det_checks.enabled`) |
| `--accuracy-check harness\|llm` | whether the deterministic answer check or the LLM decides Final calculation accuracy (default `harness`; both are always recorded) |
| `--ignore-sheets NAME ...` | drop sheets from the LLM's evidence (default none; Python always grades the whole workbook) |
| `--reasoning-effort none\|...\|max` | override the grader's pinned effort |
| `--dry-run`, `--nocall`, `--no-db-write`, `--no-s3-upload` | preview the selection / skip the API / skip the DB row / keep the bundle local |

`grade_from_db` only: `--attempt-ids` or `--task-ids` (one required;
`--task-ids` keeps attempts on the pipeline's latest prompt version unless
`--all-prompt-versions`), `--keep-workbook-copies`, `--cache-cap-gb`.
`grade_with_orchestration` only: `--task-ids` / `--all-tasks`, `--models`
(cohort labels), `--all-models`, `--workers`, `--no-dedup` (grade every valid
attempt instead of the latest per task, model and prompt version).

Neither driver skips attempts that already have a grading: a relaunch grades
them again. Both refuse to start when `det_checks.*` disagrees with the rubric
or the identity is unregistered, before any download.

### The latest-prompt guard

For `--benchmark v2`, `grade_with_orchestration.py` and `grade_from_db.py
--task-ids` keep only attempts whose `prompt_version` is the pipeline's latest:
`LATEST_PROMPT_VERSION_BY_TYPE` in `utils/misc_utils.py` (gui/excel 205, api
1609, coding_cli 113) and log what they dropped. `--all-prompt-versions` grades
everything; `--attempt-ids` is always explicit and never filtered.
`scripts/export_good_attempts.py` carries the same numbers and an offline test
keeps the two tables in agreement. Bump the table whenever a pipeline cuts a new
prompt version.

### Where files go

Each run gets `judge/scratch/grade_runs/<run_id>/` with one task folder per
attempt (`<task>__task_<id>__attempt_<id>__agent_model=<label>/`) holding the
staged workbooks, the extracted CSVs, every artefact below and `run.log`;
`run_summary.json` lists the outcomes. Extracted evidence is cached across runs
under `judge/scratch/grade_cache/<db name>/{solution,attempt,starting}_csv_cache_v9/`
(the generation number changes whenever the evidence format changes; older
generations are left untouched) and suitability annotations under
`.../rubric_suitability/`. The attempt's copies are deleted once its grade is in
the database and its bundle in S3 (`--keep-workbook-copies` keeps them).

## What a grading contains

The bundle uploaded to `s3://<bucket>/<root>/grading/<run>/...` (and recorded
in `gradings.raw_files_path` / `raw_files`):

- `ai_judgement.json`: the LLM's verdict per check, as it judged;
  `ai_judgement_harness.json`: the same with the deterministic verdicts overlaid
  (the LLM's kept as `llm_*`). `scores.json`: the 0-100 total and the twelve
  category scores.
- `answer_check.json`: the Questions-sheet comparison, question by question.
- `det_checks.json`: the deterministic checks' full verdicts, the recalculation
  plan and `code_sha` (a fingerprint of the grading code).
- `_workbook_properties.json`, `_metadata.json`, `_attempt_origin.json`,
  `rubric_suitability.json`: the evidence served and its provenance.
- `trajectory.jsonl.gz`: every model call's request and response.
- the extracted CSVs of attempt, solution and starting workbook.

In the database, `gradings.scored_results` carries `check_scores` (every item
with its `grader`: `deterministic` or `llm`), `criteria_scores`, `total_score`,
`accuracy_engine` (both engines' verdicts and totals, agreement per check),
`det_checks`, `answer_check`, `formula_cache` and `rubric_suitability`;
`grader_response` stays the pure LLM judgement.

## How the v2 judge grades (judge v14)

1. **Gate by task.** The latest complete rubric-suitability annotation for the
   task (`s3://<bucket>/MBABenchV2/rubric_suitability/`) marks checks
   `not_applicable`; they are never prompted or scored and weights renormalise
   within the category. Checks 28, 37 and 101 are retired by configuration on
   every task (`judge.retired_checks`). A v2 grading without an annotation
   refuses (`JUDGE_SKIP_SUITABILITY=1` grades every check, recorded as such).
2. **Refuse uncalculated workbooks.** The judge reads cached formula results;
   an attempt (or golden) with half or more of its formulas uncached
   (`judge.uncached_formula_max_ratio`) refuses before any API call. Fix with
   `--run-calculation`; `JUDGE_SKIP_FORMULA_CACHE_CHECK=1` grades anyway and
   records it.
3. **Deterministic checks first** (`utils/det_checks.py` over
   [detchecks/](detchecks/), documented in `detchecks/docs/`). Python grades
   the delivered workbook on 19 checks (name manager, colour conventions, zoom,
   active cell, zeros as dashes, column widths, row heights, merged cells, file
   extension, volatile functions, whole-column references, hidden sheets, rows
   and columns, white-on-white text, external links) and records two more (No
   formula errors, Negatives in parentheses) without deciding them. Formula
   values of a file not saved by Excel come from a LibreOffice recalculation
   (one LibreOffice at a time machine-wide, waiting for free memory, retried
   with doubled timeouts; a run that still fails lists the attempt for re-run).
   A check that cannot grade the file fails the grading loudly before the LLM;
   where a check once had to give up, it now decides by a documented default
   recorded in its `stats.defaults`.
4. **Deterministic answer check** (`utils/answer_check.py`, rulebook
   `utils/answer_rules.py` v6.7). The attempt's `Questions`-sheet answers are
   compared to the golden's under the equivalence rules (tolerance, rounding to
   the golden's stated precision, sign flips on outflow rows, percent forms,
   dates, sentinels) and to the task author's alternate accepted values in
   [alternate_answers.yaml](alternate_answers.yaml). A numeric constant typed
   where the golden has a formula is `hardcoded` and counts as a mistake. The
   verdict is overlaid on Final calculation accuracy (and the zero-answers case
   of Deliverable completeness) at scoring; the LLM is not told.
5. **One LLM conversation** over every applicable check, globally numbered 1-132
   in rubric order (template 8). The judge reads the attempt, the golden and
   the starting workbook through tools (`read_file` with data / formatting /
   structure views; a workbook-properties block per sheet with tab order,
   hidden state, validation, widths, print estimate, evidence flags such as
   `IMPLICIT INTERSECTION`, `PERIOD SERIES OUT OF ORDER`, `MULTI-PAGE, NO
   MANUAL BREAKS`). Judge-only scope notes
   (`prompts/rubrics/rubric_9_guidance.yaml`) render under the checks they
   qualify. Absence of evidence is not a pass. Round budget
   `single_pass.max_rounds` plus `max_forced_rounds` forced-finalisation rounds.
6. **Scoring.** Category scores from the per-check verdicts and
   `rubric_9_weights.json`; the deterministic verdicts replace the LLM's where
   they are live and counted. `accuracy_engine.effective` records whether the
   recorded total is `harness`, `llm` or `mixed`.

Rows record `judge_version` 14 and `prompt_version` 8 and are not comparable to
other versions; [HISTORY.md](HISTORY.md) says what each version changed.

## Grade a local task folder (no database, no S3)

For attempts produced outside the benchmark stores: your own tasks run through
any pipeline's local mode, or a workbook you want to check. Assemble one folder
per attempt:

```text
<folder>/
  ai_attempt.xlsx                   # the agent's workbook, renamed
  _attempt_origin.json              # {"original_filename": "<name as delivered>"} (for the file-extension check)
  solution/<golden>.xlsx            # your golden solution
  starting/starting_workbook.xlsx   # optional: what the agent was given
  context.pdf | context.txt         # optional: case text
  rubric.json | rubric_weights.json # optional, falls back to the benchmark's pair
  rubric_suitability.json           # optional: a per-task applicability annotation (else JUDGE_SKIP_SUITABILITY=1)
```

Where each pipeline leaves the agent's workbook in local mode:

| pipeline | local output |
|---|---|
| gui-agents (`sink.kind: local`) | `<paths.scratch_dir>/attempts/<ts>_<task>_p<pid>/solutions/*.xlsx` |
| cli-agents (`local_mode: true`) | `<results_dir>/<task folder name>/solution.xlsx` |
| coding-agents (`mode: external`) | `<results_dir>/<task>_<ts>_<pid>/solution.xlsx` |
| excel-agents (`sink.kind: local`) | `<paths.scratch_dir>/attempts/<ts>_<task>/solutions/*.xlsx` |

Then grade it with the same judge the v2 benchmark uses:

```bash
export OPENAI_API_KEY=sk-...
JUDGE_SKIP_SUITABILITY=1 uv run python judge/main_scripts/judge.py \
    --benchmark v2 --single-pass --model openai/gpt-5.6-sol -f /path/to/<folder>
```

- `JUDGE_SKIP_SUITABILITY=1` is required for tasks outside the v2 task pool
  (there is no annotation for them); every check is graded and `scores.json`
  records the skip.
- `--run-calculation` recalculates in LibreOffice first: required when the
  attempt was saved without cached values (openpyxl output, for example) and for
  a legacy `.xls` delivery. `--nocall` tests extraction without spending.
- `--det-checks llm` / `off` keeps the LLM's verdicts counting / skips the
  deterministic checks; `--accuracy-check llm` does the same for the answer check.

Results land in `<folder>/judge_results/`: the extracted CSVs,
`ai_judgement.json`, `ai_judgement_harness.json`, `scores.json`,
`det_checks.json` and the run log (`answer_check.json` stays in the folder).

## Prompts and rubrics

`prompts/` is append-only. Live today: `agentic_judge_template_8.yaml` (the
single-pass judge, v2), `judge_template_7_0.yaml` (the classic judge, v1),
`rubrics/rubric_9.json` + `rubric_9_weights.json` + `rubric_9_guidance.yaml`
(v2), `rubrics/rubric_8.json` + `rubric_6_weights.json` (v1). Everything else
(`agentic_judge_template_1..7`, `judge_template_6_3/6_4`, `rubric_7.json`,
`agentic_judge_template_6.yaml` for the frozen 12-category judge) is the text
earlier gradings were produced with and stays for that reason.

`rubric_9.json` is frozen: it was generated from the task author's checklist
workbook together with the agent-facing prompt text, and offline tests pin the
two against each other (`tests_offline/test_rubric9_consistency.py`). Scope
rules the judge needs that agents must not see go in `rubric_9_guidance.yaml`,
never in the rubric or the agent prompts.

## Operation scripts

Every script under `operation_scripts/` that touches the database or S3 takes
`--benchmark`:

| script | what it does |
|---|---|
| `get_tasks.py TASK_ID ...` | download a task's starting and solution files from S3 |
| `get_tasks_and_attempts.py TASK_ID ... --models LABEL ...` | the same plus the attempts of the named cohorts |
| `list_agent_models.py` | distinct `agent_model_name` values in `task_attempts` |
| `check_attempt_completion.py` | which cohorts have a valid attempt for which tasks, with the reason for invalid ones |
| `report_accuracy_engine.py` | per grading: recorded total beside the LLM and harness totals, `mixed` explained |
| `extract_csv.py FILE.xlsx` | extract one workbook's CSVs with the judge's extractor (no DB) |
| `validate_judge_trajectory.py` | check a grading's `trajectory.jsonl(.gz)` is a complete record (no DB) |

## Tests

Offline; each file runs on its own and prints `OK`/`FAIL` per test (they are
also pytest-collectable one file at a time):

```bash
cd judge
for t in tests_offline/*.py; do python "$t"; done
python -m pytest detchecks/tests          # the deterministic checks; the one real-LibreOffice test skips itself when soffice is absent
```

`tests_offline/` covers the benchmark presets, the identity registry, rubric
consistency, the formula-cache gate, the answer rules, the evidence flags, the
deterministic-check adapter end to end with a stub LLM (`test_det_checks*.py`),
and the single-pass driver's wiring. `detchecks/tests/` covers every check and
the recalculation pipeline.
