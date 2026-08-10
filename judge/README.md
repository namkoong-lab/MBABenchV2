# Judge — Quickstart

LLM-based grader for Excel-task attempts. This README covers the minimum setup
to run a single local grading on `judge/scratch/test_cases/Bread_And_Butter`.
For deeper internals see [CLAUDE.md](CLAUDE.md).

## Prerequisites

- Python 3.12.12
- [uv](https://docs.astral.sh/uv/) (`pipx install uv` or `brew install uv`)
- LibreOffice (only if you pass `--run-calculation`). On macOS the default
  install path is `/Applications/LibreOffice.app/Contents/MacOS/soffice`,
  which matches `paths.libreoffice_path` in
  [project_configs.yaml](project_configs.yaml).

## 1. Install

From the repo root:

```bash
bash judge/setups/setup.sh
```

This creates `.venv/` at the project root with Python 3.12.12, installs the
local `excel_judge` package (editable), and pulls
[setups/requirements.txt](setups/requirements.txt).

Activate it:

```bash
source .venv/bin/activate
```

## 2. Configure environment

[project_configs.sh](project_configs.sh) exports the API keys, DB URL,
`SCRATCH_PATH`, and `LIBREOFFICE_PATH` that the judge reads. Source it once
per shell (every CLI script assumes these are set):

```bash
source judge/project_configs.sh
```

Defaults like the model, prompt template, rubric, and char limits live in
[project_configs.yaml](project_configs.yaml) and are loaded into env vars by
`utils/misc_utils.load_project_configs()` at import time.

## 3. Verify the task folder

The judge expects this layout (already present in the sample case):

```
judge/scratch/test_cases/Bread_And_Butter/
  ai_attempt.xlsx
  solution/<solution>.xlsx
  context.pdf | context.txt        # optional
  rubric.json | rubric_weights.json # optional, falls back to defaults
```

Quick check:

```bash
ls judge/scratch/test_cases/Bread_And_Butter
```

## 4. Run the judge

```bash
python judge/main_scripts/judge.py -f judge/scratch/test_cases/Bread_And_Butter
```

Relative paths are resolved from the project root (see
[main_scripts/judge.py:3201](main_scripts/judge.py#L3201)).

Useful flags:

- `--agentic` — multi-turn tool-calling judge instead of the staged single-shot
- `--nocall` — skip the LLM call (file extraction only; good for a smoke test)
- `--run-calculation` — recalc formulas with LibreOffice before CSV extraction
- `--model <openrouter-model>` — override `JUDGE_MODEL`
- `--reasoning-effort {none,minimal,low,medium,high}` — thinking budget

Run `python judge/main_scripts/judge.py --help` for the full list.

## 5. Output

Results land in `judge/scratch/test_cases/Bread_And_Butter/judge_results/`:

- `<workbook_stem>/<sheet>_full.csv` + `<sheet>_additional_format.txt` —
  extracted CSVs the judge actually saw
- `judgement_*.json` — raw model judgement per category
- `scores.json` — weighted per-category and final 0–100 score
- run log (also streamed to terminal because `miscs.log_to_terminal: true`)
