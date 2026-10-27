#!/bin/bash
#
# Jumpbox Setup Script - Run Directly on Proxmox Host
# Creates a jumpbox VM using cloud image
#
# Usage: 
#   1. Copy this script to your Proxmox host
#   2. Run: bash create_jumpbox_local.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
STORAGE_OVERRIDE_SET="${JUMPBOX_STORAGE+x}"
STORAGE_OVERRIDE="${JUMPBOX_STORAGE-}"
URL_OVERRIDE_SET="${HEADSCALE_URL+x}"
URL_OVERRIDE="${HEADSCALE_URL-}"
if [[ -f "$SITE_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090 -- the operator chooses this untracked path.
    source "$SITE_ENV_FILE"
    set +a
fi
[[ -n "$STORAGE_OVERRIDE_SET" ]] && JUMPBOX_STORAGE="$STORAGE_OVERRIDE"
[[ -n "$URL_OVERRIDE_SET" ]] && HEADSCALE_URL="$URL_OVERRIDE"
for name in JUMPBOX_STORAGE HEADSCALE_URL; do
    if [[ -z "${!name:-}" ]]; then
        printf 'ERROR: required configuration variable %s is unset; set it in the environment or site.env\n' "$name" >&2
        exit 1
    fi
done

# Configuration
JUMPBOX_VMID="${JUMPBOX_VMID:-9999}"
JUMPBOX_NAME="${JUMPBOX_NAME:-jumpbox}"
JUMPBOX_MEMORY="${JUMPBOX_MEMORY:-4096}"
JUMPBOX_CORES="${JUMPBOX_CORES:-2}"
JUMPBOX_DISK_SIZE="${JUMPBOX_DISK_SIZE:-32G}"
JUMPBOX_BRIDGE="${JUMPBOX_BRIDGE:-vmbr0}"
JUMPBOX_VLAN="${JUMPBOX_VLAN:-20}"
# Optional: path to an SSH public-key file, installed for the default cloud
# image user (ubuntu) through cloud-init. Without it the VM has no login.
JUMPBOX_SSH_PUBKEY="${JUMPBOX_SSH_PUBKEY:-}"
if [[ -n "$JUMPBOX_SSH_PUBKEY" && ! -f "$JUMPBOX_SSH_PUBKEY" ]]; then
    echo "ERROR: JUMPBOX_SSH_PUBKEY file not found: $JUMPBOX_SSH_PUBKEY" >&2
    exit 1
fi

echo "========================================"
echo "Lab Jumpbox Setup (Local)"
echo "========================================"
echo ""
echo "Configuration:"
echo "  VM ID:     $JUMPBOX_VMID"
echo "  VM Name:   $JUMPBOX_NAME"
echo "  Memory:    ${JUMPBOX_MEMORY}MB"
echo "  Cores:     $JUMPBOX_CORES"
echo "  Disk:      $JUMPBOX_DISK_SIZE"
echo "  Bridge:    $JUMPBOX_BRIDGE"
echo "  VLAN:      $JUMPBOX_VLAN"
echo "  Storage:   $JUMPBOX_STORAGE"
echo ""
read -p "Continue with these settings? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Step 1: Checking for existing jumpbox..."
if qm status $JUMPBOX_VMID >/dev/null 2>&1; then
    echo "❌ VM $JUMPBOX_VMID already exists"
    read -p "Do you want to destroy it and recreate? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Stopping and destroying existing jumpbox..."
        qm stop $JUMPBOX_VMID || true
        sleep 2
        qm destroy $JUMPBOX_VMID
    else
        echo "Aborted."
        exit 1
    fi
fi

echo ""
echo "Step 2: Downloading Ubuntu cloud image..."
CLOUD_IMAGE="/tmp/jammy-server-cloudimg-amd64.img"
if [ ! -f "$CLOUD_IMAGE" ]; then
    wget -O $CLOUD_IMAGE https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
    echo "✓ Cloud image downloaded"
else
    echo "✓ Cloud image already exists"
fi

echo ""
echo "Step 3: Creating VM..."

# Create base VM
qm create $JUMPBOX_VMID \
    --name $JUMPBOX_NAME \
    --memory $JUMPBOX_MEMORY \
    --cores $JUMPBOX_CORES \
    --net0 virtio,bridge=$JUMPBOX_BRIDGE,tag=$JUMPBOX_VLAN \
    --agent enabled=1 \
    --ostype l26

echo "✓ VM created"

echo ""
echo "Step 4: Importing disk image..."

# Import disk
qm importdisk $JUMPBOX_VMID $CLOUD_IMAGE $JUMPBOX_STORAGE

# Attach disk
qm set $JUMPBOX_VMID --scsihw virtio-scsi-pci --scsi0 $JUMPBOX_STORAGE:vm-$JUMPBOX_VMID-disk-0

# Set boot disk
qm set $JUMPBOX_VMID --boot c --bootdisk scsi0

# Resize disk
qm resize $JUMPBOX_VMID scsi0 $JUMPBOX_DISK_SIZE

echo "✓ Disk configured"

echo ""
echo "Step 5: Adding cloud-init drive..."

# Add cloud-init drive
qm set $JUMPBOX_VMID --ide2 $JUMPBOX_STORAGE:cloudinit

# Set cloud-init settings
qm set $JUMPBOX_VMID --serial0 socket --vga serial0
qm set $JUMPBOX_VMID --ipconfig0 ip=dhcp
if [[ -n "$JUMPBOX_SSH_PUBKEY" ]]; then
    qm set $JUMPBOX_VMID --ciuser ubuntu --sshkeys "$JUMPBOX_SSH_PUBKEY"
fi

echo "✓ Cloud-init configured"

echo ""
echo "Step 6: Starting jumpbox VM..."
qm start $JUMPBOX_VMID

echo "✓ Jumpbox VM started"
echo ""
echo "Waiting 30 seconds for VM to boot..."
sleep 30

echo ""
echo "Step 7: Getting jumpbox IP address..."
JUMPBOX_IP=$(qm guest cmd $JUMPBOX_VMID network-get-interfaces 2>/dev/null | grep -oP '(?<="ip-address":")[^"]*' | grep -v "127.0.0.1" | head -1 || echo "")

if [ -z "$JUMPBOX_IP" ]; then
    echo "⚠️  Could not auto-detect IP. Checking status..."
    qm status $JUMPBOX_VMID
    echo ""
    echo "Get IP manually with:"
    echo "  qm guest cmd $JUMPBOX_VMID network-get-interfaces"
    echo "  Or check Proxmox web UI"
    JUMPBOX_IP="<check_vm_console>"
else
    echo "✓ Jumpbox IP: $JUMPBOX_IP"
fi

echo ""
echo "========================================"
echo "✅ Jumpbox VM Created Successfully!"
echo "========================================"
echo ""
echo "Next steps:"
echo ""
if [[ -n "$JUMPBOX_SSH_PUBKEY" ]]; then
    echo "1. SSH into the jumpbox as ubuntu with the key configured through cloud-init"
    echo "   ($JUMPBOX_SSH_PUBKEY); there is no password login:"
    echo "   ssh ubuntu@$JUMPBOX_IP"
else
    echo "1. No SSH key was configured, so the VM has no way to log in. Re-run with"
    echo "   JUMPBOX_SSH_PUBKEY=<path-to-public-key-file> to install one through"
    echo "   cloud-init, or set one on the VM: qm set $JUMPBOX_VMID --sshkeys <file>"
fi
echo ""
echo "2. Install Tailscale:"
echo "   curl -fsSL https://tailscale.com/install.sh | sh"
echo ""
echo "3. Register with Headscale:"
echo "   sudo tailscale up --login-server=$HEADSCALE_URL --hostname=jumpbox"
echo ""
echo "4. Get VPN IP and share with students:"
echo "   tailscale ip -4"
echo ""
echo "5. Create student accounts:"
echo "   sudo useradd -m -s /bin/bash <username>"
echo "   sudo passwd <username>"
echo ""
