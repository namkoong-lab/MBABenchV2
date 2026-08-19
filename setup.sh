#!/usr/bin/env bash
# setup.sh — set up the unified MBABenchV2 Python environment.
#
# Everything is driven by uv: the root pyproject.toml declares the workspace
# (judge/, cli-agents-master/, coding-agents-master/, gui-agents-master/) and
# uv.lock pins the exact resolution. `uv sync` creates the environment,
# installs the locked dependencies, and installs every workspace member in
# editable mode.
#
# The environment location comes from config/config.yaml (read through the
# bash config loader in config/bash/), falling back to config_default.yaml:
#
#   venv_path: null              -> <repo root>/.venv   (default)
#   venv_path: ~/envs/mbabench   -> that path
#
# An existing environment at that path is reused and updated in place; a
# missing one is created. Relative paths resolve against the repo root.
#
# Run it from anywhere; it resolves the repo root from its own location.
#
#   ./setup.sh              # sync the env (adds dev tools by default)
#   ./setup.sh --no-dev     # runtime dependencies only
#
# To change a dependency, edit the relevant pyproject.toml and re-run this
# script (or `uv sync`) — uv.lock updates automatically. Do not pip-install
# into the environment by hand; the next sync will revert it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Error: uv is not installed, and it now drives this project's environment.

Install it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
EOF
    exit 1
fi

# ---------------------------------------------------------------------------
# Read venv_path from the two-tiered config.
# ---------------------------------------------------------------------------
# shellcheck source=config/bash/config.sh
source "$REPO_ROOT/config/bash/config.sh"

# config_load seeds config/config.yaml from the defaults on first run and
# resolves ${env:VAR} references. Setup needs none of those secrets, so the
# "env var not set" warnings are dropped while every other diagnostic (missing
# defaults file, unfilled __REQUIRED__ fields) still reaches the terminal.
# config_load silently recreates config.yaml from the defaults when it is
# missing, which yields a file full of unresolved ${env:...} placeholders that
# looks valid but has none of your real settings. Notice that here and say so,
# rather than letting a half-configured run start.
_cfg_user_file="$REPO_ROOT/config/config.yaml"
_cfg_was_missing=0
[[ -f "$_cfg_user_file" ]] || _cfg_was_missing=1

_cfg_err="$(mktemp)"
trap 'rm -f "$_cfg_err"' EXIT
if ! config_load "$REPO_ROOT/config" 2>"$_cfg_err"; then
    grep -v 'env var not set' "$_cfg_err" >&2 || true
    echo "Error: could not load config/config.yaml — see above." >&2
    exit 1
fi
grep -v 'env var not set' "$_cfg_err" >&2 || true

if [[ "$_cfg_was_missing" == "1" ]]; then
    cat >&2 <<EOF

  ****************************************************************
  NOTE: config/config.yaml did not exist and has just been created
  from config/config_default.yaml. It contains placeholders, not
  your real settings — database URLs, keys and venv_path are all
  back at their defaults.
  Edit config/config.yaml to fill in your real values for the project.
    $_cfg_user_file
  ****************************************************************

EOF
fi

VENV_PATH="$(config get venv_path 2>/dev/null || true)"

# The YAML loader hands back an unset value as the literal string "null".
case "${VENV_PATH:-}" in
    ""|null|Null|NULL|"~"|none|None|NONE)
        VENV_PATH="$REPO_ROOT/.venv" ;;
    "~/"*)
        VENV_PATH="$HOME/${VENV_PATH#\~/}" ;;
    /*)
        ;;                                  # already absolute
    *)
        VENV_PATH="$REPO_ROOT/$VENV_PATH" ;;  # relative to the repo root
esac

# uv resolves the project environment from this, so `uv sync` and every later
# `uv run` in this script land in the same place.
export UV_PROJECT_ENVIRONMENT="$VENV_PATH"

if [[ -x "$VENV_PATH/bin/python" ]]; then
    echo "Reusing the existing environment at $VENV_PATH"
else
    echo "Creating a new environment at $VENV_PATH"
fi

echo "Syncing the MBABenchV2 workspace from $REPO_ROOT ..."
uv sync "$@"

# gui-agents drives a real Chrome over CDP for normal runs, but the
# claude_web_agent "classic" fallback calls playwright's own launch(), which
# needs the bundled browser. Cheap to install and a no-op once present.
echo "Ensuring the Playwright Chromium build is present ..."
uv run playwright install chromium

config destroy

cat <<EOF

Done. $VENV_PATH now runs judge/, scripts/, operation/,
and the cli/gui/coding agents.

Use it with source $VENV_PATH/bin/activate

To put the environment somewhere else, set venv_path in config/config.yaml
and re-run this script.
EOF
