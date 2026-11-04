#!/usr/bin/env bash
# Render the per-student Headscale policy from the running portal and apply it.
# Dry-run mode reads both hosts and makes no changes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/site_env.sh"

SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" \
    PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR \
    HEADSCALE_BASE_POLICY HEADSCALE_POLICY_FILE
for name in PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR; do
    require_site_var "$name"
done

HEADSCALE_BASE_POLICY="${HEADSCALE_BASE_POLICY:-config/headscale-acl.json}"
HEADSCALE_POLICY_FILE="${HEADSCALE_POLICY_FILE:-/etc/headscale/acl.json}"
[[ "$HEADSCALE_BASE_POLICY" == /* ]] || HEADSCALE_BASE_POLICY="$REPO_ROOT/$HEADSCALE_BASE_POLICY"
for name in HEADSCALE_DIR HEADSCALE_POLICY_FILE; do
    [[ "${!name}" == /* ]] \
        || { printf 'ERROR: %s must be an absolute path\n' "$name" >&2; exit 1; }
done
for name in PORTAL_APP_CONTAINER HEADSCALE_DIR HEADSCALE_POLICY_FILE; do
    [[ "${!name}" != *"'"* ]] \
        || { printf "ERROR: %s must not contain an apostrophe\n" "$name" >&2; exit 1; }
done
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=10)
# The renderer uses the standard library only; see term_changeover.sh for why
# a POSIX python3 is preferred over the repository venv.
if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
else
    PYTHON="$(command -v python || true)"
fi
POLICY_TOOL="$SCRIPT_DIR/headscale_policy.py"
APPLY=0
TMP_DIR=""

usage() {
    cat <<'EOF'
Usage: scripts/apply_headscale_policy.sh [--apply]

Render the Headscale policy that lets each roster student reach only their
own lab networks, their group's networks and the running KOTH arena.

The roster, VLANs, enabled backends, lab CIDRs and VLAN bands are exported
from the running portal. The operator's base policy (HEADSCALE_BASE_POLICY)
supplies every other section and rule; only its blanket
"autogroup:member" rule is replaced.

The default dry run reads both hosts, prints the change, and writes nothing.
Use --apply to check the policy inside the Headscale container, back up the
current file, install the new one atomically, signal Headscale to reload it,
and verify the read-back (restarting the headscale service once if it differs).
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --apply) APPLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
    shift
done

[[ -x "$PYTHON" ]] || die "Python not found: $PYTHON"
[[ -f "$POLICY_TOOL" ]] || die "Policy renderer not found: $POLICY_TOOL"
[[ -f "$HEADSCALE_BASE_POLICY" ]] || die "Base policy not found: $HEADSCALE_BASE_POLICY"

# Maps the container policy path onto the host file behind the headscale
# container's mount, so the new file can be renamed into place atomically.
HOST_PATH_PY='import json,re,sys
target = sys.argv[1]
best = None
for mount in json.load(sys.stdin)[0].get("Mounts") or []:
    dest = (mount.get("Destination") or "").rstrip("/")
    if dest and (target == dest or target.startswith(dest + "/")):
        if best is None or len(dest) > len(best[0]):
            best = (dest, mount.get("Source") or "")
if best is None:
    raise SystemExit(f"{target} is not on a host mount of the headscale container")
host = best[1].rstrip("/") + target[len(best[0]):]
# The path is spliced into remote shell code: plain characters only.
if not re.fullmatch(r"/[A-Za-z0-9._/@+-]+", host):
    raise SystemExit("the host path of the policy file has unsupported characters")
print(host)'

fetch_slots() {
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" \
        "docker exec '$PORTAL_APP_CONTAINER' python scripts/export_network_slots.py"
}

fetch_policy() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose exec -T headscale headscale policy get"
}

# Prints nothing when the effective policy equals the desired one.
policy_changes() {
    local current="$TMP_DIR/current.json"
    fetch_policy > "$current" || die "Could not read the effective Headscale policy."
    "$PYTHON" "$POLICY_TOOL" diff "$current" "$TMP_DIR/desired.json" \
        || die "Could not compare the effective Headscale policy."
}

resolve_host_policy_file() {
    local inspect
    inspect="$(ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker inspect \"\$(docker compose ps -q headscale)\"")" \
        || die "Could not inspect the headscale container."
    "$PYTHON" -c "$HOST_PATH_PY" "$HEADSCALE_POLICY_FILE" <<< "$inspect" \
        || die "Could not find the host file behind $HEADSCALE_POLICY_FILE."
}

install_policy() {
    local host_file="$1" stamp="$2"
    local upload="$host_file.$stamp.upload"
    local staged="$host_file.$stamp.tmp"
    local backup="$host_file.$stamp"
    local status=0
    scp "${SSH_OPTIONS[@]}" "$TMP_DIR/desired.json" "$HEADSCALE_HOST:$upload" >/dev/null \
        || die "Could not upload the rendered policy."
    # cp -p before writing keeps the live file's owner and mode on the new one;
    # mv in the same directory is the atomic step.
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "set -e
        umask 077
        trap 'rm -f \"$upload\" \"$staged\"' EXIT
        cd '$HEADSCALE_DIR'
        test -f '$host_file'
        docker compose exec -T headscale headscale policy check --file /dev/stdin < '$upload' >&2 || exit 3
        test ! -e '$backup'
        cp -p '$host_file' '$backup'
        chmod 600 '$backup'
        cp -p '$host_file' '$staged'
        cat '$upload' > '$staged'
        mv -f '$staged' '$host_file'" || status=$?
    case "$status" in
        0) printf '  backup: %s:%s\n' "$HEADSCALE_HOST" "$backup" ;;
        3) die "Headscale rejected the rendered policy; nothing was installed." ;;
        *) die "Installing the policy failed; check $HEADSCALE_HOST:$host_file." ;;
    esac
}

# Headscale (file policy mode) reloads its policy on SIGHUP; without it the
# server keeps enforcing the old rules although the file already matches.
reload_headscale() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose kill -s HUP headscale >/dev/null" \
        || die "Could not signal Headscale to reload its policy."
}

restart_headscale() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" "cd '$HEADSCALE_DIR' &&
        docker compose restart headscale >/dev/null &&
        for attempt in \$(seq 1 30); do
            docker compose exec -T headscale headscale policy get >/dev/null 2>&1 && exit 0
            sleep 1
        done
        exit 1" || die "Headscale did not come back after the restart."
}

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/headscale-policy.XXXXXX")"
chmod 700 "$TMP_DIR"

printf 'Headscale policy: %s\n' "$([[ "$APPLY" == 1 ]] && printf APPLY || printf 'DRY RUN (read-only)')"
fetch_slots > "$TMP_DIR/slots.json" || die "Could not export network slots from the portal."
"$PYTHON" "$POLICY_TOOL" render "$HEADSCALE_BASE_POLICY" "$TMP_DIR/slots.json" > "$TMP_DIR/desired.json" \
    || die "Could not render the Headscale policy."

changes="$(policy_changes)"
if [[ -z "$changes" ]]; then
    printf '  nothing to do: the effective policy matches.\n'
    exit 0
fi
printf '%s\n' "$changes"
if ((APPLY == 0)); then
    printf '  would check, back up, install, reload, and verify the rendered policy.\n'
    exit 0
fi

host_file="$(resolve_host_policy_file)"
install_policy "$host_file" "$(date -u +%Y%m%dT%H%M%SZ)"
reload_headscale
readback="$(policy_changes)"
if [[ -n "$readback" ]]; then
    # A single-file bind mount keeps showing the replaced file until restart.
    printf '  read-back differs; restarting the headscale service once.\n'
    restart_headscale
    readback="$(policy_changes)"
    [[ -z "$readback" ]] || die "The effective policy still differs after one restart."
fi
printf '  verified: the effective policy matches the rendered one.\n'
