#!/usr/bin/env bash
# The jumpbox peer check, shared by apply_headscale_policy.sh (after every
# policy reload, and its --preflight) and headscale_peer_watchdog.sh.
#
# Headscale v0.27.1 has left the jumpbox, the subnet router for every Proxmox
# lab, with no peers: once after a policy reload and once after the jumpbox's
# Tailscale reconnected. Its packet filter still held every student's rules,
# yet no student reached any lab until Headscale was restarted.
#
# Source it; it defines functions only. The caller sets SSH_OPTIONS, PYTHON,
# POLICY_TOOL (scripts/headscale_policy.py), TMP_DIR, PORTAL_HOST,
# PORTAL_APP_CONTAINER, HEADSCALE_HOST, HEADSCALE_DIR, JUMPBOX_PEER_WAIT and,
# on a Proxmox site, JUMPBOX_SSH, and defines die().

fetch_slots() {
    ssh "${SSH_OPTIONS[@]}" "$PORTAL_HOST" \
        "docker exec '$PORTAL_APP_CONTAINER' python scripts/export_network_slots.py"
}

# Prints 1 when the export enables the proxmox backend, else 0. Validates the
# export's config the same way the render does (an empty list is refused).
proxmox_enabled() {
    "$PYTHON" -c 'import json, sys
sys.path.insert(0, sys.argv[2])
import headscale_policy
slots = json.load(open(sys.argv[1]))
print(int(headscale_policy.policy_inputs(slots)[0].proxmox_enabled))' "$1" "$(dirname "$POLICY_TOOL")"
}

fetch_nodes() {
    ssh "${SSH_OPTIONS[@]}" "$HEADSCALE_HOST" \
        "cd '$HEADSCALE_DIR' && docker compose exec -T headscale headscale nodes list -o json"
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

# Writes the jumpbox's `tailscale status --json` to $TMP_DIR/jumpbox-status.json.
read_jumpbox_status() {
    ssh "${SSH_OPTIONS[@]}" "$JUMPBOX_SSH" "tailscale status --json" > "$TMP_DIR/jumpbox-status.json" \
        || die "Could not read tailscale status on the jumpbox ($JUMPBOX_SSH). Check that SSH to it works (BatchMode, host key, key) and that this account can run 'tailscale status --json' there."
}

# Prints the BackendState of the last status read_jumpbox_status wrote.
jumpbox_backend_state() {
    "$PYTHON" -c 'import json, sys
print(json.load(open(sys.argv[1])).get("BackendState") or "unknown")' "$TMP_DIR/jumpbox-status.json" \
        || die "The jumpbox's tailscale status is not JSON."
}

# Prints the roster devices the jumpbox does not list as peers, one per line.
# Needs $TMP_DIR/nodes.json (fetch_nodes) and $TMP_DIR/slots.json (fetch_slots).
missing_peers() {
    read_jumpbox_status
    "$PYTHON" "$POLICY_TOOL" missing-peers "$TMP_DIR/nodes.json" "$TMP_DIR/slots.json" \
        "$TMP_DIR/jumpbox-status.json" || die "Could not compare the jumpbox's peers with Headscale's nodes."
}

# Polls for up to JUMPBOX_PEER_WAIT seconds; leaves the last result in $missing.
wait_for_peers() {
    local deadline=$((SECONDS + JUMPBOX_PEER_WAIT))
    while :; do
        missing="$(missing_peers)" || exit 1
        [[ -z "$missing" ]] && return 0
        ((SECONDS < deadline)) || return 1
        sleep 3
    done
}
