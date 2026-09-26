#!/usr/bin/env bash
# yaml_parser.sh — Pure-bash YAML parser.
# Supports: scalar values, nested keys, lists (as bash arrays), multi-line
# strings (literal | and folded >), anchors/aliases (&name / *name / << merge),
# flow syntax ({key: val} and [a, b, c]), inline comments, and ${var} references.
#
# Variable references: use "." for nesting inside ${} references.
#   e.g. ${base_dir}          → resolves flat key "base_dir"
#        ${defaults.host}     → resolves nested key "defaults" → "host"
#        ${defaults.adapter}://${defaults.host}/${development.database}
#                             → postgres://localhost/dev_db
#
# Environment variable references: use ${env:VAR} to resolve shell env vars.
#   e.g. ${env:HOME}              → /home/user
#        ${env:DB_HOST:-localhost} → uses $DB_HOST or "localhost" if unset
#
# Compile: yaml_compile input.yaml output.yaml [strict]
#   Performs raw text substitution of ${env:...} patterns, preserving
#   comments and formatting. Output to file or stdout (use "-").
#
# Usage:
#   source yaml_parser.sh
#   yaml_parse_dict config.yaml cfg
#   cfg get "key2.nested_key.nested_item"   # → nested_value
#   cfg get "key2.listed_items"              # → item1\nitem2\nitem3
#   cfg keys                                 # list all dotted keys
#   cfg dump                                 # print all key=value pairs
#   cfg destroy                              # clean up all storage

# ── Internal parser (not part of public API) ──
_yaml_parse() {
    local file="$1"
    local prefix="${2:-}"
    local strict="${3:-}"

    if [[ ! -f "$file" ]]; then
        echo "yaml_parse: file not found: $file" >&2
        return 1
    fi

    # ── Anchor storage ──
    local _yaml_anchor_prefix="_yaml_anc_$$_"

    _yaml_set_anchor() {
        local anchor_name="$1" key="$2" value="$3"
        eval "${_yaml_anchor_prefix}${anchor_name}____${key}=\"\${value}\""
    }

    _yaml_get_anchor_keys() {
        local anchor_name="$1"
        compgen -v "${_yaml_anchor_prefix}${anchor_name}____" 2>/dev/null | \
            sed "s/^${_yaml_anchor_prefix}${anchor_name}____//"
    }

    _yaml_get_anchor_value() {
        local anchor_name="$1" key="$2"
        local var="${_yaml_anchor_prefix}${anchor_name}____${key}"
        echo "${!var}"
    }

    _yaml_cleanup_anchors() {
        local v
        for v in $(compgen -v "${_yaml_anchor_prefix}" 2>/dev/null); do
            unset "$v"
        done
    }

    _yaml_strip_comment() {
        local val="$1"
        if [[ "$val" =~ ^\"(.*)\" ]]; then
            echo "${BASH_REMATCH[1]}"
            return
        fi
        if [[ "$val" =~ ^\'(.*)\' ]]; then
            echo "${BASH_REMATCH[1]}"
            return
        fi
        if [[ "$val" =~ ^([^\"\']*[^[:space:]])[[:space:]]+#.* ]]; then
            echo "${BASH_REMATCH[1]}"
            return
        fi
        echo "$val"
    }

    _yaml_strip_quotes() {
        local val="$1"
        val="${val#\"}"
        val="${val%\"}"
        val="${val#\'}"
        val="${val%\'}"
        echo "$val"
    }

    _yaml_build_varname() {
        local full=""
        local seg
        for seg in "$@"; do
            full+="${full:+__}${seg}"
        done
        if [[ -n "$prefix" ]]; then
            echo "${prefix}__${full}"
        else
            echo "$full"
        fi
    }

    _yaml_set_var() {
        local varname="$1" value="$2"
        eval "${varname}=\"\${value}\""
    }

    _yaml_set_array() {
        local varname="$1"
        shift
        local arr_str="("
        local v
        for v in "$@"; do
            arr_str+="\"$v\" "
        done
        arr_str+=")"
        eval "${varname}=${arr_str}"
    }

    _yaml_parse_flow_mapping() {
        local var_prefix="$1"
        local content="$2"
        local anchor_name="${3:-}"

        content="${content#\{}"
        content="${content%\}}"
        content="${content## }"
        content="${content%% }"

        local IFS_OLD="$IFS"
        IFS=','
        local -a pairs=($content)
        IFS="$IFS_OLD"

        local pair
        for pair in "${pairs[@]}"; do
            pair="${pair## }"
            pair="${pair%% }"
            if [[ "$pair" =~ ^([A-Za-z0-9_-]+):[[:space:]]*(.*) ]]; then
                local fkey="${BASH_REMATCH[1]}"
                local fval="${BASH_REMATCH[2]}"
                fval="$(_yaml_strip_quotes "$(_yaml_strip_comment "$fval")")"
                local fvarname="${var_prefix}__${fkey}"
                _yaml_set_var "$fvarname" "$fval"
                if [[ -n "$anchor_name" ]]; then
                    _yaml_set_anchor "$anchor_name" "$fkey" "$fval"
                fi
            fi
        done
    }

    _yaml_parse_flow_sequence() {
        local varname="$1"
        local content="$2"

        content="${content#\[}"
        content="${content%\]}"
        content="${content## }"
        content="${content%% }"

        local IFS_OLD="$IFS"
        IFS=','
        local -a raw_items=($content)
        IFS="$IFS_OLD"

        local -a items=()
        local item
        for item in "${raw_items[@]}"; do
            item="${item## }"
            item="${item%% }"
            item="$(_yaml_strip_quotes "$item")"
            items+=("$item")
        done

        _yaml_set_array "$varname" "${items[@]}"
    }

    # ── Main parse state ──
    local -a stack=()
    local -a indents=(0)
    local -a list_values=()
    local list_var=""
    local multiline_var=""
    local multiline_style=""
    local multiline_indent=-1
    local multiline_content=""
    local current_anchor=""
    local current_anchor_base_indent=-1

    _yaml_flush_list() {
        if [[ -n "$list_var" ]]; then
            _yaml_set_array "$list_var" "${list_values[@]+"${list_values[@]}"}"
            list_values=()
            list_var=""
        fi
    }

    _yaml_flush_multiline() {
        if [[ -n "$multiline_var" ]]; then
            while [[ "$multiline_content" == *$'\n' ]]; do
                multiline_content="${multiline_content%$'\n'}"
            done
            if [[ "$multiline_style" == ">" ]]; then
                multiline_content="${multiline_content%% }"
            fi
            _yaml_set_var "$multiline_var" "$multiline_content"
            multiline_var=""
            multiline_style=""
            multiline_indent=-1
            multiline_content=""
        fi
    }

    while IFS='' read -r line || [[ -n "$line" ]]; do
        if [[ -n "$multiline_var" ]]; then
            if [[ "$line" =~ ^[[:space:]]*$ ]]; then
                multiline_content+=$'\n'
                continue
            fi
            local stripped_ml="${line#"${line%%[![:space:]]*}"}"
            local indent_ml=$(( ${#line} - ${#stripped_ml} ))

            if (( multiline_indent < 0 )); then
                multiline_indent=$indent_ml
            fi

            if (( indent_ml >= multiline_indent )); then
                local rel_indent=$(( indent_ml - multiline_indent ))
                local pad=""
                local i
                for (( i=0; i<rel_indent; i++ )); do pad+=" "; done
                if [[ "$multiline_style" == "|" ]]; then
                    multiline_content+="${pad}${stripped_ml}"$'\n'
                else
                    if [[ -n "$multiline_content" ]]; then
                        multiline_content+=" "
                    fi
                    multiline_content+="${stripped_ml}"
                fi
                continue
            else
                _yaml_flush_multiline
            fi
        fi

        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        local stripped="${line#"${line%%[![:space:]]*}"}"
        local indent=$(( ${#line} - ${#stripped} ))

        if [[ "$stripped" =~ ^-[[:space:]]+(.*) ]]; then
            local item="${BASH_REMATCH[1]}"
            item="$(_yaml_strip_comment "$item")"
            item="$(_yaml_strip_quotes "$item")"
            if [[ -z "$list_var" ]]; then
                list_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}")"
            fi
            list_values+=("$item")
            continue
        fi

        _yaml_flush_list

        if [[ "$stripped" =~ ^(<<):[[:space:]]*(.*) ]] || \
           [[ "$stripped" =~ ^([A-Za-z0-9_-]+):[[:space:]]*(.*) ]] || \
           [[ "$stripped" =~ ^([A-Za-z0-9_-]+):$ ]]; then
            local key="${BASH_REMATCH[1]}"
            local value="${BASH_REMATCH[2]:-}"

            while (( ${#indents[@]} > 1 )) && (( indent <= indents[${#indents[@]}-1] )); do
                unset 'stack[${#stack[@]}-1]'
                unset 'indents[${#indents[@]}-1]'
            done

            if [[ "$key" == "<<" ]]; then
                if [[ "$value" =~ ^\*([A-Za-z0-9_-]+) ]]; then
                    local alias_name="${BASH_REMATCH[1]}"
                    local base_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}")"
                    local akey
                    for akey in $(_yaml_get_anchor_keys "$alias_name"); do
                        local target_var="${base_var}__${akey}"
                        if [[ -z "${!target_var+x}" ]]; then
                            local aval
                            aval="$(_yaml_get_anchor_value "$alias_name" "$akey")"
                            _yaml_set_var "$target_var" "$aval"
                        fi
                    done
                fi
                continue
            fi

            local line_anchor=""
            if [[ "$value" =~ ^'&'([A-Za-z0-9_-]+)[[:space:]]*(.*) ]]; then
                line_anchor="${BASH_REMATCH[1]}"
                value="${BASH_REMATCH[2]}"
            fi

            if [[ "$value" =~ ^\*([A-Za-z0-9_-]+) ]]; then
                local alias_name="${BASH_REMATCH[1]}"
                local base_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}" "$key")"
                local akey
                for akey in $(_yaml_get_anchor_keys "$alias_name"); do
                    local aval
                    aval="$(_yaml_get_anchor_value "$alias_name" "$akey")"
                    _yaml_set_var "${base_var}__${akey}" "$aval"
                done
                continue
            fi

            if [[ -n "$value" ]]; then
                if [[ "$value" =~ ^\{.*\}$ ]]; then
                    local fm_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}" "$key")"
                    _yaml_parse_flow_mapping "$fm_var" "$value" "$line_anchor"
                    continue
                fi
                if [[ "$value" =~ ^\[.*\]$ ]]; then
                    local fs_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}" "$key")"
                    _yaml_parse_flow_sequence "$fs_var" "$value"
                    continue
                fi
                if [[ "$value" == "|" || "$value" == ">" ]]; then
                    multiline_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}" "$key")"
                    multiline_style="$value"
                    multiline_indent=-1
                    multiline_content=""
                    continue
                fi

                value="$(_yaml_strip_comment "$value")"
                value="$(_yaml_strip_quotes "$value")"

                local full_var="$(_yaml_build_varname "${stack[@]+"${stack[@]}"}" "$key")"
                _yaml_set_var "$full_var" "$value"

                if [[ -n "$line_anchor" ]]; then
                    current_anchor="$line_anchor"
                    current_anchor_base_indent=$indent
                    _yaml_set_anchor "$line_anchor" "$key" "$value"
                fi

                if [[ -n "$current_anchor" && $indent -gt $current_anchor_base_indent ]]; then
                    _yaml_set_anchor "$current_anchor" "$key" "$value"
                fi
            else
                stack+=("$key")
                indents+=("$indent")

                if [[ -n "$line_anchor" ]]; then
                    current_anchor="$line_anchor"
                    current_anchor_base_indent=$indent
                elif [[ -n "$current_anchor" && $indent -le $current_anchor_base_indent ]]; then
                    current_anchor=""
                    current_anchor_base_indent=-1
                fi
            fi
        fi
    done < "$file"

    _yaml_flush_list
    _yaml_flush_multiline

    # ── Resolve ${env:VAR} references ──
    local _env_var_prefix="${prefix:+${prefix}__}"
    local _env_failed=0
    local _evar
    for _evar in $(compgen -v "$_env_var_prefix" 2>/dev/null); do
        declare -p "$_evar" 2>/dev/null | grep -q 'declare -a' && continue
        local _eval="${!_evar}"
        case "$_eval" in
            *'${env:'*'}'*) ;;
            *) continue ;;
        esac
        local _env_resolved="$_eval"
        while [[ "$_env_resolved" =~ \$\{env:([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\} ]]; do
            local _env_name="${BASH_REMATCH[1]}"
            local _env_has_default="${BASH_REMATCH[2]}"
            local _env_default="${BASH_REMATCH[3]}"
            local _env_val=""
            if [[ -n "${!_env_name+x}" ]]; then
                _env_val="${!_env_name}"
            elif [[ -n "$_env_has_default" ]]; then
                _env_val="$_env_default"
            else
                if [[ "$strict" == "strict" ]]; then
                    echo "yaml_parse: env var not set: ${_env_name}" >&2
                    _env_failed=1
                else
                    echo "yaml_parse: warning: env var not set: ${_env_name}" >&2
                fi
            fi
            _env_resolved="${_env_resolved/\$\{env:${_env_name}${_env_has_default}\}/${_env_val}}"
        done
        if [[ "$_env_resolved" != "$_eval" ]]; then
            printf -v "$_evar" '%s' "$_env_resolved"
        fi
    done
    if (( _env_failed )); then
        return 1
    fi

    # ── Resolve ${var} references ──
    local _resolve_max=10
    local _resolve_pass=0
    local _resolve_changed=1
    local _var_prefix="${prefix:+${prefix}__}"

    while (( _resolve_changed && _resolve_pass < _resolve_max )); do
        _resolve_changed=0
        (( _resolve_pass++ ))
        local _rvar
        for _rvar in $(compgen -v "$_var_prefix" 2>/dev/null); do
            declare -p "$_rvar" 2>/dev/null | grep -q 'declare -a' && continue
            local _rval="${!_rvar}"
            case "$_rval" in
                *'${'*'}'*) ;;
                *) continue ;;
            esac
            local _resolved="$_rval"
            while [[ "$_resolved" =~ \$\{([A-Za-z0-9_.-]+)\} ]]; do
                local _ref="${BASH_REMATCH[1]}"
                # Dots in ${foo.bar} refer to nested keys and must be
                # sanitized before indirect expansion — bash rejects `.`
                # in variable names.
                local _ref_sanitized="${_ref//./__}"
                local _ref_var="${_var_prefix}${_ref_sanitized}"
                local _ref_val="${!_ref_var-}"
                if [[ -z "$_ref_val" ]]; then
                    _ref_var="${_var_prefix}${_ref_sanitized//_/__}"
                    _ref_val="${!_ref_var-}"
                fi
                if [[ -z "$_ref_val" && "$_ref" != *.* && -n "${!_ref:-}" ]]; then
                    _ref_val="${!_ref}"
                fi
                _resolved="${_resolved/\$\{${_ref}\}/${_ref_val}}"
            done
            if [[ "$_resolved" != "$_rval" ]]; then
                eval "${_rvar}=\"\${_resolved}\""
                _resolve_changed=1
            fi
        done
    done

    # Clean up
    _yaml_cleanup_anchors
    unset -f _yaml_flush_list _yaml_flush_multiline
    unset -f _yaml_set_anchor _yaml_get_anchor_keys _yaml_get_anchor_value _yaml_cleanup_anchors
    unset -f _yaml_strip_comment _yaml_strip_quotes _yaml_build_varname
    unset -f _yaml_set_var _yaml_set_array
    unset -f _yaml_parse_flow_mapping _yaml_parse_flow_sequence
}

# ── Public API ──

_yaml_dict_print_var() {
    local var="$1"
    if declare -p "$var" 2>/dev/null | grep -q 'declare -a'; then
        eval "printf '%s\n' \"\${${var}[@]}\""
    else
        echo "${!var}"
    fi
}

# Parse a YAML file into a named dictionary.
# Creates a lookup function named $dict_name.
#
# Usage:
#   yaml_parse_dict config.yaml cfg [strict]
#   cfg get "key2.nested_key.nested_item"   # → nested_value
#   cfg get "key2.listed_items"              # → item1\nitem2\nitem3
#   cfg keys                                 # list all dotted keys
#   cfg dump                                 # print all key=value pairs
#   cfg destroy                              # clean up all storage
#
# Pass "strict" as 3rd arg to error on missing ${env:VAR} without defaults.
yaml_parse_dict() {
    local file="$1"
    local dict_name="$2"
    local strict="${3:-}"
    local _dict_prefix="__yd_${dict_name}"

    _yaml_parse "$file" "$_dict_prefix" "$strict" || return 1

    eval "${dict_name}() {
        local cmd=\"\$1\"; shift
        case \"\$cmd\" in
            get)
                local key=\"\$1\"
                local var=\"${_dict_prefix}__\${key//./__}\"
                _yaml_dict_print_var \"\$var\"
                ;;
            keys)
                compgen -v \"${_dict_prefix}__\" 2>/dev/null | \
                    sed 's/^${_dict_prefix}__//' | \
                    sed 's/__/./g' | \
                    sort
                ;;
            dump)
                local v
                for v in \$(compgen -v \"${_dict_prefix}__\" 2>/dev/null | sort); do
                    local key=\"\${v#${_dict_prefix}__}\"
                    key=\"\${key//__/.}\"
                    printf '%s=' \"\$key\"
                    _yaml_dict_print_var \"\$v\"
                done
                ;;
            destroy)
                local v
                for v in \$(compgen -v \"${_dict_prefix}__\" 2>/dev/null); do
                    unset \"\$v\"
                done
                unset -f \"${dict_name}\"
                ;;
        esac
    }"
}

# Compile a YAML file by substituting ${env:VAR} patterns with shell env vars.
# Raw text substitution — preserves comments, formatting, anchors.
#
# Usage:
#   yaml_compile input.yaml output.yaml [strict]
#   yaml_compile input.yaml -              # output to stdout
#
# Pass "strict" as 3rd arg to error on missing env vars without defaults.
yaml_compile() {
    local infile="$1"
    local outfile="$2"
    local strict="${3:-}"

    if [[ ! -f "$infile" ]]; then
        echo "yaml_compile: file not found: $infile" >&2
        return 1
    fi

    local _compile_failed=0
    local result=""

    while IFS='' read -r line || [[ -n "$line" ]]; do
        local resolved="$line"
        while [[ "$resolved" =~ \$\{env:([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\} ]]; do
            local _cname="${BASH_REMATCH[1]}"
            local _chas_default="${BASH_REMATCH[2]}"
            local _cdefault="${BASH_REMATCH[3]}"
            local _cval=""
            if [[ -n "${!_cname+x}" ]]; then
                _cval="${!_cname}"
            elif [[ -n "$_chas_default" ]]; then
                _cval="$_cdefault"
            else
                if [[ "$strict" == "strict" ]]; then
                    echo "yaml_compile: env var not set: ${_cname}" >&2
                    _compile_failed=1
                else
                    echo "yaml_compile: warning: env var not set: ${_cname}" >&2
                fi
            fi
            resolved="${resolved/\$\{env:${_cname}${_chas_default}\}/${_cval}}"
        done
        result+="${resolved}"$'\n'
    done < "$infile"

    if (( _compile_failed )); then
        return 1
    fi

    if [[ "$outfile" == "-" ]]; then
        printf '%s' "$result"
    else
        printf '%s' "$result" > "$outfile"
    fi
}
