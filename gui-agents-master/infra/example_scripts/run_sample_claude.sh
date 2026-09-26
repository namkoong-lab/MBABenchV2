#!/usr/bin/env bash
# Run the bundled Claude sample task end-to-end.
#
# Always launches `python -m infra.run` from the gui-agents-master/ directory
# so relative paths in upload_files (e.g. "data/sample/...") resolve correctly.
# Any extra CLI args are forwarded to infra.run (e.g. --dry-run, -y).
#
# Prereqs:
#   1. Have Chrome running on the Claude CDP port (default 9222) with a
#      logged-in claude.ai session — Playwright attaches to that browser
#      rather than launching a fresh one, so cookies persist between runs.
#   2. (Optional) To pin runs to a specific Claude project, set
#      claude_web.project_id in infra/configs/configs.yaml or in
#      infra/configs/run_configs/local_run_examples/sample_task.yaml;
#      otherwise the agent uses the default chat (no project scope).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

exec python -m infra.run \
  --run-config infra/configs/run_configs/local_run_examples/sample_task.yaml \
  "$@"
