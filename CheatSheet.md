# Cheatsheet


## Running judges

Decide V1 or V2. Use project_configs_v2.yaml or project_configs_v1.yaml

Then run AWS_PROFILE=mbabench JUDGE_OPENROUTER_MODELS=google/gemini-2.5-pro python judge/main_scripts/grade_from_db.py --attempt-ids x

## Running Claude GUI

- Setup Chrome
```
 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \D
  --remote-debugging-port=9223 \
  --user-data-dir=~/.chrome-web-agent-claude2 \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

- Run a benchmark. No config swapping — the run config's `benchmark:` key
  selects the database (`database.v1_url` / `database.v2_url` in
  `config/config.yaml`), the schema, and the S3 prefix together.

```
cd gui-agents-master
python -m infra.run --run-config infra/configs/run_configs/v2_fable5_claude.yaml --dry-run
# drop --dry-run and add -y to run for real
```

  Available run configs: `v1_fable5_claude_cowork`, `v2_fable5_claude`,
  `v1_sol56_chatgpt_work`, `v2_sol56_chatgpt`. Edit `source.filters.task_ids`
  in the one you pick. The run logs which database it resolved, e.g.
  `Database: MBABenchV2 (from config/config.yaml database.v2_url)` — check
  that line before letting a long run proceed.
