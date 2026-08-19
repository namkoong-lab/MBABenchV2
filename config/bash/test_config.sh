#!/usr/bin/env bash
# test_config.sh — self-contained tests for the bash config loader.
# Runs in a temp dir so it never touches the repo's real config.yaml.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$HERE/.."

PASS=0
FAIL=0
check() { # check <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        echo "  ok: $1"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $1 — expected [$2], got [$3]"
        FAIL=$((FAIL + 1))
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$REPO_ROOT/config_default.yaml" "$TMP/config_default.yaml"

source "$HERE/config.sh"

echo "Test 1: create config.yaml on first run, defaults resolve"
[[ -f "$TMP/config.yaml" ]] && { echo "  precondition failed"; exit 1; }
config_load "$TMP" 2>/dev/null
check "config.yaml created" "yes" "$([[ -f "$TMP/config.yaml" ]] && echo yes || echo no)"
check "app.name from defaults" "my-app" "$(config get app.name)"
check "app.workers from defaults" "4" "$(config get app.workers)"
config destroy

echo "Test 2: partial override falls back to defaults for omitted keys"
cat > "$TMP/config.yaml" <<'EOF'
app:
  log_level: debug
EOF
config_load "$TMP" 2>/dev/null
check "overridden log_level" "debug" "$(config get app.log_level)"
check "omitted workers falls back" "4" "$(config get app.workers)"
check "omitted name falls back" "my-app" "$(config get app.name)"
config destroy

echo 'Test 3: ${env:VAR:-default} expansion'
unset DB_PASSWORD
config_load "$TMP" 2>/dev/null
check "db.password uses env default" "changeme" "$(config get database.password)"
config destroy

export DB_PASSWORD="s3cret"
config_load "$TMP" 2>/dev/null
check "db.password uses env when set" "s3cret" "$(config get database.password)"
unset DB_PASSWORD
config destroy

echo "Test 4: __REQUIRED__ placeholder triggers early exit"
REQ_TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$REQ_TMP"' EXIT
cat > "$REQ_TMP/config_default.yaml" <<'EOF'
venv_path: __REQUIRED__
aws:
  s3_bucket: "mbabench"
  region: __REQUIRED__
features:
  - alpha
  - __REQUIRED__
EOF

# 4a: nothing filled in — load fails and names every pending field.
cat > "$REQ_TMP/config.yaml" <<'EOF'
aws:
  s3_bucket: "mbabench"
EOF
config_load "$REQ_TMP" 2>/dev/null
check "load fails while placeholders remain" "1" "$?"
check "config torn down after failure" "no" \
    "$(declare -F config >/dev/null 2>&1 && echo yes || echo no)"

CONFIG_SKIP_REQUIRED_CHECK=1 config_load "$REQ_TMP" 2>/dev/null
check "skip flag loads anyway" "0" "$?"
check "required lists all pending fields" "aws.region
features
venv_path" "$(config required)"
config destroy

# 4b: user tier fills them in — placeholders in the defaults are overridden.
cat > "$REQ_TMP/config.yaml" <<'EOF'
venv_path: /opt/venvs/mbabench
aws:
  s3_bucket: "mbabench"
  region: us-east-1
features:
  - alpha
  - beta
EOF
config_load "$REQ_TMP" 2>/dev/null
check "load succeeds once filled in" "0" "$?"
check "no pending fields" "" "$(config required)"
check "underscore key still resolves" "mbabench" "$(config get aws.s3_bucket)"
config destroy

# 4c: a placeholder written into the user tier is caught too.
cat > "$REQ_TMP/config.yaml" <<'EOF'
venv_path: __REQUIRED__
aws:
  s3_bucket: "mbabench"
  region: us-east-1
features:
  - alpha
EOF
config_load "$REQ_TMP" 2>/dev/null
check "user-tier placeholder fails load" "1" "$?"
CONFIG_SKIP_REQUIRED_CHECK=1 config_load "$REQ_TMP" 2>/dev/null
check "user-tier placeholder listed" "venv_path" "$(config required)"
config destroy

echo
echo "Passed: $PASS  Failed: $FAIL"
[[ "$FAIL" -eq 0 ]]
