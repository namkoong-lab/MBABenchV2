#!/usr/bin/env bash
# example.sh — minimal demo of the bash two-tiered config loader, against this
# repository's own config (config_default.yaml + the gitignored config.yaml).
#
#     bash config/bash/example.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"

# Load from the repo root (creates config.yaml from the defaults on first run).
# Unset ${env:VAR} references print a warning on stderr and resolve to "".
config_load

echo "aws.s3_bucket    = $(config get aws.s3_bucket)"
echo "venv_path        = $(config get venv_path)"          # null prints "null"
echo "libreoffice_path = $(config get libreoffice_path)"
echo "keys present:"
config keys | grep '^keys\.' | sed 's/^/  - /'

config destroy
