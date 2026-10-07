# operation/ — maintainers' analysis scripts

Scripts that read the benchmark database (`database.v1_url` / `database.v2_url`
in `config/config.yaml`, read-only SELECTs) and turn gradings into result tables
and figures. They are not part of reproducing a run: the pipelines record
attempts and the judge records gradings on their own. Outputs go under
`operation/results/` (gitignored). Each script's docstring names its inputs,
flags and output files; run from the repository root, e.g.
`uv run python operation/v2/paper_scripts/plot_leaderboard.py`.
