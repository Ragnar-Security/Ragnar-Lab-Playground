#!/usr/bin/env bash
#
# Create the Proxmox base template for Linux image builds,
# base-ubuntu-2404-<yyyymmdd>, from the official Ubuntu 24.04 cloud image.
# Runs on the build host, next to config.env, and reaches PVE_HOST as root
# over SSH (key auth), as build_image.sh does.
#
# Usage: scripts/image_build/proxmox_base_ubuntu.sh [--apply] [options]
#   --image PATH      cloud image on PVE_NODE
#                     (default /var/lib/vz/template/iso/noble-server-cloudimg-amd64.img)
#   --snippets STORE  storage with the `snippets` content type, for the
#                     first-boot user-data (default local)
#   --vmid N          VMID in 8000-8499 (default: the lowest V with V and
#                     V+500 free, so the replica can sit at V+500)
#   --date YYYYMMDD   date in the name (default today, UTC)
#   --disk SIZE       root disk, e.g. 20G (default 20G); the Proxmox backend
#                     does not resize, so this is every image's disk size
#
# Dry run by default: reads the cluster and prints every command that would
# change it. --apply runs them. It never touches an existing guest: a VMID in
# use, a VMID outside 8000-8499, or a template name already taken is an
# error. Config: the PVE_ keys of config.env (IMAGE_BUILD_CONFIG overrides
# the path); it uses PVE_HOST, PVE_NODE, PVE_STORAGE, PVE_BUILD_BRIDGE and
# PVE_BUILD_VLAN.
#
# The template meets the README's base-template contract:
#   - user builder: passwordless sudo, password locked, no SSH key;
#   - openssh-server (ssh.socket enabled) and qemu-guest-agent installed;
#   - DHCP on any en*/eth* interface (netplan matched by name, not MAC:
#     a clone has a new MAC and no cloud-init drive), cloud-init's own
#     network config disabled;
#   - no SSH key, authorized_keys or password anywhere; no host keys (a
#     drop-in runs ssh-keygen -A when ssh.service starts, as the seal's does);
#   - cloud-init cleaned, /etc/machine-id empty, no cloud-init drive.
# First boot runs cloud-init from a user-data snippet (packages, user); the
# rest is done through the guest agent; the snippet is deleted afterwards.
#
# A run that fails or is interrupted (INT, TERM, HUP) after it began to
# create the VM destroys that VM, but only after the node shows it is not a
# template and its description carries this run's construction marker
# (proxmox-base-build=<time>-<random>, written by qm create itself); any
# other VM at that VMID is named and left alone. The snippet is deleted on
# every exit, and an error names it if the node cannot confirm it is gone.
#
# Then prove the base (README "Base-template contract") with a test clone and
# replicate it: scripts/sync_template.sh <PVE_HOST> <vmid> <replica host>
#   <replica storage> <vmid+500>.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${IMAGE_BUILD_CONFIG:-$SCRIPT_DIR/config.env}"

APPLY=0
IMAGE=/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img
SNIPPETS=local
VMID=""
DATE="$(date -u +%Y%m%d)"
DISK=20G
BUILD_MEMORY=2048
BUILD_CORES=2
MIN_AVAILABLE_KIB=$((16 * 1024 * 1024))
BLOCK_MIN=8000
BLOCK_MAX=8499
REPLICA_OFFSET=500

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --image) IMAGE="${2:?--image needs a path}"; shift ;;
        --snippets) SNIPPETS="${2:?--snippets needs a storage}"; shift ;;
        --vmid) VMID="${2:?--vmid needs a VMID}"; shift ;;
        --date) DATE="${2:?--date needs YYYYMMDD}"; shift ;;
        --disk) DISK="${2:?--disk needs a size}"; shift ;;
        -h|--help) sed -n '3,47p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

# --- config: only the PVE_ keys come back, as in build_image.sh ------------
[ -f "$CONFIG" ] || die "config $CONFIG does not exist"
assignments="$(
    # shellcheck source=/dev/null
    source "$CONFIG" >&2 || exit 1
    for name in $(compgen -v PVE_); do
        printf '%s=%q\n' "$name" "${!name}"
    done
)" || die "config $CONFIG could not be loaded"
eval "$assignments"
PVE_NODE="${PVE_NODE:-pve}"
for key in PVE_HOST PVE_NODE PVE_STORAGE PVE_BUILD_BRIDGE PVE_BUILD_VLAN; do
    [ -n "${!key:-}" ] || die "$key is not set in $CONFIG"
done
for key in PVE_HOST PVE_NODE PVE_STORAGE PVE_BUILD_BRIDGE; do
    [[ "${!key}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$key must match [A-Za-z0-9][A-Za-z0-9._-]* (got '${!key}')"
done
[[ "$PVE_BUILD_VLAN" =~ ^[1-9][0-9]{0,3}$ && "$PVE_BUILD_VLAN" -le 4094 ]] \
    || die "PVE_BUILD_VLAN must be 1-4094 (got '$PVE_BUILD_VLAN')"

[[ "$IMAGE" =~ ^/[A-Za-z0-9._/-]+$ && "$IMAGE" != *..* ]] || die "--image must be an absolute path (got '$IMAGE')"
[[ "$SNIPPETS" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "--snippets must be a storage name (got '$SNIPPETS')"
[[ "$DATE" =~ ^20[0-9]{6}$ ]] || die "--date must be YYYYMMDD (got '$DATE')"
[[ "$DISK" =~ ^[1-9][0-9]*G$ ]] || die "--disk must look like 20G (got '$DISK')"
if [ -n "$VMID" ]; then
    [[ "$VMID" =~ ^[0-9]{4}$ ]] && [ "$VMID" -ge "$BLOCK_MIN" ] && [ "$VMID" -le "$BLOCK_MAX" ] \
        || die "--vmid must be in $BLOCK_MIN-$BLOCK_MAX (got '$VMID'); never 9000 or above"
fi
command -v python3 >/dev/null || die "python3 is required"

NAME="base-ubuntu-2404-$DATE"
SNIPPET_FILE="$NAME-user.yaml"

# --- remote layer ----------------------------------------------------------
# One argument for the node's root shell: bare when it holds only characters
# no shell reads, single-quoted otherwise.
q() {
    if [[ "$1" =~ ^[A-Za-z0-9._/:=,@+%-]+$ ]]; then
        printf '%s' "$1"
    else
        printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
    fi
}
line_of() {
    local arg line=""
    for arg in "$@"; do line+="${line:+ }$(q "$arg")"; done
    printf '%s' "$line"
}
# Reads, always run. pve_in passes this script's stdin through.
pve() { ssh -n -o BatchMode=yes root@"$PVE_HOST" "$(line_of "$@")"; }
pve_in() { ssh -o BatchMode=yes root@"$PVE_HOST" "$(line_of "$@")"; }
# Changes: printed in a dry run, run with --apply.
run() {
    if [ "$APPLY" -eq 1 ]; then say "+ $(line_of "$@")"; pve "$@"; else say "would run: $(line_of "$@")"; fi
}

py() { python3 -c "$1" "${@:2}"; }

# --- checks (read-only, in both modes) -------------------------------------
node="$(pve hostname)" || die "cannot reach root@$PVE_HOST over SSH"
[ "${node%%.*}" = "$PVE_NODE" ] || die "root@$PVE_HOST is node '${node%%.*}', but PVE_NODE is '$PVE_NODE'"

resources="$(pve pvesh get /cluster/resources --type vm --output-format json)"
# Prints the chosen VMID, or exits 1 naming why.
VMID="$(printf '%s' "$resources" | py '
import json, sys
want, name, lo, hi, off = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:6])
r = json.load(sys.stdin)
used = {int(x["vmid"]) for x in r}
for x in r:
    if x.get("name") == name:
        sys.exit("a guest named %s already exists (VMID %s); pick another --date" % (name, x["vmid"]))
if want:
    v = int(want)
    if v in used or v + off in used:
        sys.exit("VMID %d or its replica %d is in use; this script never touches an existing guest" % (v, v + off))
    print(v)
    sys.exit(0)
for v in range(lo, hi + 1):
    if v not in used and v + off not in used:
        print(v)
        sys.exit(0)
sys.exit("no free VMID pair in %d-%d" % (lo, hi))
' "$VMID" "$NAME" "$BLOCK_MIN" "$BLOCK_MAX" "$REPLICA_OFFSET")" || die "no usable VMID (see above)"
[[ "$VMID" =~ ^[0-9]{4}$ ]] && [ "$VMID" -ge "$BLOCK_MIN" ] && [ "$VMID" -le "$BLOCK_MAX" ] \
    || die "VMID '$VMID' is outside $BLOCK_MIN-$BLOCK_MAX; refusing"
# The config file is the last word on whether a VMID exists.
if [ -n "$(pve sh -c "ls /etc/pve/nodes/*/qemu-server/$VMID.conf /etc/pve/nodes/*/lxc/$VMID.conf 2>/dev/null || true")" ]; then
    die "VMID $VMID is in use"
fi

avail_kib="$(pve sh -c "sed -n 's/^MemAvailable: *\([0-9]*\) kB/\1/p' /proc/meminfo")"
[[ "$avail_kib" =~ ^[0-9]+$ ]] || die "cannot read MemAvailable on $PVE_NODE"
[ "$avail_kib" -ge "$MIN_AVAILABLE_KIB" ] \
    || die "$PVE_NODE has $((avail_kib / 1024 / 1024)) GiB available; 16 GiB is the floor"

pve test -f "$IMAGE" || die "cloud image $IMAGE not found on $PVE_NODE"
image_sha="$(pve sha256sum "$IMAGE" | cut -d' ' -f1)"
snippet_path="$(pve pvesm path "$SNIPPETS:snippets/$SNIPPET_FILE")" \
    || die "storage $SNIPPETS has no snippets content type"
pve test -d "$(dirname "$snippet_path")" || die "snippet directory $(dirname "$snippet_path") does not exist on $PVE_NODE"
if pve test -e "$snippet_path"; then
    die "$snippet_path already exists; remove it or pick another --date"
fi

in_block="$(printf '%s' "$resources" | py '
import json, sys
for x in sorted(json.load(sys.stdin), key=lambda x: x["vmid"]):
    if 8000 <= int(x["vmid"]) <= 8999:
        print("  %s %s %s %s" % (x["vmid"], x.get("node"), x.get("name"), "template" if x.get("template") else x.get("status")))
')"

if [ "$APPLY" -eq 1 ]; then say "== base template: APPLY =="; else say "== base template: dry run (nothing will change) =="; fi
say "VM $VMID '$NAME' on $PVE_NODE: disk $DISK on $PVE_STORAGE, net0 $PVE_BUILD_BRIDGE VLAN $PVE_BUILD_VLAN (DHCP)"
say "  image:     $IMAGE (sha256 $image_sha)"
say "  user-data: $SNIPPETS:snippets/$SNIPPET_FILE (deleted afterwards)"
say "  $PVE_NODE MemAvailable: $((avail_kib / 1024 / 1024)) GiB"
say "  guests in 8000-8999 now:${in_block:+$'\n'$in_block}${in_block:- none}"

# --- first-boot user-data: no key, no password -----------------------------
user_data() {
    cat <<'EOF'
#cloud-config
# base-ubuntu-2404 first boot (proxmox_base_ubuntu.sh). No keys, no passwords.
users:
  - name: builder
    gecos: image builder
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
disable_root: true
ssh_pwauth: false
package_update: true
package_upgrade: true
packages:
  - qemu-guest-agent
  - openssh-server
runcmd:
  - [systemctl, enable, ssh.socket]
  - [systemctl, start, qemu-guest-agent]
EOF
}

# --- finalize, run as root in the guest through the agent ------------------
finalize_script() {
    cat <<'EOF'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
step() { printf '== %s\n' "$*"; }

step "contract: builder, sudo, packages"
id builder
if id ubuntu >/dev/null 2>&1; then echo "user ubuntu exists; the base must hold builder only" >&2; exit 1; fi
runuser -u builder -- sudo -n true
echo "builder: passwordless sudo ok"
dpkg-query -W -f='${Package} ${Version}\n' qemu-guest-agent openssh-server
systemctl is-active qemu-guest-agent
systemctl is-enabled ssh.socket

step "network: DHCP on any en*/eth* interface, not keyed to this MAC"
cat > /etc/netplan/01-image-build-dhcp.yaml <<'NETPLAN'
# Written by proxmox_base_ubuntu.sh. Matched by name, not MAC: every clone
# has a new MAC and no cloud-init drive.
network:
  version: 2
  ethernets:
    en-any:
      match:
        name: "en*"
      dhcp4: true
      dhcp-identifier: mac
    eth-any:
      match:
        name: "eth*"
      dhcp4: true
      dhcp-identifier: mac
NETPLAN
chmod 600 /etc/netplan/01-image-build-dhcp.yaml
rm -f /etc/netplan/50-cloud-init.yaml
printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
netplan generate
ls -l /etc/netplan/

step "ssh: no host keys; a clone generates its own when ssh.service starts"
mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n' \
  > /etc/systemd/system/ssh.service.d/10-regen-hostkeys.conf
systemctl daemon-reload
rm -f /etc/ssh/ssh_host_*

step "no keys, no passwords"
find / -xdev \( -name authorized_keys -o -name authorized_keys2 \) -print -delete
passwd -l root
passwd -l builder
passwd -S builder
passwd -S root

step "identity and logs"
apt-get clean
cloud-init clean --logs
truncate -s 0 /etc/machine-id
if [ -e /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then rm -f /var/lib/dbus/machine-id; fi
for f in /root/.bash_history /home/*/.bash_history; do
  if [ -f "$f" ]; then truncate -s 0 "$f"; fi
done
fstrim -av || true
sync
echo finalized
EOF
}

# The agent's JSON for one finished command: print its output, fail unless
# it exited 0.
exec_ok() {
    py '
import json, sys
r = json.load(sys.stdin)
for k in ("out-data", "err-data"):
    if r.get(k):
        sys.stderr.write(r[k] if r[k].endswith("\n") else r[k] + "\n")
if r.get("exited") not in (1, True):
    sys.exit("guest command did not finish within the timeout")
sys.exit(0 if r.get("exitcode") == 0 else "guest command exited %s" % r.get("exitcode"))
'
}

# --- cleanup ---------------------------------------------------------------
# This run's construction marker. qm create writes it into the description,
# so the VM is recognised by what the node holds, not by a flag set after a
# call returns: a create that succeeds but whose SSH session drops, or a run
# interrupted while qm create runs, still leaves a VM this run can prove is
# its own. The marker stays until the VM is a template.
RUN_TAG="proxmox-base-build=$(date -u +%Y%m%d%H%M%S)-$(py 'import secrets; print(secrets.token_hex(4))')"
[[ "$RUN_TAG" =~ ^proxmox-base-build=[0-9]{14}-[0-9a-f]{8}$ ]] || die "unexpected run marker '$RUN_TAG'"

# The node-side destroy of VM $VMID: one sh script, so the node itself checks
# the marker immediately before `qm stop` and again before `qm destroy`, with
# no round trip in which the VMID could change hands (as the Proxmox
# backend's destroy does). Exit 0: destroyed. 5: no such VMID anywhere.
# 3: refused (a template, another node's guest, or no marker). 4: the config
# could not be read. 6: qm stop or qm destroy failed (255: SSH failed).
cleanup_script() {
    cat <<EOF
set -e
v=$VMID
check() {
  if ! c=\$(qm config \$v 2>/dev/null); then
    e=\$(qm config \$v 2>&1 >/dev/null) || true
    case "\$e" in
      *"/\$v.conf' does not exist"*) ;;
      *) echo "cannot read the config of VM \$v (\${e:-qm config failed})" >&2; exit 4 ;;
    esac
    if ! ls -d /etc/pve/nodes >/dev/null 2>&1; then
      echo "/etc/pve/nodes is not readable (cluster filesystem down?)" >&2; exit 4
    fi
    if [ -n "\$(ls /etc/pve/nodes/*/qemu-server/\$v.conf /etc/pve/nodes/*/lxc/\$v.conf 2>/dev/null)" ]; then
      echo "VMID \$v is a guest on another node" >&2; exit 3
    fi
    exit 5
  fi
  if printf '%s\n' "\$c" | grep -qx 'template: 1'; then
    echo "VM \$v is a template" >&2; exit 3
  fi
  if ! printf '%s\n' "\$c" | grep -qxF 'name: $NAME' \\
     || ! printf '%s\n' "\$c" | sed -n 's/^description: //p' | tr ' ' '\n' | grep -qxF '$RUN_TAG'; then
    echo "VM \$v is not named $NAME or does not carry this run's marker $RUN_TAG" >&2; exit 3
  fi
}
check
qm stop \$v || exit 6
check
qm destroy \$v --purge 1 || exit 6
EOF
}

# Destroy VM $VMID if, and only if, the node shows it carries RUN_TAG.
destroy_vm() {
    local rc=0
    if ! [[ "$VMID" =~ ^[0-9]{4}$ && "$VMID" -ge "$BLOCK_MIN" && "$VMID" -le "$BLOCK_MAX" ]]; then
        say "ERROR: VMID '$VMID' is outside $BLOCK_MIN-$BLOCK_MAX; not touched"
        return 1
    fi
    say "+ destroy VM $VMID on $PVE_NODE if it carries $RUN_TAG"
    pve sh -c "$(cleanup_script)" >&2 || rc=$?
    case "$rc" in
        0)
            VM_ARMED=0
            say "VM $VMID ('$NAME', $RUN_TAG) was destroyed: the run stopped before the template was finished." ;;
        5)
            VM_ARMED=0
            say "No VM $VMID exists on the cluster now; nothing was destroyed. If this run stopped while"
            say "  qm create was still running on $PVE_NODE, check again: a VM $VMID named $NAME whose"
            say "  description carries $RUN_TAG is this run's (qm config $VMID; qm destroy $VMID --purge 1)." ;;
        *)
            say "ERROR: VM $VMID was NOT destroyed (exit $rc; reason above). Inspect it on $PVE_NODE: qm config $VMID."
            say "       Remove it by hand (qm destroy $VMID --purge 1) only if it is not a template and its"
            say "       description carries this run's marker $RUN_TAG."
            return 1 ;;
    esac
}

# Delete the user-data snippet; the state is cleared only once the node
# confirms the file is gone.
remove_snippet() {
    say "+ rm -f $snippet_path"
    if [ "$(pve sh -c "rm -f -- $(q "$snippet_path") && if [ -e $(q "$snippet_path") ]; then echo present; else echo absent; fi")" = absent ]; then
        SNIPPET_ARMED=0
        return 0
    fi
    say "ERROR: the first-boot user-data snippet $snippet_path on $PVE_NODE was NOT deleted."
    say "       Delete it by hand: rm -f $(q "$snippet_path") on $PVE_NODE."
    return 1
}

# Set before the change each guards, so an interruption at any point is
# covered; each is cleared only once its cleanup is confirmed.
VM_ARMED=0
SNIPPET_ARMED=0
on_exit() {
    local status=$?
    set +e
    trap '' INT TERM HUP
    if [ "$VM_ARMED" -eq 1 ]; then
        [ "$status" -ne 0 ] || status=1
        say ""
        say "ERROR: the run stopped before template $VMID was finished; cleaning up."
        destroy_vm
    fi
    if [ "$SNIPPET_ARMED" -eq 1 ]; then
        remove_snippet || { [ "$status" -ne 0 ] || status=1; }
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- apply -----------------------------------------------------------------
say "  marker:    $RUN_TAG (in the description until the VM is a template)"
if [ "$APPLY" -eq 1 ]; then
    # The path was checked absent above: what is there from now on is ours.
    SNIPPET_ARMED=1
    say "+ write $snippet_path"
    user_data | pve_in sh -c "cat > $(q "$snippet_path")"
else
    say "would write $snippet_path:"
    user_data | sed 's/^/    /' >&2
fi

VM_ARMED="$APPLY"
run qm create "$VMID" --name "$NAME" --ostype l26 --cores "$BUILD_CORES" --memory "$BUILD_MEMORY" \
    --agent enabled=1 --scsihw virtio-scsi-single --serial0 socket --vga serial0 \
    --net0 "virtio,bridge=$PVE_BUILD_BRIDGE,tag=$PVE_BUILD_VLAN" \
    --description "$RUN_TAG $NAME under construction by proxmox_base_ubuntu.sh; not a template yet"

run qm set "$VMID" --scsi0 "$PVE_STORAGE:0,import-from=$IMAGE,discard=on" --boot order=scsi0
# Before first boot: cloud-init's growpart then gives the root filesystem
# the whole disk.
run qm resize "$VMID" scsi0 "$DISK"
run qm set "$VMID" --ide2 "$PVE_STORAGE:cloudinit" --cicustom "user=$SNIPPETS:snippets/$SNIPPET_FILE" \
    --ipconfig0 ip=dhcp
run qm start "$VMID"

if [ "$APPLY" -eq 1 ]; then
    say "waiting for the guest agent (the first boot installs it; up to 20 minutes)"
    tries=0
    until pve qm guest cmd "$VMID" ping >/dev/null 2>&1; do
        tries=$((tries + 1)); [ "$tries" -lt 120 ] || die "no guest agent on VM $VMID after 20 minutes"
        sleep 10
    done
    say "waiting for cloud-init to finish"
    tries=0
    while :; do
        st="$(pve qm guest exec "$VMID" --timeout 60 -- cloud-init status 2>/dev/null \
              | py 'import json,sys; print(json.load(sys.stdin).get("out-data",""))' 2>/dev/null)" || st=""
        case "$st" in
            *"status: done"*) break ;;
            *"status: error"*|*"status: degraded"*)
                pve qm guest exec "$VMID" --timeout 60 -- cloud-init status --long | exec_ok || true
                die "cloud-init on VM $VMID did not finish cleanly" ;;
        esac
        tries=$((tries + 1)); [ "$tries" -lt 120 ] || die "cloud-init on VM $VMID still running after 20 minutes"
        sleep 10
    done
    # The record of the first boot, warnings included.
    pve qm guest exec "$VMID" --timeout 60 -- cloud-init status --long | exec_ok || true
    say "+ finalize in the guest (qm guest exec $VMID --pass-stdin 1 -- /bin/bash -s)"
    finalize_script | pve_in qm guest exec "$VMID" --timeout 900 --pass-stdin 1 -- /bin/bash -s | exec_ok \
        || die "finalize failed in VM $VMID"
else
    say "would wait for the guest agent, then for 'cloud-init status: done'"
    say "would run as root in the guest (qm guest exec $VMID --pass-stdin 1 -- /bin/bash -s):"
    finalize_script | sed 's/^/    /' >&2
fi

run qm shutdown "$VMID" --timeout 300
if [ "$APPLY" -eq 1 ]; then
    [ "$(pve qm status "$VMID")" = "status: stopped" ] || die "VM $VMID did not stop"
fi
# The cloud-init drive goes: a clone that kept it would replay its config.
run qm set "$VMID" --delete ide2,cicustom,ipconfig0
if [ "$APPLY" -eq 1 ]; then
    if pve qm config "$VMID" | grep -E 'cloudinit|cicustom|ipconfig|^unused'; then
        die "VM $VMID still has a cloud-init drive or an unused volume (above)"
    fi
fi
run qm template "$VMID"
# A template is finished: from here a failure is reported, never destroyed.
VM_ARMED=0
run qm set "$VMID" --description "$NAME: Ubuntu 24.04 cloud image $(basename "$IMAGE") sha256 $image_sha, built $(date -u +%Y-%m-%d) by scripts/image_build/proxmox_base_ubuntu.sh. User builder: passwordless sudo, password locked, no key. No cloud-init drive, no host keys, empty machine-id." \
    || die "template $VMID is finished, but setting its description failed; it still reads 'under construction' with $RUN_TAG: set it by hand (qm set $VMID --description ...)"
if [ "$APPLY" -eq 1 ]; then
    remove_snippet || exit 1
else
    say "would run: rm -f $snippet_path"
fi

say ""
if [ "$APPLY" -ne 1 ]; then
    say "Dry run only. Re-run with --apply to create template $VMID."
    exit 0
fi
say "Template $VMID '$NAME' is ready on $PVE_NODE. Next:"
say "  1. Prove it: full-clone it to a test VMID in the block, boot it, check the contract, destroy the clone."
say "  2. Replicate: scripts/sync_template.sh $PVE_HOST $VMID <replica host> <replica storage> $((VMID + REPLICA_OFFSET))"
printf '%s\n' "$VMID"
