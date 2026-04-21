#!/usr/bin/env bash
# Run the bundled ChatGPT sample task end-to-end.
#
# Always launches `python -m infra.run` from the gui-agents-master/ directory
# so relative paths in upload_files (e.g. "data/sample/...") resolve correctly.
# Any extra CLI args are forwarded to infra.run (e.g. --dry-run, -y).
#
# Prereqs:
#   1. Edit infra/configs/run_configs/local_run_examples/sample_task_chatgpt.yaml
#      and replace the placeholder chatgpt_web.project_id / project_slug with
#      the values from your https://chatgpt.com/g/g-p-{id}-{slug}/project URL.
#   2. Have Chrome running on the ChatGPT CDP port (default 9333) with a
#      logged-in chatgpt.com session.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

exec \
  python -m infra.run --run-config infra/configs/run_configs/local_run_examples/sample_task_chatgpt.yaml \
  "$@"
