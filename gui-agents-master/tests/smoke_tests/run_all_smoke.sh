#!/usr/bin/env bash
# Run all smoke tests sequentially
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  Running all smoke tests"
echo "========================================"
echo ""

"$SCRIPT_DIR/run_smoke_claude.sh"
echo ""
echo "----------------------------------------"
echo ""

"$SCRIPT_DIR/run_smoke_chatgpt.sh"
echo ""
echo "========================================"
echo "  All smoke tests complete"
echo "========================================"
