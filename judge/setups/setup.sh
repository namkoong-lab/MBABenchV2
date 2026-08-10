# Setup Environment

## Set VENV_PATH here, or leave empty to use default (<project_root>/.venv)
VENV_PATH=""

## Resolve VENV_PATH
if [ -z "$VENV_PATH" ]; then
    VENV_PATH=$(cd "$(dirname "$0")/.." && pwd)/.venv
fi

## Confirm before proceeding
echo ""
echo "=== Setup Summary ==="
echo "  VENV_PATH: $VENV_PATH"
echo "  Python:    3.12.12"
echo "====================="
echo ""
read -p "Proceed with setup? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Setup aborted."
    exit 0
fi

mkdir -p "$VENV_PATH"

cd "$(dirname "$0")/.."
uv venv "$VENV_PATH" --python 3.12.12
source "$VENV_PATH/bin/activate"

uv pip install -e .
uv pip install -r setups/requirements.txt

# Instruction
echo -e "Activate the virtual environment with:\n\tsource $VENV_PATH/bin/activate"