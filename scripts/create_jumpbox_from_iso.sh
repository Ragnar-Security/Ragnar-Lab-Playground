#!/bin/bash
#
# Jumpbox Setup Script - Using Existing ISO
# Creates a jumpbox VM from existing Ubuntu ISO
#
# Usage: ./scripts/create_jumpbox_from_iso.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/site_env.sh"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" JUMPBOX_NODE JUMPBOX_STORAGE HEADSCALE_URL
for name in JUMPBOX_NODE JUMPBOX_STORAGE HEADSCALE_URL; do
    require_site_var "$name"
done

# Configuration
JUMPBOX_VMID="${JUMPBOX_VMID:-9999}"
JUMPBOX_NAME="${JUMPBOX_NAME:-jumpbox}"
JUMPBOX_MEMORY="${JUMPBOX_MEMORY:-4096}"
JUMPBOX_CORES="${JUMPBOX_CORES:-2}"
JUMPBOX_DISK_SIZE="${JUMPBOX_DISK_SIZE:-32}"
JUMPBOX_BRIDGE="${JUMPBOX_BRIDGE:-vmbr0}"

# ISO Configuration - CHANGE THIS to your ISO location
JUMPBOX_ISO="${JUMPBOX_ISO:-local:iso/ubuntu-24.04-live-server-amd64.iso}"

echo "========================================"
echo "Lab Jumpbox Setup (From ISO)"
echo "========================================"
echo ""
echo "This script will create a jumpbox VM using your existing Ubuntu ISO."
echo "You'll need to complete the Ubuntu installation manually via console."
echo ""
echo "Configuration:"
echo "  VM ID:     $JUMPBOX_VMID"
echo "  VM Name:   $JUMPBOX_NAME"
echo "  Memory:    ${JUMPBOX_MEMORY}MB"
echo "  Cores:     $JUMPBOX_CORES"
echo "  Disk:      ${JUMPBOX_DISK_SIZE}G"
echo "  Node:      $JUMPBOX_NODE"
echo "  Storage:   $JUMPBOX_STORAGE"
echo "  ISO:       $JUMPBOX_ISO"
echo ""
read -p "Continue with these settings? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Step 1: Checking for existing jumpbox..."
if ssh root@"$JUMPBOX_NODE" "qm status $JUMPBOX_VMID >/dev/null 2>&1"; then
    echo "❌ VM $JUMPBOX_VMID already exists on node $JUMPBOX_NODE"
    read -p "Do you want to destroy it and recreate? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Stopping and destroying existing jumpbox..."
        ssh root@"$JUMPBOX_NODE" "qm stop $JUMPBOX_VMID || true; sleep 2; qm destroy $JUMPBOX_VMID"
    else
        echo "Aborted."
        exit 1
    fi
fi

echo ""
echo "Step 2: Verifying ISO exists..."
if ! ssh root@"$JUMPBOX_NODE" "pvesm list local | grep -q ubuntu.*iso || pvesm list local-iso | grep -q ubuntu.*iso 2>/dev/null"; then
    echo "⚠️  Could not verify ISO existence. Available ISOs:"
    ssh root@"$JUMPBOX_NODE" "pvesm list local | grep iso || true"
    echo ""
    echo "If your ISO is not shown, upload it to Proxmox:"
    echo "  GUI: Datacenter > Node > local > ISO Images > Upload"
    echo "  CLI: scp ubuntu-24.04-live-server-amd64.iso root@$JUMPBOX_NODE:/var/lib/vz/template/iso/"
    echo ""
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
else
    echo "✓ ISO found"
fi

echo ""
echo "Step 3: Creating VM..."

ssh root@"$JUMPBOX_NODE" <<EOF
set -e

# Create VM
qm create $JUMPBOX_VMID \\
    --name $JUMPBOX_NAME \\
    --memory $JUMPBOX_MEMORY \\
    --cores $JUMPBOX_CORES \\
    --net0 virtio,bridge=$JUMPBOX_BRIDGE \\
    --scsihw virtio-scsi-pci \\
    --scsi0 $JUMPBOX_STORAGE:${JUMPBOX_DISK_SIZE} \\
    --ide2 $JUMPBOX_ISO,media=cdrom \\
    --boot order=scsi0 \\
    --ostype l26 \\
    --agent enabled=1

echo "✓ VM created"
EOF

echo ""
echo "========================================"
echo "✅ Jumpbox VM Created!"
echo "========================================"
echo ""
echo "⚠️  MANUAL STEPS REQUIRED:"
echo ""
echo "1. Start the VM and open console:"
echo "   Proxmox UI: Select VM $JUMPBOX_VMID > Start > Console"
echo "   Or run: ssh root@$JUMPBOX_NODE 'qm start $JUMPBOX_VMID'"
echo ""
echo "2. Complete Ubuntu installation:"
echo "   - Choose English"
echo "   - Install Ubuntu Server"
echo "   - Configure network (DHCP is fine)"
echo "   - Set hostname: $JUMPBOX_NAME"
echo "   - Create user: ubuntu (or your choice)"
echo "   - Install OpenSSH server: ✓ YES"
echo "   - No additional packages needed"
echo "   - Reboot when done"
echo ""
echo "3. After Ubuntu installation completes, get the IP:"
echo "   ssh root@$JUMPBOX_NODE 'qm guest cmd $JUMPBOX_VMID network-get-interfaces'"
echo "   Or check in Proxmox UI: VM > Summary > IPs"
echo ""
echo "4. Then run the configuration script:"
echo "   ./scripts/configure_jumpbox.sh <jumpbox_ip>"
echo "   Headscale server: $HEADSCALE_URL"
echo ""
echo "Alternatively, use the automated cloud image script:"
echo "   ./scripts/create_jumpbox.sh"
echo ""
