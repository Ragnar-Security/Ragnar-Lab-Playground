#!/usr/bin/env bash
#
# Configure the lab jumpbox: the gateway, DHCP server and Tailscale subnet
# router for every lab VLAN. Runs ON the jumpbox, as root.
#
# Usage: sudo scripts/jumpbox_configure.sh [--apply] [--no-try]
#             [--tailscale [--authkey-stdin]] [--ssh-keys-only] [--ssh-staff-only [--i-have-console]]
#             [--monitoring]
#
# Dry run by default: prints a diff of every file it would write and every
# command it would run, and changes nothing. --apply makes the changes; a
# second --apply on a converged jumpbox changes nothing.
#
# Always handled:
#   - an LVM root that does not fill its volume group is grown into it
#   - netplan: management NIC on DHCP, one vlan<N> gateway per lab VLAN
#   - dnsmasq: DHCP and DNS forwarding on the lab VLANs only
#   - sysctl: IPv4/IPv6 forwarding, loose reverse-path filtering
# Options:
#   --tailscale       join Headscale as the 10.0.0.0/8 subnet router. The
#                     auth key comes from JUMPBOX_TS_AUTHKEY in the
#                     environment, or one line on stdin with --authkey-stdin;
#                     it never appears on a command line.
#   --ssh-keys-only   PasswordAuthentication no. Refuses unless
#                     JUMPBOX_ADMIN_USER has a usable authorized key.
#   --ssh-staff-only  nftables table that drops inbound tcp/22 except on lo, the
#                     management NIC and tailscale0 from JUMPBOX_SSH_STAFF_IPS.
#                     Forwarded student traffic is untouched. Refuses when
#                     that list is empty and the management NIC is unknown, when
#                     nft is missing, and (over SSH) when this session is not
#                     provably covered by the rules, unless --i-have-console.
#                     Filters every port sshd/ssh.socket listen on; re-run after
#                     changing the SSH port.
#   --monitoring      Zeek on the trunk plus an hourly RITA import. Refuses
#                     below JUMPBOX_MONITORING_MIN_GB of root disk.
#   --no-try          use `netplan apply` instead of `netplan try`, which
#                     needs a terminal to confirm and reverts on its own.
#
# Settings come from scripts/site.env (see site.env.example); docs/JUMPBOX.md
# has the procedure. JUMPBOX_SYSROOT is a test hook that prefixes the system
# paths this script reads and writes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=site_env.sh
source "$SCRIPT_DIR/site_env.sh"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" HEADSCALE_URL JUMPBOX_ADMIN_USER JUMPBOX_MGMT_IF \
    JUMPBOX_TRUNK_IF JUMPBOX_VLAN_BANDS JUMPBOX_DHCP_START JUMPBOX_DHCP_END \
    JUMPBOX_DHCP_LEASE JUMPBOX_DNS_UPSTREAM JUMPBOX_DHCP_RESERVATIONS \
    JUMPBOX_NETPLAN_FILE JUMPBOX_DNSMASQ_FILE JUMPBOX_NETPLAN_TRY_TIMEOUT \
    JUMPBOX_TS_ROUTES JUMPBOX_TS_TAG JUMPBOX_TS_HOSTNAME JUMPBOX_TS_KEY_DIR \
    JUMPBOX_SSH_STAFF_IPS JUMPBOX_MONITORING_MIN_GB JUMPBOX_RITA_VERSION JUMPBOX_RITA_DATABASE

# The key must not reach any child process's environment or argv.
TS_KEY="${JUMPBOX_TS_AUTHKEY:-}"
unset JUMPBOX_TS_AUTHKEY

NETGEN="$SCRIPT_DIR/jumpbox_netgen.py"
R="${JUMPBOX_SYSROOT:-}"
NETPLAN_FILE="${JUMPBOX_NETPLAN_FILE:-/etc/netplan/60-jumpbox-vlans.yaml}"
DNSMASQ_FILE="${JUMPBOX_DNSMASQ_FILE:-/etc/dnsmasq.d/jumpbox.conf}"
SYSCTL_FILE=/etc/sysctl.d/90-jumpbox-router.conf
SSHD_DROPIN=/etc/ssh/sshd_config.d/10-jumpbox-keys-only.conf
SSH_NFT_FILE=/etc/nftables.d/jumpbox-ssh.nft
SSH_UNIT_NAME=jumpbox-ssh-filter.service
SSH_UNIT_FILE=/etc/systemd/system/jumpbox-ssh-filter.service
TRY_TIMEOUT="${JUMPBOX_NETPLAN_TRY_TIMEOUT:-120}"
TS_ROUTES="${JUMPBOX_TS_ROUTES:-10.0.0.0/8}"
TS_TAG="${JUMPBOX_TS_TAG:-tag:jumpbox}"
MIN_GB="${JUMPBOX_MONITORING_MIN_GB:-64}"
RITA_VERSION="${JUMPBOX_RITA_VERSION:-v5.1.1}"
RITA_DATABASE="${JUMPBOX_RITA_DATABASE:-jumpbox}"
ZEEK_ETC=/opt/zeek/etc

APPLY=0 NO_TRY=0 DO_TS=0 KEY_STDIN=0 DO_SSH=0 DO_SSHF=0 DO_MON=0 I_HAVE_CONSOLE=0
usage() { sed -n '3,41p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --no-try) NO_TRY=1 ;;
        --tailscale) DO_TS=1 ;;
        --authkey-stdin) KEY_STDIN=1 ;;
        --ssh-keys-only) DO_SSH=1 ;;
        --ssh-staff-only) DO_SSHF=1 ;;
        --i-have-console) I_HAVE_CONSOLE=1 ;;
        --monitoring) DO_MON=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
BLOCKERS=0
block() { printf 'ERROR: %s\n' "$*" >&2; BLOCKERS=$((BLOCKERS + 1)); }
# Every state-changing command goes through run: shown in a dry run, executed with --apply.
run() {
    if [ "$APPLY" -eq 1 ]; then say "+ $*"; "$@"; else say "would run: $*"; fi
}

[ "$(id -u)" -eq 0 ] || die "run as root (sudo); the netplan files are readable only by root"
command -v python3 >/dev/null || die "python3 is required"

if [ "$KEY_STDIN" -eq 1 ]; then
    if [ -t 0 ]; then
        IFS= read -r -s -p "Headscale auth key: " TS_KEY || true
        echo >&2
    else
        IFS= read -r TS_KEY || true
    fi
    [ -n "$TS_KEY" ] || die "--authkey-stdin given but no key was read"
fi

TMP_BASE="${TMPDIR:-/tmp}"
TMP="$(mktemp -d "${TMP_BASE%/}/jumpbox.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# --- staging ------------------------------------------------------------------

CHANGED=""   # space-separated targets whose content or mode differs
# stage <target> <mode> <staged-file>: diff against the live file and record a change.
stage() {
    local target="$1" mode="$2" staged="$3" live="$R$1"
    if [ -f "$live" ] && cmp -s "$live" "$staged" && [ -n "$(find "$live" -prune -perm "$mode")" ]; then
        say "unchanged: $target"
        return 0
    fi
    CHANGED="$CHANGED $target"
    say "changes:   $target (mode $mode)"
    python3 "$NETGEN" diff --label "$target" "$live" "$staged"
}
changed() { case " $CHANGED " in *" $1 "*) return 0 ;; esac; return 1; }
# install_file <target> <mode> <staged-file>: atomic replace.
install_file() {
    local live="$R$1"
    mkdir -p "$(dirname "$live")"
    cp "$3" "$live.jumpbox-new"
    chmod "$2" "$live.jumpbox-new"
    mv -f "$live.jumpbox-new" "$live"
    say "wrote:     $1"
}

# --- disk ----------------------------------------------------------------------

# The Ubuntu server installer gives the root LV half of its volume group. A
# full root once stopped dnsmasq from writing its lease file.
GROW_ROOT=""
plan_root_growth() {
    local src vg free
    src="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
    case "$src" in
        /dev/mapper/*|/dev/dm-*) ;;
        *) say "disk:      root ($src) is not on LVM; cloud images grow it at first boot"; return 0 ;;
    esac
    vg="$(lvs --noheadings -o vg_name "$src" 2>/dev/null | tr -d '[:space:]')"
    free="$(vgs --noheadings -o vg_free_count "$vg" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$free" ] && [ "$free" -gt 0 ]; then
        GROW_ROOT="$src"
        say "disk:      $vg has $free free extents; root will grow into them"
    else
        say "unchanged: root LV fills ${vg:-its volume group}"
    fi
}

# --- network -------------------------------------------------------------------

MGMT_IF="${JUMPBOX_MGMT_IF:-}"
TRUNK_IF="${JUMPBOX_TRUNK_IF:-}"
detect_interfaces() {
    local dev name candidates="" count=0
    if [ -z "$MGMT_IF" ] && command -v ip >/dev/null; then
        MGMT_IF="$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')"
    fi
    if [ -z "$TRUNK_IF" ]; then
        for dev in "$R"/sys/class/net/*; do
            name="$(basename "$dev")"
            [ -e "$dev/device" ] && [ "$name" != "$MGMT_IF" ] || continue
            candidates="$candidates $name"
            count=$((count + 1))
        done
        [ "$count" -eq 1 ] && TRUNK_IF="${candidates# }"
    fi
    if [ "$DO_SSHF" -eq 1 ] && [ -z "$MGMT_IF" ] && [ -z "${JUMPBOX_SSH_STAFF_IPS:-}" ]; then
        die "--ssh-staff-only would lock everyone out of SSH: JUMPBOX_SSH_STAFF_IPS is empty and the management interface cannot be determined. Set one of them (JUMPBOX_MGMT_IF)."
    fi
    [ -n "$MGMT_IF" ] || die "cannot detect the management interface; set JUMPBOX_MGMT_IF"
    [ -n "$TRUNK_IF" ] || die "cannot pick the trunk interface (candidates:${candidates:- none}); set JUMPBOX_TRUNK_IF"
    [ -e "$R/sys/class/net/$MGMT_IF" ] || die "management interface $MGMT_IF does not exist"
    [ -e "$R/sys/class/net/$TRUNK_IF" ] || die "trunk interface $TRUNK_IF does not exist"
    say "network:   management $MGMT_IF (DHCP), trunk $TRUNK_IF (lab VLANs)"
}

render_network() {
    local bands=(--bands "${JUMPBOX_VLAN_BANDS:-}")
    python3 "$NETGEN" vlans "${bands[@]}" > "$TMP/vlans"
    python3 "$NETGEN" netplan "${bands[@]}" --mgmt-if "$MGMT_IF" --trunk-if "$TRUNK_IF" > "$TMP/netplan"
    python3 "$NETGEN" dnsmasq "${bands[@]}" --mgmt-if "$MGMT_IF" \
        --upstream "${JUMPBOX_DNS_UPSTREAM:-8.8.8.8}" \
        --reservations "${JUMPBOX_DHCP_RESERVATIONS:-}" \
        --dhcp-start "${JUMPBOX_DHCP_START:-50}" --dhcp-end "${JUMPBOX_DHCP_END:-254}" \
        --lease "${JUMPBOX_DHCP_LEASE:-12h}" > "$TMP/dnsmasq"
    python3 "$NETGEN" sysctl > "$TMP/sysctl"
}

# A second definition of the same VLANs (the hand-built 01-vlans.yaml or
# /etc/dnsmasq.conf) would collide with ours; the operator retires it first.
check_foreign_config() {
    local f names
    for f in "$R"/etc/netplan/*.yaml; do
        [ -f "$f" ] && [ "$f" != "$R$NETPLAN_FILE" ] || continue
        # Captured first: under pipefail, grep -q closing a pipe early reads as "no match".
        names="$(grep -oE '^[[:space:]]+vlan[0-9]+:' "$f" | tr -d ' :\t' || true)"
        if grep -qFxf "$TMP/vlans" <<<"$names" \
            || grep -qE "^[[:space:]]+link:[[:space:]]*${TRUNK_IF}[[:space:]]*$" "$f"; then
            block "${f#"$R"} already defines VLANs on $TRUNK_IF. Move it aside, or set JUMPBOX_NETPLAN_FILE=${f#"$R"} so this script owns it."
        fi
    done
    for f in "$R"/etc/dnsmasq.conf "$R"/etc/dnsmasq.d/*; do
        [ -f "$f" ] && [ "$f" != "$R$DNSMASQ_FILE" ] || continue
        if grep -qE '^[[:space:]]*(dhcp-range|dhcp-host|interface|listen-address|bind-interfaces|except-interface)([[:space:]]*=|[[:space:]]*$)' "$f"; then
            block "${f#"$R"} already configures dnsmasq DHCP or listening. Move it aside (keep a copy), or set JUMPBOX_DNSMASQ_FILE=${f#"$R"} so this script owns it."
        fi
    done
}

# --- tailscale -----------------------------------------------------------------

TS_STATE=""
plan_tailscale() {
    [ -n "${HEADSCALE_URL:-}" ] || block "HEADSCALE_URL is unset; set it in scripts/site.env"
    if command -v tailscale >/dev/null; then
        TS_STATE="$(tailscale status --json 2>/dev/null | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("BackendState", ""))
except ValueError:
    print("")' || true)"
    fi
    say "tailscale: state ${TS_STATE:-not installed}"
    if [ "$TS_STATE" != "Running" ] && [ -z "$TS_KEY" ]; then
        if [ "$APPLY" -eq 1 ]; then
            block "joining Headscale needs a pre-auth key: export JUMPBOX_TS_AUTHKEY or pass --authkey-stdin"
        else
            say "tailscale: --apply will need a pre-auth key (JUMPBOX_TS_AUTHKEY or --authkey-stdin)"
        fi
    fi
}

tailscale_prefs_match() {
    tailscale debug prefs 2>/dev/null | ROUTES="$TS_ROUTES" python3 -c 'import json, os, sys
try:
    p = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
want = sorted(r for r in os.environ["ROUTES"].replace(",", " ").split())
ok = sorted(p.get("AdvertiseRoutes") or []) == want and p.get("NoSNAT") and p.get("NoStatefulFiltering")
sys.exit(0 if ok else 1)'
}

apply_tailscale() {
    local keydir keyfile cmd
    if ! command -v tailscale >/dev/null; then
        run snap install tailscale
        hash -r
    fi
    if [ "$TS_STATE" = "Running" ]; then
        if tailscale_prefs_match; then
            say "unchanged: tailscale advertises $TS_ROUTES without SNAT"
        else
            run tailscale set --advertise-routes="$TS_ROUTES" --snat-subnet-routes=false \
                --stateful-filtering=false
        fi
        return 0
    fi
    # The snap's CLI cannot read the host /tmp; its common directory it can.
    keydir="${JUMPBOX_TS_KEY_DIR:-}"
    if [ -z "$keydir" ]; then
        case "$(command -v tailscale || true)" in
            /snap/*) keydir=/var/snap/tailscale/common ;;
            *) keydir="$TMP" ;;
        esac
    fi
    cmd=(tailscale up --login-server="$HEADSCALE_URL" --advertise-routes="$TS_ROUTES"
         --advertise-tags="$TS_TAG" --snat-subnet-routes=false --stateful-filtering=false)
    [ -n "${JUMPBOX_TS_HOSTNAME:-}" ] && cmd+=(--hostname="$JUMPBOX_TS_HOSTNAME")
    if [ "$APPLY" -ne 1 ]; then
        run "${cmd[@]}" --auth-key=file:"$keydir/<auth-key-file>"
        return 0
    fi
    keyfile="$(umask 077; mktemp "$keydir/ts-authkey.XXXXXX")"
    # printf is a builtin: the key is in no process's argv.
    printf '%s' "$TS_KEY" > "$keyfile"
    TS_KEY=""
    if ! run "${cmd[@]}" --auth-key=file:"$keyfile"; then
        rm -f "$keyfile"
        die "tailscale up failed"
    fi
    rm -f "$keyfile"
    say "tailscale: joined. Approve the $TS_ROUTES route in Headscale (docs/JUMPBOX.md) unless autoApprovers covers $TS_TAG."
}

# --- ssh -----------------------------------------------------------------------

plan_ssh_keys_only() {
    local admin="${JUMPBOX_ADMIN_USER:-${SUDO_USER:-}}" home keys
    if [ -z "$admin" ]; then
        block "--ssh-keys-only: set JUMPBOX_ADMIN_USER to the account you log in with"
        return 0
    fi
    home="$(getent passwd "$admin" | cut -d: -f6 || true)"
    keys="$home/.ssh/authorized_keys"
    if [ -z "$home" ]; then
        block "--ssh-keys-only: no such user $admin"
    elif ! grep -qE '^([^#]*[[:space:]])?(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)[[:space:]]+AAAA[0-9A-Za-z+/]+=*' "$keys" 2>/dev/null; then
        block "--ssh-keys-only would lock $admin out: $keys has no authorized key. Add one and test a key login first."
    elif [ -n "$(find "$keys" "$home/.ssh" "$home" -prune -perm -022 2>/dev/null)" ]; then
        block "--ssh-keys-only would lock $admin out: sshd ignores $keys while it, $home/.ssh or $home is group- or world-writable"
    fi
    {
        say "# Generated by scripts/jumpbox_configure.sh --ssh-keys-only."
        say "# sshd keeps the first value it reads and includes this directory before"
        say "# its own settings, so this file must sort before 50-cloud-init.conf."
        say "PasswordAuthentication no"
        say "KbdInteractiveAuthentication no"
    } > "$TMP/sshd"
    stage "$SSHD_DROPIN" 644 "$TMP/sshd"
}

apply_ssh_keys_only() {
    changed "$SSHD_DROPIN" || return 0
    local previous="$TMP/sshd.previous"
    [ -f "$R$SSHD_DROPIN" ] && cp "$R$SSHD_DROPIN" "$previous"
    install_file "$SSHD_DROPIN" 644 "$TMP/sshd"
    if ! run sshd -t; then
        if [ -f "$previous" ]; then cp "$previous" "$R$SSHD_DROPIN"; else rm -f "$R$SSHD_DROPIN"; fi
        die "sshd -t rejected the configuration; restored the previous state"
    fi
    run systemctl try-reload-or-restart ssh
    if ! grep -qx 'passwordauthentication no' <<<"$(sshd -T 2>/dev/null || true)"; then
        printf 'WARNING: sshd -T still reports password authentication; another sshd_config line sorts first\n' >&2
    fi
}

# Ports sshd listens on, from its effective config and from ssh.socket (Ubuntu
# socket activation ignores sshd_config's Port). A port left out stays open.
ssh_ports() {
    {
        if command -v sshd >/dev/null; then sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' || true; fi
        if command -v systemctl >/dev/null; then
            systemctl show -p Listen ssh.socket 2>/dev/null \
                | awk '/^Listen=/ && /\(Stream\)/ { sub(/ .*/, ""); n = split($0, a, ":"); if (a[n] ~ /^[0-9]+$/) print a[n] }' || true
        fi
    } | sort -un | tr '\n' ' '
}

# iface_addrs <iface>: its IP addresses, one per line, prefix length removed.
iface_addrs() {
    command -v ip >/dev/null || return 0
    ip -o addr show dev "$1" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "inet" || $i == "inet6") {sub(/\/.*/, "", $(i + 1)); print $(i + 1)}}'
}

# Refuse to apply from an SSH session the new rules could cut off. Provable
# survivors: loopback, arrival on the management NIC, or a listed staff source
# arriving on tailscale0 (approximated by the local address being tailscale0's).
ssh_session_survives() {
    local src _sport dst _dport
    read -r src _sport dst _dport <<<"$SSH_CONNECTION"
    case "$src" in 127.*|::1) return 0 ;; esac
    iface_addrs "$MGMT_IF" | grep -qxF "$dst" && return 0
    if iface_addrs tailscale0 | grep -qxF "$dst" \
        && python3 "$NETGEN" staff-has --staff-ips "${JUMPBOX_SSH_STAFF_IPS:-}" "$src"; then
        return 0
    fi
    return 1
}

# nft drops every SSH packet that no rule accepts, including the session
# running this script, so the plan says who stays reachable.
plan_ssh_filter() {
    local ports
    ports="$(ssh_ports)"
    say "ssh:       filtering tcp port(s) ${ports:-22}"
    python3 "$NETGEN" ssh-filter --mgmt-if "$MGMT_IF" --staff-ips "${JUMPBOX_SSH_STAFF_IPS:-}" --ports "$ports" > "$TMP/ssh-filter.nft" \
        || { block "--ssh-staff-only: could not render the ruleset (check JUMPBOX_SSH_STAFF_IPS, JUMPBOX_MGMT_IF and the SSH ports: ${ports:-22})"; return 0; }
    python3 "$NETGEN" ssh-filter-unit --nft-file "$SSH_NFT_FILE" > "$TMP/ssh-filter.service"
    if ! command -v nft >/dev/null; then
        if [ "$APPLY" -eq 1 ]; then
            block "--ssh-staff-only: nft is not installed, so the ruleset cannot be validated or loaded. Install the nftables package first."
            return 0
        fi
        say "note: nft not installed; the ruleset below is unvalidated. Install the nftables package before --apply."
    elif ! nft -c -f "$TMP/ssh-filter.nft"; then
        block "--ssh-staff-only: nft -c -f rejected the generated ruleset"
        return 0
    fi
    [ -n "${JUMPBOX_SSH_STAFF_IPS:-}" ] || say "WARNING: JUMPBOX_SSH_STAFF_IPS is empty; SSH will work only on loopback and $MGMT_IF" >&2
    if [ -n "${SSH_CONNECTION:-}" ]; then
        say "WARNING: running over SSH; this session survives only if it arrives on $MGMT_IF or from a listed staff address" >&2
        if [ "$APPLY" -eq 1 ] && [ "$I_HAVE_CONSOLE" -eq 0 ] && ! ssh_session_survives; then
            block "--ssh-staff-only: this SSH session (${SSH_CONNECTION%% *}) is not provably covered by the new rules and would be cut off. Run from the console or a listed staff address, or pass --i-have-console if you can recover without SSH."
        fi
    fi
    stage "$SSH_NFT_FILE" 644 "$TMP/ssh-filter.nft"
    stage "$SSH_UNIT_FILE" 644 "$TMP/ssh-filter.service"
}

# Never restart an active unit: ExecStop deletes the table, leaving SSH open
# until ExecStart reloads it. reload-or-restart runs ExecReload, an atomic swap.
ssh_filter_activate() {
    if changed "$SSH_NFT_FILE" || changed "$SSH_UNIT_FILE"; then
        run systemctl daemon-reload
        run systemctl enable "$SSH_UNIT_NAME"
        if systemctl is-active --quiet "$SSH_UNIT_NAME"; then
            run systemctl reload-or-restart "$SSH_UNIT_NAME"
        else
            run systemctl start "$SSH_UNIT_NAME"
        fi
    fi
}

apply_ssh_filter() {
    changed "$SSH_NFT_FILE" && install_file "$SSH_NFT_FILE" 644 "$TMP/ssh-filter.nft"
    changed "$SSH_UNIT_FILE" && install_file "$SSH_UNIT_FILE" 644 "$TMP/ssh-filter.service"
    ssh_filter_activate
}

# --- monitoring ----------------------------------------------------------------

plan_monitoring() {
    local kb gb
    kb="$(df -Pk / | awk 'NR == 2 {print $2}')"
    gb=$((kb / 1024 / 1024))
    if [ "$gb" -lt "$MIN_GB" ]; then
        block "--monitoring: / is ${gb} GB; Zeek logs and RITA's ClickHouse need at least ${MIN_GB} GB (JUMPBOX_MONITORING_MIN_GB). Grow the disk first: a full root disk stops DHCP."
    fi
    python3 "$NETGEN" zeek-node --trunk-if "$TRUNK_IF" > "$TMP/zeek-node"
    python3 "$NETGEN" zeek-networks --routes "$TS_ROUTES" > "$TMP/zeek-networks"
    say "*/5 * * * * root /opt/zeek/bin/zeekctl cron" > "$TMP/zeek-cron"
    # Minute 20 lets Zeek finish its hourly log rotation; flock stops overlap.
    say "20 * * * * root flock -n /run/rita-import.lock /usr/local/bin/rita import --rolling --database=$RITA_DATABASE --logs=/opt/zeek/logs/ >> /var/log/rita-import.log 2>&1" > "$TMP/rita-cron"
    [ -x "$R/opt/zeek/bin/zeekctl" ] || say "monitoring: Zeek is not installed; --apply installs it from the openSUSE security:zeek repository"
    command -v rita >/dev/null || say "monitoring: RITA is not installed; --apply installs RITA $RITA_VERSION (Docker + ClickHouse)"
    stage "$ZEEK_ETC/node.cfg" 644 "$TMP/zeek-node"
    stage "$ZEEK_ETC/networks.cfg" 644 "$TMP/zeek-networks"
    stage /etc/cron.d/zeekctl 644 "$TMP/zeek-cron"
    stage /etc/cron.d/rita-import 644 "$TMP/rita-cron"
}

install_zeek() {
    local version_id repo
    # shellcheck disable=SC1091,SC2153 # VERSION_ID comes from os-release
    version_id="$(. /etc/os-release && echo "$VERSION_ID")"
    repo="download.opensuse.org/repositories/security:/zeek/xUbuntu_$version_id"
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL "https://$repo/Release.key" | gpg --dearmor --yes -o /etc/apt/keyrings/zeek.gpg
    printf 'deb [signed-by=/etc/apt/keyrings/zeek.gpg] https://%s/ /\n' "$repo" > /etc/apt/sources.list.d/zeek.list
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y zeek
}

install_rita() {
    local tarball="$TMP/rita-$RITA_VERSION.tar.gz"
    curl -fsSL -o "$tarball" "https://github.com/activecm/rita/releases/download/$RITA_VERSION/rita-$RITA_VERSION.tar.gz"
    tar -xzf "$tarball" -C "$TMP"
    (cd "$TMP/rita-$RITA_VERSION-installer" && ./install_rita.sh localhost)
}

apply_monitoring() {
    [ -x "$R/opt/zeek/bin/zeekctl" ] || run install_zeek
    command -v rita >/dev/null || run install_rita
    local target redeploy=0
    for target in "$ZEEK_ETC/node.cfg" "$ZEEK_ETC/networks.cfg"; do
        if changed "$target"; then redeploy=1; fi
    done
    changed "$ZEEK_ETC/node.cfg" && install_file "$ZEEK_ETC/node.cfg" 644 "$TMP/zeek-node"
    changed "$ZEEK_ETC/networks.cfg" && install_file "$ZEEK_ETC/networks.cfg" 644 "$TMP/zeek-networks"
    changed /etc/cron.d/zeekctl && install_file /etc/cron.d/zeekctl 644 "$TMP/zeek-cron"
    changed /etc/cron.d/rita-import && install_file /etc/cron.d/rita-import 644 "$TMP/rita-cron"
    # Zeek keeps logs forever by default; a week bounds the disk it uses.
    if ! grep -qx 'LogExpireInterval = 7day' "$R$ZEEK_ETC/zeekctl.cfg"; then
        run sed -i -E 's/^#?[[:space:]]*LogExpireInterval[[:space:]]*=.*/LogExpireInterval = 7day/' "$R$ZEEK_ETC/zeekctl.cfg"
        redeploy=1
    fi
    [ "$redeploy" -eq 0 ] || run "$R/opt/zeek/bin/zeekctl" deploy
}

# --- plan ----------------------------------------------------------------------

if [ "$APPLY" -eq 1 ]; then
    say "== jumpbox configure: APPLY =="
else
    say "== jumpbox configure: dry run (nothing will change) =="
fi
plan_root_growth
detect_interfaces
render_network
check_foreign_config
stage "$SYSCTL_FILE" 644 "$TMP/sysctl"
stage "$DNSMASQ_FILE" 644 "$TMP/dnsmasq"
stage "$NETPLAN_FILE" 600 "$TMP/netplan"
if changed "$NETPLAN_FILE" && [ "$NO_TRY" -eq 0 ] && [ "$APPLY" -eq 1 ] && [ ! -t 0 ]; then
    block "netplan try needs a terminal to confirm the new network config (it reverts on its own if you lose access). Run from an interactive shell, or pass --no-try to apply without the safety net."
fi
[ "$DO_TS" -eq 1 ] && plan_tailscale
[ "$DO_SSH" -eq 1 ] && plan_ssh_keys_only
[ "$DO_SSHF" -eq 1 ] && plan_ssh_filter
[ "$DO_MON" -eq 1 ] && plan_monitoring

if [ "$BLOCKERS" -gt 0 ]; then
    die "$BLOCKERS problem(s) above; nothing was changed"
fi

if [ "$APPLY" -ne 1 ]; then
    [ -n "$GROW_ROOT" ] && run lvextend -r -l +100%FREE "$GROW_ROOT"
    changed "$SYSCTL_FILE" && run sysctl -p "$SYSCTL_FILE"
    if changed "$NETPLAN_FILE"; then
        if [ "$NO_TRY" -eq 1 ]; then run netplan apply; else run netplan try --timeout "$TRY_TIMEOUT"; fi
    fi
    if changed "$DNSMASQ_FILE" || changed "$NETPLAN_FILE"; then run systemctl restart dnsmasq; fi
    [ "$DO_TS" -eq 1 ] && apply_tailscale
    if [ "$DO_SSH" -eq 1 ] && changed "$SSHD_DROPIN"; then run systemctl try-reload-or-restart ssh; fi
    [ "$DO_SSHF" -eq 1 ] && ssh_filter_activate
    say ""
    say "Dry run only. Re-run with --apply to make these changes."
    exit 0
fi

# --- apply ---------------------------------------------------------------------

[ -n "$GROW_ROOT" ] && run lvextend -r -l +100%FREE "$GROW_ROOT"

if changed "$SYSCTL_FILE"; then
    install_file "$SYSCTL_FILE" 644 "$TMP/sysctl"
    run sysctl -p "$R$SYSCTL_FILE"
fi

# The config goes in before the package: a stock dnsmasq first start listens
# on every interface and can fail on port 53 next to systemd-resolved.
if changed "$DNSMASQ_FILE"; then
    if command -v dnsmasq >/dev/null; then
        dnsmasq --test --conf-file="$TMP/dnsmasq" || die "dnsmasq rejected the generated configuration"
    fi
    install_file "$DNSMASQ_FILE" 644 "$TMP/dnsmasq"
fi

missing=""
command -v dnsmasq >/dev/null || missing="$missing dnsmasq"
command -v qemu-ga >/dev/null || missing="$missing qemu-guest-agent"
if [ -n "$missing" ]; then
    run apt-get update
    # shellcheck disable=SC2086 # the package list is meant to word-split
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y $missing
    dnsmasq --test --conf-file="$R$DNSMASQ_FILE" || die "dnsmasq rejected $DNSMASQ_FILE"
fi

if changed "$NETPLAN_FILE"; then
    # Validate against a copy of the live tree before touching it.
    mkdir -p "$TMP/np/etc/netplan"
    cp "$R"/etc/netplan/*.yaml "$TMP/np/etc/netplan/" 2>/dev/null || true
    cp "$TMP/netplan" "$TMP/np$NETPLAN_FILE"
    netplan generate --root-dir "$TMP/np" || die "netplan rejected the generated configuration"
    previous="$TMP/netplan.previous"
    [ -f "$R$NETPLAN_FILE" ] && cp -p "$R$NETPLAN_FILE" "$previous"
    install_file "$NETPLAN_FILE" 600 "$TMP/netplan"
    if [ "$NO_TRY" -eq 1 ]; then
        applied=0; run netplan apply && applied=1
    else
        say "netplan try: press ENTER within ${TRY_TIMEOUT}s once you have confirmed access, or it reverts."
        applied=0; run netplan try --timeout "$TRY_TIMEOUT" && applied=1
    fi
    if [ "$applied" -ne 1 ]; then
        if [ -f "$previous" ]; then cp -p "$previous" "$R$NETPLAN_FILE"; else rm -f "$R$NETPLAN_FILE"; fi
        die "the network change was not confirmed; restored ${NETPLAN_FILE}"
    fi
fi

if changed "$DNSMASQ_FILE" || changed "$NETPLAN_FILE"; then
    run systemctl enable dnsmasq
    run systemctl restart dnsmasq
fi

[ "$DO_TS" -eq 1 ] && apply_tailscale
[ "$DO_SSH" -eq 1 ] && apply_ssh_keys_only
[ "$DO_SSHF" -eq 1 ] && apply_ssh_filter
[ "$DO_MON" -eq 1 ] && apply_monitoring

say ""
say "Done. Re-running with --apply should now report every file unchanged."
