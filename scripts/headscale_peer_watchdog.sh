#!/usr/bin/env bash
# Keep the jumpbox from staying peerless: Headscale v0.27.1 can leave the
# jumpbox, the subnet router for every Proxmox lab, with no peers after a
# policy reload or after the jumpbox's Tailscale reconnects, and then no student
# reaches any lab until Headscale restarts.
#
# Usage: scripts/headscale_peer_watchdog.sh
#
# Runs the peer check apply_headscale_policy.sh --apply runs (headscale_peers.sh)
# and never writes or reloads the policy:
#   - every roster device is a jumpbox peer: log "ok", exit 0;
#   - the jumpbox's Tailscale is not Running: log it, exit 1, no restart (a
#     Headscale restart cannot fix the jumpbox);
#   - devices are missing: restart Headscale once, check again, exit 0 if they
#     are back. At most one restart per HEADSCALE_WATCHDOG_MIN_RESTART_INTERVAL
#     seconds (default 1800): within it, a still-missing device only logs and
#     exits 1, so a fault Headscale cannot fix never becomes a restart loop. A
#     healthy run ends the interval, so a new fault after a recovery is repaired
#     at once.
# A portal without the proxmox backend has no jumpbox: the check is skipped.
#
# Settings come from scripts/site.env, as for apply_headscale_policy.sh:
# PORTAL_HOST, PORTAL_APP_CONTAINER, HEADSCALE_HOST, HEADSCALE_DIR, JUMPBOX_SSH,
# JUMPBOX_PEER_WAIT (default 60), and optionally HEADSCALE_WATCHDOG_STATE (the
# file holding the last restart time, default
# ${XDG_STATE_HOME:-~/.local/state}/headscale-peer-watchdog/last-restart).
# Run it from a systemd timer every 5 minutes (docs/JUMPBOX.md, "Headscale peer
# watchdog"); every line it prints goes to the journal.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/site_env.sh"
source "$SCRIPT_DIR/headscale_peers.sh"

SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" \
    PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR JUMPBOX_SSH JUMPBOX_PEER_WAIT \
    HEADSCALE_WATCHDOG_STATE HEADSCALE_WATCHDOG_MIN_RESTART_INTERVAL
for name in PORTAL_HOST PORTAL_APP_CONTAINER HEADSCALE_HOST HEADSCALE_DIR; do
    require_site_var "$name"
done
JUMPBOX_PEER_WAIT="${JUMPBOX_PEER_WAIT:-60}"
MIN_INTERVAL="${HEADSCALE_WATCHDOG_MIN_RESTART_INTERVAL:-1800}"
STATE_FILE="${HEADSCALE_WATCHDOG_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/headscale-peer-watchdog/last-restart}"
for name in JUMPBOX_PEER_WAIT MIN_INTERVAL; do
    [[ "${!name}" =~ ^[0-9]+$ ]] || { printf 'ERROR: %s must be a number of seconds\n' "$name" >&2; exit 1; }
done
for name in PORTAL_APP_CONTAINER HEADSCALE_DIR; do
    [[ "${!name}" != *"'"* ]] || { printf "ERROR: %s must not contain an apostrophe\n" "$name" >&2; exit 1; }
done
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=10)
if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
else
    PYTHON="$(command -v python || true)"
fi
POLICY_TOOL="$SCRIPT_DIR/headscale_policy.py"
TMP_DIR=""

log() { printf 'headscale-peer-watchdog: %s\n' "$*"; }
die() {
    printf 'headscale-peer-watchdog: ERROR: %s\n' "$*" >&2
    exit 1
}
cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT

[[ -x "$PYTHON" ]] || die "Python not found: $PYTHON"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/headscale-peer-watchdog.XXXXXX")"
chmod 700 "$TMP_DIR"

fetch_slots > "$TMP_DIR/slots.json" || die "Could not export network slots from the portal."
proxmox="$(proxmox_enabled "$TMP_DIR/slots.json")" || die "Could not read the enabled backends from the portal's export."
if ((proxmox == 0)); then
    log "skipped: proxmox backend not enabled, so there is no jumpbox to check."
    exit 0
fi
require_site_var JUMPBOX_SSH
fetch_nodes > "$TMP_DIR/nodes.json" || die "Could not list Headscale's nodes."

missing=""
if wait_for_peers; then
    rm -f -- "$STATE_FILE"
    log "ok: the jumpbox lists every roster device as a peer."
    exit 0
fi
state="$(jumpbox_backend_state)"
[[ "$state" == Running ]] \
    || die "the jumpbox's Tailscale ($JUMPBOX_SSH) is $state; not restarting Headscale, which cannot fix that. No student can reach a lab until it is Running."
log "the jumpbox does not list these roster devices as peers:"
sed 's/^/    /' <<< "$missing"

now="$(date +%s)"
last="$(cat "$STATE_FILE" 2>/dev/null || true)"
[[ "$last" =~ ^[0-9]+$ ]] || last=0
if ((now - last < MIN_INTERVAL)); then
    die "Headscale was restarted at $(date -u -d "@$last" +%FT%TZ) and the devices are still missing; not restarting it again before $(date -u -d "@$((last + MIN_INTERVAL))" +%FT%TZ). Students cannot reach their labs. Check 'headscale nodes list-routes' and 'tailscale debug netmap' on the jumpbox."
fi
mkdir -p "$(dirname "$STATE_FILE")"
printf '%s\n' "$now" > "$STATE_FILE"
log "restarting Headscale once."
restart_headscale
if wait_for_peers; then
    log "recovered: the jumpbox lists every roster device as a peer after one Headscale restart."
    exit 0
fi
die "after one Headscale restart the jumpbox ($JUMPBOX_SSH) still does not list these roster devices as peers: ${missing//$'\n'/; }. Students cannot reach their labs. Check 'headscale nodes list-routes' and 'tailscale debug netmap' on the jumpbox."
