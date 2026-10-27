#!/usr/bin/env bash
#
# Create the lab jumpbox VM. Runs ON the Proxmox node named by JUMPBOX_NODE,
# as root.
#
# Usage: scripts/jumpbox_provision.sh [--apply] [--no-start]
#
# Dry run by default: checks the node and prints every qm command. --apply
# runs them. It never touches an existing VM; an existing JUMPBOX_VMID is an
# error.
#
# The VM boots the Ubuntu cloud image (JUMPBOX_CLOUD_IMAGE, downloaded from
# JUMPBOX_CLOUD_IMAGE_URL when missing). Its disk is resized to
# JUMPBOX_DISK_SIZE before first boot, and cloud-init grows the root
# filesystem into all of it. The Ubuntu ISO installer instead gives the root
# LV half the disk.
#   net0  management: JUMPBOX_BRIDGE, VLAN JUMPBOX_MGMT_VLAN, DHCP
#   net1  lab trunk:  JUMPBOX_TRUNK_BRIDGE, untagged so it carries every lab
#         VLAN, Proxmox firewall on
# cloud-init creates JUMPBOX_ADMIN_USER with the key in JUMPBOX_SSH_PUBKEY.
# The account has no password.
#
# Then run scripts/jumpbox_configure.sh on the new VM (docs/JUMPBOX.md).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=site_env.sh
source "$SCRIPT_DIR/site_env.sh"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" JUMPBOX_NODE JUMPBOX_STORAGE JUMPBOX_VMID JUMPBOX_NAME \
    JUMPBOX_BRIDGE JUMPBOX_MGMT_VLAN JUMPBOX_TRUNK_BRIDGE JUMPBOX_CORES JUMPBOX_MEMORY \
    JUMPBOX_DISK_SIZE JUMPBOX_CLOUD_IMAGE JUMPBOX_CLOUD_IMAGE_URL JUMPBOX_SSH_PUBKEY \
    JUMPBOX_ADMIN_USER
for name in JUMPBOX_NODE JUMPBOX_STORAGE JUMPBOX_VMID JUMPBOX_SSH_PUBKEY; do
    require_site_var "$name"
done

VMID="$JUMPBOX_VMID"
STORAGE="$JUMPBOX_STORAGE"
PUBKEY="$JUMPBOX_SSH_PUBKEY"
NAME="${JUMPBOX_NAME:-jumpbox}"
BRIDGE="${JUMPBOX_BRIDGE:-vmbr0}"
MGMT_VLAN="${JUMPBOX_MGMT_VLAN-1}"  # empty: untagged
TRUNK_BRIDGE="${JUMPBOX_TRUNK_BRIDGE:-$BRIDGE}"
CORES="${JUMPBOX_CORES:-2}"
MEMORY="${JUMPBOX_MEMORY:-4096}"
DISK="${JUMPBOX_DISK_SIZE:-32G}"
IMAGE="${JUMPBOX_CLOUD_IMAGE:-/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img}"
IMAGE_URL="${JUMPBOX_CLOUD_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
ADMIN="${JUMPBOX_ADMIN_USER:-ubuntu}"

APPLY=0 START=1
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --no-start) START=0 ;;
        -h|--help) sed -n '3,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'ERROR: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() {
    if [ "$APPLY" -eq 1 ]; then say "+ $*"; "$@"; else say "would run: $*"; fi
}

[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]] || die "JUMPBOX_VMID must be a VMID of 100 or more (got '$VMID')"
[[ -z "$MGMT_VLAN" || "$MGMT_VLAN" =~ ^[0-9]+$ && "$MGMT_VLAN" -ge 1 && "$MGMT_VLAN" -le 4094 ]] \
    || die "JUMPBOX_MGMT_VLAN must be 1-4094, or empty for untagged (got '$MGMT_VLAN')"
[[ "$CORES" =~ ^[1-9][0-9]*$ && "$MEMORY" =~ ^[1-9][0-9]*$ ]] || die "JUMPBOX_CORES and JUMPBOX_MEMORY must be positive integers"
[[ "$DISK" =~ ^[1-9][0-9]*G$ ]] || die "JUMPBOX_DISK_SIZE must look like 32G (got '$DISK')"
[[ "$ADMIN" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "JUMPBOX_ADMIN_USER is not a valid user name"
grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) AAAA' "$PUBKEY" 2>/dev/null \
    || die "JUMPBOX_SSH_PUBKEY ($PUBKEY) holds no SSH public key; without one the VM has no login"

node="$(hostname)"
[ "${node%%.*}" = "$JUMPBOX_NODE" ] \
    || die "this is node '${node%%.*}', but JUMPBOX_NODE is '$JUMPBOX_NODE'; run on that node"
command -v qm >/dev/null || die "qm not found; run this on a Proxmox node"
if qm status "$VMID" >/dev/null 2>&1; then
    die "VM $VMID already exists. This script never modifies or destroys a VM; pick another JUMPBOX_VMID."
fi

if [ "$APPLY" -eq 1 ]; then
    say "== jumpbox provision: APPLY =="
else
    say "== jumpbox provision: dry run (nothing will change) =="
fi
say "VM $VMID '$NAME' on $JUMPBOX_NODE: $CORES cores, ${MEMORY} MB, $DISK disk on $STORAGE"
say "  net0 management: $BRIDGE${MGMT_VLAN:+ VLAN $MGMT_VLAN}, DHCP"
say "  net1 lab trunk:  $TRUNK_BRIDGE untagged, firewall on"
say "  login: $ADMIN with the key in $PUBKEY"

CREATED=0
on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$CREATED" -eq 1 ]; then
        printf 'ERROR: VM %s was created but setup did not finish. Inspect it (qm config %s), remove it with qm destroy %s if it is unusable, and re-run.\n' \
            "$VMID" "$VMID" "$VMID" >&2
    fi
}
trap on_exit EXIT

if [ ! -f "$IMAGE" ]; then
    run curl -fL --create-dirs -o "$IMAGE.part" "$IMAGE_URL"
    run mv "$IMAGE.part" "$IMAGE"
fi

run qm create "$VMID" --name "$NAME" --ostype l26 --cores "$CORES" --memory "$MEMORY" \
    --agent enabled=1 --onboot 1 --scsihw virtio-scsi-single --serial0 socket --vga serial0 \
    --net0 "virtio,bridge=$BRIDGE${MGMT_VLAN:+,tag=$MGMT_VLAN}" \
    --net1 "virtio,bridge=$TRUNK_BRIDGE,firewall=1"
CREATED="$APPLY"
run qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$IMAGE,discard=on" --boot order=scsi0
# Resize before first boot: cloud-init's growpart then gives the root
# filesystem the whole disk.
run qm resize "$VMID" scsi0 "$DISK"
run qm set "$VMID" --ide2 "$STORAGE:cloudinit" --ciuser "$ADMIN" --sshkeys "$PUBKEY" \
    --ipconfig0 ip=dhcp
[ "$START" -eq 1 ] && run qm start "$VMID"
CREATED=0

say ""
if [ "$APPLY" -ne 1 ]; then
    say "Dry run only. Re-run with --apply to create the VM."
    exit 0
fi
say "Created VM $VMID. Next:"
say "  1. Find its management address: qm guest cmd $VMID network-get-interfaces"
say "     (the guest agent arrives with jumpbox_configure.sh; until then use the DHCP server's leases)"
say "  2. Copy scripts/site_env.sh, scripts/site.env and scripts/jumpbox_* to the VM."
say "  3. On the VM: sudo ./jumpbox_configure.sh   (dry run), then --apply"
