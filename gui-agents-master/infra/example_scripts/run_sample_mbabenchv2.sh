#!/usr/bin/env bash
# Run the MBABenchV2 sample end-to-end: pull task(s) from the MBABenchV2 Postgres
# DB + S3, hand them to the ChatGPT agent.
#
# Always launches `python -m infra.run` from the gui-agents-master/ directory
# so relative paths resolve correctly. Any extra CLI args are forwarded to
# infra.run (e.g. --dry-run, -y).
#
# Prereqs:
#   1. Set database.v2_url and aws.* in <repo>/config/config.yaml (the run
#      config's `benchmark: v2` selects the v2 url), and in
#      infra/configs/configs.yaml set chatgpt_web.project_id / project_slug
#      from your https://chatgpt.com/g/g-p-{id}-{slug}/project URL.
#   2. AWS credentials resolvable by boto3 (config.yaml aws.*, ~/.aws/credentials
#      or AWS_* env vars) with GetObject permission on the task bucket.
#   3. Have Chrome running on the ChatGPT CDP port (default 9333) with a
#      logged-in chatgpt.com session.
#   4. (Optional) Edit infra/configs/run_configs/mbabenchv2_run_examples/sample_mbabenchv2.yaml
#      to change the filters (task_ids / task_sources).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

exec \
  python -m infra.run --run-config infra/configs/run_configs/mbabenchv2_run_examples/sample_mbabenchv2.yaml \
  "$@"
