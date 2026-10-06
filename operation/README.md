# operation/ — result tables and paper figures

Scripts that read the benchmark database and turn gradings into the tables and
figures of the paper. None of them is needed to run an experiment: the
pipelines record attempts and the judge records gradings on their own. All of
them connect through `config/config.yaml` (`database.v1_url` / `database.v2_url`)
with plain read-only SELECTs, and write under `operation/results/` (gitignored,
one timestamped folder per run where a script produces data files).

```text
operation/
├── v1/                         the closed BizbenchV1 wave
│   ├── get_results.py          one row per (cohort × task): the attempt that represents the
│   │                           cohort and its grading; imputed cohort means; difficulty breakdown
│   └── paper_scripts/          plot_main.py (rubric + stacked-score figure),
│                               plot_performance_vs_time.py (score and time vs difficulty)
└── v2/                         the current MBABenchV2 task set
    ├── rubrics.csv             the 129 live rubric items with category, weight and Importance tier
    ├── style_guide.yaml        colours, type and print sizing shared by every v2 figure
    └── paper_scripts/
        ├── pass_rule.py        THE pass rule: per-check verdicts -> pass/fail per task, pass rate
        │                       per cohort (shared by every script below)
        ├── pass_rule_all.py    every pass-rule-dependent number in the paper, recomputed
        ├── plot_leaderboard.py pass-rate leaderboard of every graded cohort
        ├── plot_accuracy_by_difficulty.py
        │                       final-answer accuracy on the harder tasks; accuracy and time
        │                       against difficulty; agent time against the expert estimate
        ├── plot_difficulty.py  difficulty distribution (the paper's half-column figure)
        ├── plot_difficulty_and_types.py
        │                       superseded figure; keeps the style helpers the others import
        ├── plot_task_coverage.py
        │                       task-type coverage against prior spreadsheet benchmarks
        ├── plot_solve_time.py  expert solve-time distribution (reads the task-analysis JSON)
        ├── plot_rubric_map.py  treemap of the rubric items by weight (reads rubrics.csv only)
        ├── plot_judge_agreement.py
        │                       judge vs human annotation per category and attempt
        ├── calc_judge_stat.py  judge error rates against the human annotations
        └── additional_experiments/
            ├── _common.py      shared loading for the stage scripts
            ├── stage2.py       judge self-consistency (90 attempts graded three times)
            ├── stage3.py       does the ranking persist under a second, costlier judge?
            └── stage4.py       thinking-effort ablation (low / high / max)
```

Selection rule shared by the v2 scripts: for each (cohort, task) the newest
clean grading by the current judge (`judge_version >= MIN_JUDGE_VERSION` in the
script, not failed, not deprecated, attempt and task not deprecated). Each
script's docstring names its inputs, flags and output files; run them from the
repository root, for example:

```bash
uv run python operation/v2/paper_scripts/plot_leaderboard.py
uv run python operation/v1/get_results.py --database v1
```

`rubrics.csv` and `style_guide.yaml` are inputs, not outputs: edit the style
guide to restyle every figure at once. The stage scripts read a consolidated
gradings JSON under `operation/results/v2/` that the maintainers export; without
it they stop with a message naming the file.
