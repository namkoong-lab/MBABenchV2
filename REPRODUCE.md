# Reproducing the SpreadsheetSmith experiments

The experiments can be recreated from this repository plus one download: the
repository holds the 101 task rows, every prompt and rubric, and the code that
ran the agents and the judge; the tasks'
starting files and golden solutions come from a zip you download by hand (step
2 below). No database and no object store are needed. What you bring is
model-provider access for the cohorts you want to re-run.

Two things can be reproduced, from cheaper to more expensive:

1. **The judge** — grade your own attempts. An API key for the judge model;
   about $3.5 per attempt.
2. **The agents** — re-run a cohort over the 101 tasks and grade the result.
   Provider keys or, for the browser cohorts, paid consumer accounts.

## Prerequisites

| Needed for | What |
| --- | --- |
| everything | Python 3.12+, [uv](https://docs.astral.sh/uv/), git |
| CLI pipeline, judge | LibreOffice (`soffice`) for formula recalculation |
| coding pipeline | Docker (one sandbox container per attempt) |
| GUI and Excel pipelines | Chrome, a paid consumer account with the provider (claude.ai, chatgpt.com, Excel for the web with the vendor add-in) and a signed-in browser profile |
| any model call | the provider's API key in `config/config.yaml` (`keys.*`) |

## Set up

1. Environment and config:

```bash
./setup.sh                                   # creates .venv and installs the workspace
cp config/config.yaml.example config/config.yaml
$EDITOR config/config.yaml                   # fill in the keys you need; leave database/aws unset
```

2. Task files. Open <https://anonymous-hf.com/a/v410gr4w0zf8/> in a browser
   and click **Download ZIP** near the top of the page (383 MB, lands in
   `~/Downloads/v410gr4w0zf8.zip`; the page sits behind a browser check, so
   it cannot be fetched by script). Then:

```bash
uv run python scripts/install_task_files.py ~/Downloads/v410gr4w0zf8.zip   # copies the 202 workbooks into data/tasks/, checks sha256
uv run python scripts/verify_offline_bundle.py   # hashes, task completeness, manifests, prompt versions
```

The installer never overwrites an existing workbook (`--force` to), skips the
zip's `task.json` copies, and exits non-zero while any workbook is missing or
differs from `data/MANIFEST.json`; the verifier fails with a pointer to this
step until the workbooks are in place.

With no database url configured every pipeline and the judge run offline:
tasks come from `data/tasks/`, results go to `outputs/`. `data/README.md`
describes the bundle and the `outputs/` layout.

## 1. Run the judge

The judge grades an attempt's workbook against the task's golden solution with
the rubric in `judge/prompts/rubrics/rubric_9.json`, judge version 12
(`judge/project_configs.yaml`). Offline it reads attempts from every `outputs/**/task_attempts.jsonl`
and writes `outputs/gradings/gradings.jsonl` plus one folder per grading.

```bash
J=judge/main_scripts/grade_from_db.py
# every attempt under outputs/ whose workbook is on disk (your own runs, section 2)
uv run python $J --benchmark v2 --single-pass --source local --sink local --all-local \
    --model openai/gpt-5.6-sol --accuracy-check harness
```

`--attempt-ids <id>` grades one attempt instead of all of them.
Add `--dry-run` to any of these to see the resolved files and paths without a
model call. The leaderboard judge is GPT-5.6 Sol at effort none; the recorded
gradings averaged $3.55 and 2.2 minutes per attempt. The second judge (stage 3)
is Claude Fable 5.1 at $11.6 per attempt.

## 2. Re-run a cohort

One config per cohort, named after its `agent_model_name` label (a `/` in the
label becomes `__` in the file name). Every config pins benchmark v2, the
identity, the effort, the prompt or template version the cohort ran with, the
House Standards attachment, the timeouts, and tasks 1–101; the identities
themselves live in each pipeline's identity yaml. Attempts land in
`outputs/<label>/`; re-launching a config skips tasks that already have a row.

| Cohort | Pipeline | Command |
| --- | --- | --- |
| CLI Fable 5.1 | cli | `cd cli-agents-master && uv run excel-agent --batch-config examples/offline/openpyxl_anthropic__claude-fable-5-1-max.yaml` |
| CLI Astra | cli | `… --batch-config examples/offline/openpyxl_openai__gpt-6-astra-xhigh.yaml` |
| CLI Grok 4.6 | cli | `… --batch-config examples/offline/openpyxl_tensorblock__grok-4.6-xhigh.yaml` |
| CLI Kimi K3 | cli | `… --batch-config examples/offline/openpyxl_tensorblock__kimi-k3-max.yaml` |
| CLI Gemini 3.8 Flash | cli | `… --batch-config examples/offline/openpyxl_tensorblock__gemini-3.8-flash-high.yaml` |
| CLI Qwen 3.8 max | cli | `… --batch-config examples/offline/openpyxl_tensorblock__qwen3.8-max-xhigh.yaml` |
| CLI GLM 5.3 max | cli | `… --batch-config examples/offline/openpyxl_tensorblock__glm-5.3-max.yaml` |
| Coding Fable 5.1 (max) | coding | `cd coding-agents-master && uv run python -m coding_agent.run_sweep --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-max.yaml` |
| Coding Fable 5.1 high (stage 4) | coding | `… --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-high.yaml` |
| Coding Fable 5.1 low (stage 4) | coding | `… --config run_configs/offline/claudecode_anthropic__claude-fable-5-1-low.yaml` |
| Coding Opus 5 max | coding | `… --config run_configs/offline/claudecode_anthropic__claude-opus-5-max.yaml` (the recorded cohort ran 65 tasks through the Codex route, `codex_tensorblock__claude-opus-5-max.yaml`) |
| Coding Astra | coding | `… --config run_configs/offline/codex_openai__gpt-6-astra-xhigh.yaml` |
| Coding Gemini 3.8 Flash | coding | `… --config run_configs/offline/codex_tensorblock__gemini-3.8-flash-high.yaml` |
| Coding Grok 4.6 | coding | `… --config run_configs/offline/codex_tensorblock__grok-4.6-xhigh.yaml` |
| Coding Kimi K3 | coding | `… --config run_configs/offline/codex_tensorblock__kimi-k3-max.yaml` |
| Coding Qwen 3.8 max | coding | `… --config run_configs/offline/codex_tensorblock__qwen3.8-max-xhigh.yaml` |
| Coding GLM 5.3 max | coding | `… --config run_configs/offline/codex_tensorblock__glm-5.3-max.yaml` |
| Coding Fable 5.1 max, stage 5 rubric inlined (template v14) | coding | `… --config run_configs/offline/codex_tensorblock__claude-fable-5-1-max_v14.yaml` |
| Coding Fable 5.1 max, stage 5 no standards (template v15) | coding | `… --config run_configs/offline/codex_tensorblock__claude-fable-5-1-max_v15.yaml` |
| GUI Fable 5.1 (work mode) | gui | `cd gui-agents-master && uv run python -m infra.run --run-config infra/configs/run_configs/offline/claude_fable_5_1_cowork_max.yaml` |
| GUI Astra (work mode) | gui | `… --run-config infra/configs/run_configs/offline/chatgpt_gpt_6_astra_work_ultra.yaml` |
| GUI Opus 5 (work mode) | gui | `… --run-config infra/configs/run_configs/offline/claude_opus_5_cowork_max.yaml` |
| GUI chat GPT-6 Pro | gui | `… --run-config infra/configs/run_configs/offline/chatgpt_gpt_6_pro.yaml` |
| Excel Fable 5.1 | excel | `cd excel-agents-master && uv run python -m infra.run --run-config infra/configs/run_configs/offline/claude_excel_fable_5_1.yaml` |
| Excel Sol 5.6 | excel | `… --run-config infra/configs/run_configs/offline/chatgpt_excel_gpt_5_6_sol_xhigh.yaml` |
| Excel Opus 5 | excel | `… --run-config infra/configs/run_configs/offline/claude_excel_opus_5.yaml` |

`…` stands for the same command as the first row of that pipeline. The GUI and
Excel pipelines first need Chrome started on the CDP port the config names and
signed in to the provider account (see `CheatSheet.md`); add `-y` to run for
real. Each pipeline has a dry run (`--dry-run`) that prints the resolved
prompt, attachments and output paths for a task without calling a model, and
each pipeline's README describes its offline path in detail.

## Verification without spending

```bash
uv run python scripts/verify_offline_bundle.py
(cd cli-agents-master && uv run pytest -o addopts="" tests)   # 3 known failures in test_multi_actions
uv run pytest coding-agents-master/tests
uv run pytest gui-agents-master/tests                          # one package per invocation: the GUI and
uv run pytest excel-agents-master/tests                        # Excel suites share module names
for t in judge/tests_offline/*.py; do uv run python "$t"; done
```

Parity tests (`test_offline_*`) in each package build the model input from the
bundle and from a saved database row and assert byte equality, and check that
an offline attempt row has exactly the database columns.

## Known limitations

* **Browser cohorts need accounts.** The GUI and Excel pipelines drive a real
  Chrome signed in to a paid consumer account; providers enforce weekly usage
  caps, so a 101-task cohort takes several accounts or several weeks.
* **Provider-side change.** Consumer products change their UI and retire
  models without notice; a cohort whose model is no longer offered cannot be
  re-run, only re-graded.
* **Costs.** The API cohorts above cost from a few hundred to several thousand
  dollars each at the recorded effort levels.

## Cloud profile (optional)

The maintainers' path keeps a Postgres database and an object store. Setting
`database.v2_url` and `aws.*` in `config/config.yaml` switches every pipeline
back to it; nothing in the offline path is lost. `scripts/export_benchmark_data.py`
regenerates this bundle from it, read-only.
