#!/usr/bin/env bash
# config.sh — two-tiered config loader for bash.
#
# Reads config_default.yaml (committed defaults) and config.yaml (local
# overrides, gitignored). On first run, config.yaml is seeded from the
# defaults so you have a file to edit. At load time the two are merged:
# config.yaml wins, and any key it omits falls back to config_default.yaml.
#
# Usage:
#   source /path/to/bash/config.sh
#   config_load                      # uses repo root (../ from this file)
#   config_load /some/config/dir     # or point it at an explicit dir
#
#   config get app.name              # → my-app
#   config get app.workers           # → 4   (falls back to defaults)
#   config get features              # → list, one item per line
#   config keys                      # all dotted keys (union of both tiers)
#   config required                  # keys still set to __REQUIRED__
#   config destroy                   # tear down all loaded state
#
# Pass "strict" as 2nd arg to config_load to error on unset ${env:VAR}.
#
# Required fields: any value equal to __REQUIRED__ marks a field the user must
# fill in. config_load lists every such field and fails, so a half-configured
# install dies at startup instead of somewhere deep in a run:
#
#   venv_path: __REQUIRED__     # in config_default.yaml
#
# Set CONFIG_SKIP_REQUIRED_CHECK=1 to load anyway (e.g. to inspect the config
# with `config required` before filling it in).

# Placeholder for "the user must fill this in". Mirrors REQUIRED in python/config.py.
CONFIG_REQUIRED="__REQUIRED__"

# List every leaf path in a YAML file as "<flag> <dotted.path>", where <flag>
# is R when the value is the placeholder and . otherwise. List items report
# their parent key.
#
# This reads the file rather than the parsed dict because the dict encodes
# nesting as "__", which is ambiguous with underscores in key names
# (`aws.s3_bucket` comes back from `config keys` as `aws.s3.bucket`).
_config_scan_required() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    awk -v sentinel="$CONFIG_REQUIRED" '
        function trim(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); return s }
        function clean(v,   q) {
            v = trim(v)
            q = substr(v, 1, 1)
            if ((q == "\"" || q == "'"'"'") && substr(v, length(v), 1) == q)
                return substr(v, 2, length(v) - 2)
            sub(/[[:space:]]+#.*$/, "", v)
            return trim(v)
        }
        function path(   i, p) {
            for (i = 1; i <= top; i++) p = (i == 1 ? keys[i] : p "." keys[i])
            return p
        }
        /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
        {
            match($0, /^[ ]*/); ind = RLENGTH
            s = substr($0, ind + 1)
            if (s ~ /^-[[:space:]]/) {
                print (clean(substr(s, 2)) == sentinel ? "R " : ". ") path()
                next
            }
            if (match(s, /^[A-Za-z0-9_-]+:/)) {
                key = substr(s, 1, RLENGTH - 1)
                val = clean(substr(s, RLENGTH + 1))
                while (top > 0 && ind <= indents[top]) top--
                if (val == "") { top++; indents[top] = ind; keys[top] = key }
                else print (val == sentinel ? "R " : ". ") path() "." key
            }
        }
    ' "$file"
}

# Resolve the directory this file lives in (works when sourced).
_config_sh_dir() {
    local src="${BASH_SOURCE[0]}"
    cd "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

# shellcheck source=yaml_parser.sh
source "$(_config_sh_dir)/yaml_parser.sh"

# config_load [config_dir] [strict]
#   config_dir defaults to the repo root (one level up from this file).
config_load() {
    local config_dir="${1:-$(_config_sh_dir)/..}"
    local strict="${2:-}"

    local default_file="${config_dir}/config_default.yaml"
    local user_file="${config_dir}/config.yaml"

    if [[ ! -f "$default_file" ]]; then
        echo "config_load: defaults not found: $default_file" >&2
        return 1
    fi

    # Tier 2: create config.yaml from defaults on first run, dropping
    # whole-line comments (lines whose first non-blank char is #).
    if [[ ! -f "$user_file" ]]; then
        grep -v '^[[:space:]]*#' "$default_file" | sed '/./,$!d' > "$user_file" || return 1
        echo "config_load: created $user_file from defaults — edit as needed" >&2
    fi

    # Reset any previous load.
    declare -F _cfg_def >/dev/null 2>&1 && _cfg_def destroy
    declare -F _cfg_usr >/dev/null 2>&1 && _cfg_usr destroy

    yaml_parse_dict "$default_file" _cfg_def "$strict" || return 1
    yaml_parse_dict "$user_file"   _cfg_usr "$strict" || return 1

    # Globals, not locals: `config` outlives this function and `config required`
    # re-reads the source files.
    _cfg_default_file="$default_file"
    _cfg_user_file="$user_file"

    # True if KEY is present in the user tier (config.yaml). A key that exists
    # but holds an empty value still counts as present. Defined here (not at
    # source scope) so it is recreated after a `config destroy`.
    _config_user_has() {
        _cfg_usr keys | grep -Fxq "$1"
    }

    # Build the merged accessor.
    config() {
        local cmd="$1"; shift
        case "$cmd" in
            get)
                local key="$1"
                # User value wins only if the key is actually present there.
                if _config_user_has "$key"; then
                    _cfg_usr get "$key"
                else
                    _cfg_def get "$key"
                fi
                ;;
            keys)
                { _cfg_def keys; _cfg_usr keys; } | sort -u
                ;;
            required)
                # A field is pending if the winning tier holds the placeholder:
                # the user tier when it defines the key, the default tier
                # otherwise — the same rule `config get` applies.
                {
                    _config_scan_required "$_cfg_user_file" | sed -n 's/^R //p'
                    comm -23 \
                        <(_config_scan_required "$_cfg_default_file" | sed -n 's/^R //p' | sort -u) \
                        <(_config_scan_required "$_cfg_user_file" | cut -c3- | sort -u)
                } | sort -u
                ;;
            dump)
                local k
                while IFS= read -r k; do
                    printf '%s=%s\n' "$k" "$(config get "$k")"
                done < <(config keys)
                ;;
            destroy)
                declare -F _cfg_def >/dev/null 2>&1 && _cfg_def destroy
                declare -F _cfg_usr >/dev/null 2>&1 && _cfg_usr destroy
                unset -f config _config_user_has
                unset _cfg_default_file _cfg_user_file
                ;;
            *)
                echo "config: unknown command: $cmd" >&2
                return 1
                ;;
        esac
    }

    # Early exit: refuse to hand back a config with unfilled placeholders,
    # reporting every offender rather than one per run.
    if [[ "${CONFIG_SKIP_REQUIRED_CHECK:-}" != "1" ]]; then
        local _cfg_pending _cfg_pending_key
        _cfg_pending="$(config required)"
        if [[ -n "$_cfg_pending" ]]; then
            echo "config_load: field(s) still set to ${CONFIG_REQUIRED} in ${user_file}:" >&2
            while IFS= read -r _cfg_pending_key; do
                echo "  - ${_cfg_pending_key}" >&2
            done <<< "$_cfg_pending"
            config destroy
            return 1
        fi
    fi
}
