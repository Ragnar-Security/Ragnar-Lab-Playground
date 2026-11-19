#!/usr/bin/env bash
# Render the per-student Headscale policy from the running portal and apply it.
# Dry-run mode reads both hosts and makes no changes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/site_env.sh"
source "$SCRIPT_DIR/headscale_peers.sh"

SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" \
    PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR \
    HEADSCALE_BASE_POLICY HEADSCALE_POLICY_FILE JUMPBOX_SSH JUMPBOX_PEER_WAIT VLAN_BANDS
for name in PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR; do
    require_site_var "$name"
done

HEADSCALE_BASE_POLICY="${HEADSCALE_BASE_POLICY:-config/headscale-acl.json}"
HEADSCALE_POLICY_FILE="${HEADSCALE_POLICY_FILE:-/etc/headscale/acl.json}"
# Seconds to wait for the jumpbox to list every roster device after a reload.
JUMPBOX_PEER_WAIT="${JUMPBOX_PEER_WAIT:-60}"
[[ "$JUMPBOX_PEER_WAIT" =~ ^[0-9]+$ ]] \
    || { printf 'ERROR: JUMPBOX_PEER_WAIT must be a number of seconds\n' >&2; exit 1; }
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
NETGEN="$SCRIPT_DIR/jumpbox_netgen.py"
# The jumpbox routes the bands scripts/site.env gives jumpbox_configure.sh;
# the policy grants the bands the portal exports. Both must be one layout,
# or students are granted networks the jumpbox does not route.
SITE_BANDS_PY='import importlib.util,json,sys
spec = importlib.util.spec_from_file_location("jumpbox_netgen", sys.argv[2])
netgen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(netgen)
site = " ".join((sys.argv[3] or netgen.DEFAULT_BANDS).split())
with open(sys.argv[1], encoding="utf-8") as handle:
    portal = (json.load(handle).get("config") or {}).get("vlan_bands")
if not isinstance(portal, str):
    raise SystemExit("the portal export has no config.vlan_bands")
portal = " ".join(portal.split())
if portal != site:
    raise SystemExit("the portal exports VLAN_BANDS \"" + portal + "\" but the jumpbox layout "
                     "(VLAN_BANDS in scripts/site.env, else the default) is \"" + site + "\"")'
APPLY=0
PREFLIGHT=0
TMP_DIR=""

usage() {
    cat <<'EOF'
Usage: scripts/apply_headscale_policy.sh [--apply | --preflight]

Render the Headscale policy that lets each roster student reach only their
own lab networks, their group's networks and the running KOTH arena.

The roster, VLANs, enabled backends, lab CIDRs and VLAN bands are exported
from the running portal. With the proxmox backend, the exported VLAN_BANDS
must equal the jumpbox's (VLAN_BANDS in scripts/site.env, else the default)
or the run refuses before contacting Headscale.
The operator's base policy (HEADSCALE_BASE_POLICY)
supplies every other section and rule; its blanket "autogroup:member" rule
is cut down to its autogroup:self part, and any other rule whose src
includes autogroup:member or "*" must grant only autogroup:self or the
render refuses.

The default dry run reads both hosts, prints the change, and writes nothing.
Use --apply to check the policy inside the Headscale container, back up the
current file, install the new one atomically, signal Headscale to reload it,
and verify the read-back (restarting the headscale service once if it differs).
With the proxmox backend enabled, --apply then checks over SSH (JUMPBOX_SSH,
required) that the jumpbox lists every roster device as a peer, waiting up to
JUMPBOX_PEER_WAIT seconds; if any is missing it restarts Headscale once and
checks again, and fails if one still is. A portal without the proxmox backend
has no jumpbox, and the check is skipped.

--preflight reads the portal export and checks only what --apply will need:
with the proxmox backend, that JUMPBOX_SSH is set, answers over SSH, and
reports a Running Tailscale. It changes nothing and does not contact
Headscale. term_changeover.sh --apply runs it before its first stage.
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
        --preflight) PREFLIGHT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
    shift
done
((APPLY == 0 || PREFLIGHT == 0)) || die "--apply and --preflight are separate runs."

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

fetch_policy() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose exec -T headscale headscale policy get"
}

# Prints nothing when the policy file Headscale serves equals the desired one.
# In file mode `policy get` returns that file, not the policy Headscale has
# loaded, so a match never proves what is enforced.
policy_changes() {
    local current="$TMP_DIR/current.json"
    fetch_policy > "$current" || die "Could not read the policy file Headscale serves."
    "$PYTHON" "$POLICY_TOOL" diff "$current" "$TMP_DIR/desired.json" \
        || die "Could not compare the policy file Headscale serves."
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

# The check install_policy runs, for a policy that is already in place: a
# Headscale upgrade or a hand-installed file can hold a policy it now rejects.
check_policy() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose exec -T headscale headscale policy check --file /dev/stdin >&2" \
        < "$TMP_DIR/desired.json" \
        || die "Headscale rejects the rendered policy; not signalling a reload."
}

# Headscale (file policy mode) reloads its policy on SIGHUP; without it the
# server keeps enforcing the old rules although the file already matches.
reload_headscale() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose kill -s HUP headscale >/dev/null" \
        || die "Could not signal Headscale to reload its policy."
}

verify_jumpbox_peers() {
    local missing=""
    if ((PROXMOX_ENABLED == 0)); then
        printf '  Proxmox jumpbox peer check skipped: proxmox backend not enabled.\n'
        return 0
    fi
    fetch_nodes > "$TMP_DIR/nodes.json" || die "Could not list Headscale's nodes."
    if ! wait_for_peers; then
        printf '  the jumpbox does not list these roster devices as peers; restarting Headscale once:\n'
        sed 's/^/    /' <<< "$missing"
        restart_headscale
        wait_for_peers || die "After one Headscale restart the jumpbox ($JUMPBOX_SSH) still does not list" \
            "these roster devices as peers, so their students cannot reach their labs:" \
            "${missing//$'\n'/; }." \
            "Check 'headscale nodes list-routes' and 'tailscale debug netmap' on the jumpbox."
    fi
    printf '  verified: the jumpbox lists every roster device as a peer.\n'
}

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/headscale-policy.XXXXXX")"
chmod 700 "$TMP_DIR"

mode="DRY RUN (read-only)"
((APPLY == 0)) || mode=APPLY
((PREFLIGHT == 0)) || mode="PREFLIGHT (read-only)"
printf 'Headscale policy: %s\n' "$mode"
fetch_slots > "$TMP_DIR/slots.json" || die "Could not export network slots from the portal."
"$PYTHON" "$POLICY_TOOL" render "$HEADSCALE_BASE_POLICY" "$TMP_DIR/slots.json" > "$TMP_DIR/desired.json" \
    || die "Could not render the Headscale policy."
# The jumpbox serves only the Proxmox labs; a cloud-only portal has none to check.
PROXMOX_ENABLED="$(proxmox_enabled "$TMP_DIR/slots.json")" \
    || die "Could not read the enabled backends from the portal's export."
if ((PROXMOX_ENABLED)); then
    "$PYTHON" -c "$SITE_BANDS_PY" "$TMP_DIR/slots.json" "$NETGEN" "${VLAN_BANDS:-}" \
        || die "Set the same VLAN_BANDS in the portal's .env and scripts/site.env (then re-run jumpbox_configure.sh) before applying the policy."
fi

# What --apply needs before it may change anything, checked on its own so that
# term_changeover.sh can run it before its first destructive stage.
if ((PREFLIGHT)); then
    if ((PROXMOX_ENABLED == 0)); then
        printf '  Proxmox jumpbox peer check skipped: proxmox backend not enabled.\n'
    else
        require_site_var JUMPBOX_SSH
        read_jumpbox_status
        state="$(jumpbox_backend_state)"
        [[ "$state" == Running ]] \
            || die "The jumpbox's Tailscale ($JUMPBOX_SSH) is $state, not Running: no student can reach a lab, and the policy stage's peer check would fail. Bring it up first."
        printf '  ready: the jumpbox (%s) answers and its Tailscale is Running.\n' "$JUMPBOX_SSH"
    fi
    printf '  preflight passed: --apply can apply and check the policy.\n'
    exit 0
fi
# Before any change: with Proxmox, a reload nothing can check is never applied.
((APPLY == 0 || PROXMOX_ENABLED == 0)) || require_site_var JUMPBOX_SSH

changes="$(policy_changes)"
if [[ -z "$changes" ]]; then
    if ((APPLY == 0)); then
        printf '  nothing to do: the policy file Headscale serves matches the rendered one.\n'
        printf '  not checked: which policy Headscale has loaded; --apply signals a reload.\n'
        exit 0
    fi
    # An earlier --apply can have installed this file and then failed to
    # signal, leaving Headscale on the old rules; SIGHUP is safe to repeat.
    printf '  nothing to install: the policy file Headscale serves matches the rendered one.\n'
    check_policy
    reload_headscale
    printf '  signalled Headscale to reload that file.\n'
    verify_jumpbox_peers
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
    [[ -z "$readback" ]] || die "The policy file Headscale serves still differs after one restart."
fi
printf '  verified: the policy file Headscale serves matches the rendered one.\n'
verify_jumpbox_peers
