#!/usr/bin/env bash
# example.sh — minimal demo of the bash two-tiered config loader.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"

# Load from the repo root (creates config.yaml from defaults on first run).
config_load

echo "app.name      = $(config get app.name)"
echo "app.log_level = $(config get app.log_level)"
echo "app.workers   = $(config get app.workers)"
echo "paths.cache   = $(config get paths.cache_dir)"   # ${data_dir} resolved
echo "db.password   = $(config get database.password)" # ${env:DB_PASSWORD:-...} resolved
echo "features:"
config get features | sed 's/^/  - /'

config get multiline_literal
config destroy
