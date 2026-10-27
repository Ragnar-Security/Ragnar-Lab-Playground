#!/usr/bin/env bash

# Values exported by the caller must win over workstation defaults, matching
# normal environment-based configuration while still allowing a sourceable file.
load_site_env() {
    local file="$1"
    shift
    [[ -f "$file" ]] || return 0

    local names=("$@")
    local was_exported=()
    local values=()
    local name declaration index
    for name in "${names[@]}"; do
        declaration="$(declare -p "$name" 2>/dev/null || true)"
        if [[ "$declaration" == "declare -x "* ]]; then
            was_exported+=(1)
            values+=("${!name}")
        else
            was_exported+=(0)
            values+=("")
        fi
    done

    local had_allexport=0
    [[ "$-" == *a* ]] && had_allexport=1
    set -a
    # shellcheck disable=SC1090 -- the operator chooses this untracked path.
    source "$file"
    ((had_allexport)) || set +a

    for ((index = 0; index < ${#names[@]}; index++)); do
        if ((was_exported[index])); then
            printf -v "${names[index]}" '%s' "${values[index]}"
            export "${names[index]}"
        fi
    done
}

require_site_var() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        printf 'ERROR: required configuration variable %s is unset; set it in the environment or scripts/site.env\n' "$name" >&2
        exit 1
    fi
}
