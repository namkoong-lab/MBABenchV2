#!/usr/bin/env bash
# Smoke Test: Claude.ai web agent
# Expected: SUCCESS, Excel workbook downloaded with test sheets
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Smoke Test: Claude.ai Web Agent ==="
echo "Task: Smoke_Test (no file uploads)"
echo "Expected: SUCCESS on first attempt"
echo ""

.venv/bin/python claude_web_batch_runner.py \
    --tasks tests/smoke_tests/smoke_test_tasks.yaml \
    --template tests/smoke_tests/template_smoke_claude.yaml
