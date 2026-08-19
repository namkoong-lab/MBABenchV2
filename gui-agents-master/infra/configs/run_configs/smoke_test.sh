#!/usr/bin/env bash
# Pipeline smoke test: v2 / Claude Haiku 4.5 / task 2 (BasicGrowth).
#
#   ./infra/configs/run_configs/smoke_test.sh            # dry-run only (default)
#   ./infra/configs/run_configs/smoke_test.sh --real     # actually run it
#
# Run from gui-agents-master/. Chrome must already be up on CDP 9223 and
# logged in to claude.ai — see the launch command below.
set -euo pipefail

CONFIG="infra/configs/run_configs/v2_haiku45_smoke.yaml"
PYTHON="${PYTHON:-$HOME/.uv/uv_venvs/mbabench2/bin/python}"
CDP_PORT=9223

REAL=0
[[ "${1:-}" == "--real" ]] && REAL=1

cd "$(dirname "$0")/../../.."   # -> gui-agents-master/

echo "── 1. preflight: is Chrome listening on CDP ${CDP_PORT}? ──"
if ! curl -sf --max-time 3 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null; then
  cat <<EOF
Chrome is not reachable on CDP port ${CDP_PORT}. Start it with:

  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
    --remote-debugging-port=${CDP_PORT} \\
    --user-data-dir=~/.chrome-web-agent-claude2 \\
    --no-first-run --no-default-browser-check \\
    --disable-background-timer-throttling \\
    --disable-backgrounding-occluded-windows \\
    --disable-renderer-backgrounding \\
    '--remote-allow-origins=*'

Then log in to claude.ai in that window and re-run this script.
EOF
  exit 1
fi
echo "   OK: $(curl -s "http://127.0.0.1:${CDP_PORT}/json/version" | head -c 120)"
echo

echo "── 2. offline config checks ──"
"$PYTHON" tests/test_run_config_offline.py
echo

echo "── 3. dry-run (resolves credentials + DB + S3, runs no browser) ──"
# Exit 3 = "no tasks matched" — a filtered no-op, not success. Catch it here
# rather than discovering it after starting the browser.
set +e
"$PYTHON" -m infra.run --run-config "$CONFIG" --dry-run
rc=$?
set -e
if [[ $rc -eq 3 ]]; then
  echo "Dry-run matched 0 tasks — check source.filters in $CONFIG." >&2
  exit 1
elif [[ $rc -ne 0 ]]; then
  echo "Dry-run failed (exit $rc)." >&2
  exit $rc
fi
echo

if [[ $REAL -eq 0 ]]; then
  cat <<EOF
── dry-run clean ──
Re-run with --real to execute for real. That will:
  * drive Chrome on CDP ${CDP_PORT} for up to ~1h (30 min/task, 2 attempts)
  * upload the solution + logs to s3://mbabench/MBABenchV2/attempts/claude_haiku_4_5/
  * INSERT one row into task_attempts as agent_model_name='claude_haiku_4_5'
EOF
  exit 0
fi

echo "── 4. real run ──"
# -y skips the interactive confirmation; the dry-run above already showed
# exactly which task will run.
"$PYTHON" -m infra.run --run-config "$CONFIG" -y
rc=$?

echo
case $rc in
  0) echo "PASS — task succeeded, attempt recorded." ;;
  1) echo "FAIL — task ran but the attempt failed (check the quality gate / engine log)." ;;
  2) echo "FAIL — config or preflight error; nothing was attempted." ;;
  3) echo "FAIL — source matched no tasks; nothing was attempted." ;;
  4) echo "FAIL — environment blocked (CDP lock held, or auth precheck failed)." ;;
  *) echo "FAIL — unexpected exit $rc" ;;
esac
exit $rc
