# Data stores: the database and the object store

Every DB-backed run in this repository reads tasks from a Postgres database and
an S3 bucket, writes attempts back to both, and the judge writes gradings the
same way. This page lists what the code expects to find there, so that a
deployment with its own Postgres and S3 can be set up. Nothing here is specific
to a hosting provider: the maintainers use Neon for Postgres and AWS S3, but any
Postgres 14+ and any S3-compatible bucket work.

Two benchmarks share the layout, each with its own database and S3 root:

| | `v1` | `v2` |
|---|---|---|
| database name | `BizbenchV1` | `MBABenchV2` |
| S3 root | `s3://<bucket>/BizbenchV1/` | `s3://<bucket>/MBABenchV2/` |
| config key | `database.v1_url` | `database.v2_url` |

Every pipeline checks that the database named in the connection string matches
the benchmark it was asked to run (`benchmark: v1|v2`), and refuses otherwise.
The `v2` database has two columns the `v1` database lacks
(`task_attempts.extra_configs`, `gradings.grader_reasoning`); the code probes
for them and skips them when absent.

## Tables

Column types are as the code uses them; `JSON` columns hold JSON text or
`jsonb`. `id` columns are serial primary keys; `created_at` defaults to now().

### `tasks` (read-only for every pipeline)

| column | type | meaning |
|---|---|---|
| `id` | integer | task id, what `--task-id` / `--task-ids` refer to |
| `task_name` | varchar(512) | folder name of the task's files in S3 |
| `task_source` | varchar(100) | provenance label; also an S3 path segment |
| `task_starting_files` | JSON | list of `s3://` URIs: the files handed to the agent (one workbook plus any PDFs/text) |
| `task_solution_files` | JSON | list of `s3://` URIs: the golden solution workbook (plus optional context files) |
| `deprecated` | boolean (nullable) | true = excluded from every run and every report |
| `deprecated_reason` | text | |
| `human_difficulty_measure` | text | v2 only, optional: `Easy`, `Medium`, `Medium-Hard`, `Hard` |
| `case_classification` | jsonb | v2 only, optional: task taxonomy |
| `ai_time_estimate_min` | numeric | v2 only, optional |
| `created_at`, `updated_at` | timestamptz | |

### `task_attempts` (one row per recorded attempt)

| column | type | meaning |
|---|---|---|
| `id` | integer | attempt id, what the judge's `--attempt-ids` refers to |
| `task_id` | integer, FK `tasks.id` | |
| `agent_model_name` | varchar(512) | the cohort label from the pipeline's identity registry |
| `agent_model_type` | varchar(128) | `gui`, `excel`, `api` (CLI harness) or `coding_cli` |
| `prompt_version` | integer | the prompt set that was sent (per-pipeline numbering) |
| `prompt_files` | JSON | list of `s3://` URIs: the prompt text as sent, plus attachments |
| `attempt_files` | JSON | list of `s3://` URIs; the first Excel file is the one the judge grades |
| `start_time`, `end_time` | timestamptz | |
| `time_taken_min` | double | |
| `cost` | double (nullable) | USD, when the pipeline can measure it |
| `agent_failed` | boolean | true = the agent ran but produced no valid workbook |
| `agent_failed_reason` | text | |
| `deprecated` | boolean | true = excluded from grading and reports |
| `deprecated_reason` | text | |
| `context_reduced` | boolean (nullable) | CLI harness: the context ladder had to shrink the workbook view |
| `extra_configs` | jsonb | **v2 only.** The identity's pinned settings, the sandbox image, `house_standards {version, file, sha256}` and other provenance the pipeline records |
| `created_at`, `updated_at` | timestamptz | |

### `gradings` (one row per judge run over an attempt)

| column | type | meaning |
|---|---|---|
| `id` | integer | grading id |
| `task_id`, `attempt_id` | integer | |
| `grader_model` | text | the judge label from `judge/judge_identities.yaml` |
| `grader_prompts`, `grader_response` | text | the prompt sent and the LLM's raw judgement |
| `grader_reasoning` | text | **v2 only.** The model's reasoning summary when the API returns one |
| `accuracy_grade`, `formula_grade`, `format_grade` | double | the three v1 category scores (v2 rows carry their 12 categories inside `scored_results`) |
| `rubric_version`, `rubric_weight_version` | text | `8` / `6` for v1, `9` / `9` for v2 |
| `prompt_version` | text | the judge template version (`7.0` classic, `8` single-pass) |
| `judge_version` | integer | the judge pipeline version (`single_pass.version` in `judge/project_configs.yaml`) |
| `agentic_mode` | boolean | true for the agentic and single-pass judges |
| `scored_results` | jsonb | per-check verdicts (`check_scores`, each item with `grader: deterministic|llm`), category scores, `total_score` (0-100), `accuracy_engine`, `det_checks`, `answer_check`, `formula_cache`, `rubric_suitability` |
| `time_elapsed_min`, `cost` | double | |
| `raw_files_path` | text | `s3://.../grading/<run>/` folder holding the full bundle |
| `raw_files` | JSON | the files in that folder |
| `errors_encountered` | text | |
| `failed`, `failed_reason` | boolean, text | a grading that stopped (parse failure, refusal) |
| `deprecated`, `deprecated_reason` | boolean, text | |
| `solution_context_reduced`, `attempt_context_reduced`, `context_reduced_details` | boolean, boolean, text | the classic judge had to shorten its evidence |
| `created_at` | timestamptz | |

### `judge_annotations` (human review of gradings)

Written by `judge-annotator/` (`grading_id`, `attempt_id`, `annotator`,
`annotator_id`, `revision`, `labels` as jsonb `"Category::Check" -> TP|FP|TN|FN`,
`s3_key` of the full annotation JSON under `annotations/`, `created_at`).
Optional: only the maintainers' analysis scripts under `operation/` read it.

## Object-store layout

All keys sit under the benchmark's root (`MBABenchV2/` or `BizbenchV1/`).

```text
<root>/tasks/<task_name>/starting_files/<file>      what tasks.task_starting_files points at
<root>/tasks/<task_name>/solution_files/<file>      what tasks.task_solution_files points at
<root>/rubric_suitability/task_id=<id>/<...>.json   v2 only: per-task rubric applicability
                                                    annotations the judge gates checks with
<root>/attempts/<agent_folder>/task_source=<src>/task_id=<id>/<ts>_<file>
                                                    every attempt's files (GUI, Excel, coding);
                                                    the CLI harness uses <model>_openpyxl as the
                                                    agent folder
<root>/prompts/<agent_folder>/<ts>_<file>           the prompt snapshot an attempt was sent
<root>/grading/<run>/...                            each grading's bundle (gradings.raw_files_path)
annotations/grading_id=<id>/<user>_<ts>.json        judge-annotator output (bucket root)
```

The bucket name comes from `aws.s3_bucket` in `config/config.yaml` (default
`mbabench`); credentials from `aws.access_key_id` / `aws.secret_access_key` or
boto3's default chain.

## Loading your own tasks

A task needs one `tasks` row and its files in S3. The starting workbook of a v2
task carries a `Questions` sheet (questions in column A from row 2, a column
headed `Answers` left blank for the agent, a `Unit` column where given); the
judge's deterministic answer check compares the attempt's `Answers` column to
the golden's, so a task without that sheet is graded by the LLM alone. v2
grading also expects a rubric-suitability annotation per task under
`rubric_suitability/`; without one, grade with `JUDGE_SKIP_SUITABILITY=1`
(every check applies).

The `tasks` table is never written by this repository's code. Insert rows with
your own SQL or tooling; `judge/operation_scripts/get_tasks.py` downloads a
task's files back from S3 for inspection.
