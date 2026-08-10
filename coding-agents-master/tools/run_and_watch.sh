#!/bin/bash
# Run one attempt and stream a live human-readable view of the agent working.
# Usage: bash tools/run_and_watch.sh [config] [--task-dir DIR | --task-id N]
set -e
cd "$(dirname "$0")/.."
CONFIG="${1:-run_configs/demo_watch_claude.yaml}"; shift || true
export PATH="$PATH:/usr/local/bin"
ENV=/Users/patrick/BizBench/MBABench-main/cli-agents-master/.env
export ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' $ENV | cut -d= -f2- | tr -d '"')"
export OPENAI_API_KEY="$(grep '^OPENAI_API_KEY=' $ENV | cut -d= -f2- | tr -d '"')"
BEFORE=$(ls -d workspaces/* 2>/dev/null | sort)
.venv/bin/python -m coding_agent.run_task --config "$CONFIG" ${@:---task-dir ladder/task_rung0 --results-dir ladder/results} &
RUNNER=$!
NEW=""
while [ -z "$NEW" ]; do
  sleep 1
  NEW=$(comm -13 <(echo "$BEFORE") <(ls -d workspaces/* 2>/dev/null | sort) | head -1)
done
echo "== watching $NEW =="
python3 tools/watch_transcript.py "$NEW" --follow &
WATCHER=$!
wait $RUNNER
sleep 1
kill $WATCHER 2>/dev/null
echo; echo "== attempt finished; artifacts in $NEW =="
