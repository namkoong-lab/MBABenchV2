#!/usr/bin/env bash
# setup.sh — set up the BizbenchV2 Python environment.
#
# Installs the runtime dependencies (setup/requirements.txt) and then the
# project itself in editable mode (pip install -e .), which exposes the vendored
# `config` module for import. Run it from anywhere; it resolves the repo root
# from its own location.
set -euo pipefail

# Repo root is one level up from this script's directory (setup/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"

echo "Installing BizbenchV2 dependencies from $REPO_ROOT ..."
# Prefer uv if it's available (faster, matches the freeze workflow), else pip.
if command -v uv >/dev/null 2>&1; then
    uv pip install -r setup/requirements.txt
    uv pip install -e .
else
    "$PYTHON" -m pip install -r setup/requirements.txt
    "$PYTHON" -m pip install -e .
fi

cat <<'EOF'

Done. The `config` module and the scripts are ready.

Next steps:
  1. Set your secrets (referenced from config/config_default.yaml via ${env:VAR}):
       export DATABASE_URL=...
       export GEMINI_API_KEY=...
  2. Confirm AWS access:  aws sts get-caller-identity
  3. Run a script, e.g.:  python scripts/estimate_task_times.py --dry-run --limit 1
EOF
