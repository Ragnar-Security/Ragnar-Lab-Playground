#!/bin/bash
#
# Jumpbox Setup Script for Lab Automation
# Creates a shared VPN jumpbox that all students can use to access their VMs
#
# Usage: ./scripts/create_jumpbox.sh
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
JUMPBOX_DISK_SIZE="${JUMPBOX_DISK_SIZE:-32G}"
JUMPBOX_BRIDGE="${JUMPBOX_BRIDGE:-vmbr0}"
JUMPBOX_VLAN="${JUMPBOX_VLAN:-20}"  # VLAN tag

# Headscale configuration
HEADSCALE_USER="${HEADSCALE_USER:-jumpbox}"

echo "========================================"
echo "Lab Jumpbox Setup"
echo "========================================"
echo ""
echo "This script will create a jumpbox VM that students can use to access"
echo "their internal VMs via Tailscale/Headscale VPN."
echo ""
echo "Configuration:"
echo "  VM ID:     $JUMPBOX_VMID"
echo "  VM Name:   $JUMPBOX_NAME"
echo "  Memory:    ${JUMPBOX_MEMORY}MB"
echo "  Cores:     $JUMPBOX_CORES"
echo "  Disk:      $JUMPBOX_DISK_SIZE"
echo "  Node:      $JUMPBOX_NODE"
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
echo "Step 2: Downloading Ubuntu cloud image..."
CLOUD_IMAGE="jammy-server-cloudimg-amd64.img"
if [ ! -f "/tmp/$CLOUD_IMAGE" ]; then
    wget -O /tmp/$CLOUD_IMAGE https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
    echo "✓ Cloud image downloaded"
else
    echo "✓ Cloud image already exists"
fi

echo ""
echo "Step 3: Creating VM on Proxmox node $JUMPBOX_NODE..."

# Create VM
ssh root@"$JUMPBOX_NODE" <<EOF
set -e

# Create base VM
qm create $JUMPBOX_VMID \
    --name $JUMPBOX_NAME \
    --memory $JUMPBOX_MEMORY \,tag=$JUMPBOX_VLAN
    --cores $JUMPBOX_CORES \
    --net0 virtio,bridge=$JUMPBOX_BRIDGE \
    --agent enabled=1 \
    --ostype l26

echo "✓ VM created"
EOF

echo ""
echo "Step 4: Uploading and importing disk image..."
scp /tmp/$CLOUD_IMAGE root@"$JUMPBOX_NODE":/tmp/

ssh root@"$JUMPBOX_NODE" <<EOF
set -e

# Import disk
qm importdisk $JUMPBOX_VMID /tmp/$CLOUD_IMAGE $JUMPBOX_STORAGE

# Attach disk
qm set $JUMPBOX_VMID --scsihw virtio-scsi-pci --scsi0 $JUMPBOX_STORAGE:vm-$JUMPBOX_VMID-disk-0

# Set boot disk
qm set $JUMPBOX_VMID --boot c --bootdisk scsi0

# Resize disk
qm resize $JUMPBOX_VMID scsi0 $JUMPBOX_DISK_SIZE

# Add cloud-init drive
qm set $JUMPBOX_VMID --ide2 $JUMPBOX_STORAGE:cloudinit

# Set cloud-init settings
qm set $JUMPBOX_VMID --serial0 socket --vga serial0
qm set $JUMPBOX_VMID --ipconfig0 ip=dhcp

# Clean up
rm -f /tmp/$CLOUD_IMAGE

echo "✓ Disk configured"
EOF

echo ""
echo "Step 5: Starting jumpbox VM..."
ssh root@"$JUMPBOX_NODE" "qm start $JUMPBOX_VMID"

echo "✓ Jumpbox VM started"
echo ""
echo "Waiting 30 seconds for VM to boot..."
sleep 30

echo ""
echo "Step 6: Getting jumpbox IP address..."
JUMPBOX_IP=$(ssh root@"$JUMPBOX_NODE" "qm guest cmd $JUMPBOX_VMID network-get-interfaces" | grep -oP '(?<="ip-address":")[^"]*' | grep -v "127.0.0.1" | head -1)

if [ -z "$JUMPBOX_IP" ]; then
    echo "⚠️  Could not auto-detect IP. Please check Proxmox console."
    echo "   Run: ssh root@$JUMPBOX_NODE 'qm guest cmd $JUMPBOX_VMID network-get-interfaces'"
    JUMPBOX_IP="<check_proxmox_console>"
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
echo "1. SSH into the jumpbox (default user: ubuntu, use Proxmox console first time):"
echo "   ssh ubuntu@$JUMPBOX_IP"
echo ""
echo "2. Install Tailscale on the jumpbox:"
echo "   curl -fsSL https://tailscale.com/install.sh | sh"
echo ""
echo "3. Register with Headscale:"
echo "   sudo tailscale up --login-server=$HEADSCALE_URL --hostname=$JUMPBOX_NAME"
echo ""
echo "4. On your Headscale server, approve the jumpbox:"
echo "   headscale nodes list"
echo "   headscale nodes register --user $HEADSCALE_USER --key <node_key>"
echo ""
echo "5. Get the VPN IP and share with students:"
echo "   tailscale ip -4"
echo ""
echo "6. Set up student SSH access (create accounts or use SSH keys)"
echo ""
echo "To configure this jumpbox, run:"
echo "  ./scripts/configure_jumpbox.sh $JUMPBOX_IP"
echo ""
