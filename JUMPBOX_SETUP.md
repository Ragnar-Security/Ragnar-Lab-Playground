# Jumpbox Setup Guide

## Overview

This guide explains how to set up a shared **jumpbox** VM that all students use to access their internal VMs via Tailscale/Headscale VPN.

## Architecture

```
[Student's Computer]
    ↓ (Tailscale VPN)
[Headscale VPN Network]
    ↓
[Shared Jumpbox - 100.x.x.x] ← All students SSH here
    ↓ (Internal Network)
[Student VMs - 10.1.x.x] ← Students SSH from jumpbox
```

**Benefits:**
- Simple setup (one VM for everyone)
- No cloud-init needed
- Easy to troubleshoot
- Students learn bastion/jump host pattern

## Prerequisites

1. Proxmox cluster with SSH access
2. Headscale server running (for example, `https://vpn.example.edu`)
3. Student list in `data/students.txt` (optional, for account creation)

## Quick Start

### Step 1: Create the Jumpbox VM

```bash
cd /opt/lab-playground
cp scripts/site.env.example scripts/site.env

# Set these in scripts/site.env; exported environment variables override them.
JUMPBOX_NODE=node1
JUMPBOX_STORAGE=local-zfs
HEADSCALE_URL=https://vpn.example.edu

# Optional per-run values
export JUMPBOX_VMID="9999"

./scripts/create_jumpbox.sh
```

The script will:
- Download Ubuntu cloud image
- Create a VM on your Proxmox cluster
- Configure networking
- Output the jumpbox IP address

### Step 2: Configure Tailscale and Student Access

```bash
# Get the jumpbox IP from Step 1 output
export JUMPBOX_IP="<jumpbox-lan-ip>"  # Use the address from Step 1

./scripts/configure_jumpbox.sh "$JUMPBOX_IP"
```

This will:
- Install Tailscale on the jumpbox
- Prompt you to register with Headscale
- Create student accounts
- Configure SSH access

### Step 3: Manual Headscale Registration

During configuration, you'll need to manually register the jumpbox:

```bash
# SSH to jumpbox
ssh ubuntu@<jumpbox-lan-ip>

# Use the deployment's HEADSCALE_URL
sudo tailscale up --login-server=https://vpn.example.edu --hostname=jumpbox

# Copy the authentication URL and paste in browser
# OR use the provided key with headscale CLI
```

On your Headscale server:
```bash
headscale nodes list
headscale nodes register --user jumpbox --key <node_key>
```

Get the VPN IP:
```bash
tailscale ip -4
# Example output: <jumpbox-vpn-ip>
```

### Step 4: Record Connection Details

Record the jumpbox hostname or VPN address shown by `tailscale ip -4` and
distribute it through the deployment's approved student instructions. Do not
commit deployment addresses or credentials to this repository.

## Student Workflow

### 1. Install Tailscale
Students download and install from: https://tailscale.com/download

### 2. Connect to Headscale
```bash
tailscale up --login-server=https://vpn.example.edu
```

### 3. SSH to Jumpbox
```bash
ssh <username>@<jumpbox-vpn-ip>
# Password: <choose-a-password> (change on first login)
```

### 4. Access Their VMs
From the jumpbox:
```bash
# List available VMs (from web UI)
# Then SSH to internal IP
ssh root@<vm-lan-ip>
```

## Student Account Management

### Auto-Create from CSV

Place student list in `data/students.txt`:
```
student1
student2
student3
```

Run configuration script again:
```bash
./scripts/configure_jumpbox.sh $JUMPBOX_IP
```

### Manual Account Creation

SSH to jumpbox:
```bash
ssh ubuntu@<jumpbox_ip>

# Create user
sudo useradd -m -s /bin/bash newstudent
sudo passwd newstudent

# Optional: Add to sudoers (if needed)
sudo usermod -aG sudo newstudent
```

## Customization

### Change Jumpbox VM Settings

Edit variables before running `create_jumpbox.sh`:

```bash
export JUMPBOX_VMID="9999"
export JUMPBOX_NAME="jumpbox"
export JUMPBOX_MEMORY="4096"      # MB
export JUMPBOX_CORES="2"
export JUMPBOX_DISK_SIZE="32G"
export JUMPBOX_NODE="node1"          # Your Proxmox node
export JUMPBOX_STORAGE="local-zfs"   # Your Proxmox storage
```

### Use Different Cloud Image

The script defaults to Ubuntu 22.04 (Jammy). To use a different version:

```bash
# Edit scripts/create_jumpbox.sh
# Change the CLOUD_IMAGE URL to:
# - Ubuntu 20.04: https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img
# - Ubuntu 24.04: https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
# - Debian 12: https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2
```

## Troubleshooting

### Cannot SSH to Jumpbox

Check VM is running:
```bash
ssh root@<proxmox-host> "qm status <vmid>"
```

Access via Proxmox console:
```bash
# In Proxmox web UI: Select VM > Console
# Or use CLI:
ssh root@<proxmox-host> "qm terminal <vmid>"
```

### Tailscale Not Connecting

Check Tailscale status:
```bash
ssh ubuntu@<jumpbox-lan-ip>
sudo tailscale status
sudo journalctl -u tailscaled -n 50
```

Re-register:
```bash
sudo tailscale down
sudo tailscale up --login-server=https://vpn.example.edu
```

### Students Can't SSH from Jumpbox to VMs

Verify network connectivity:
```bash
# From jumpbox
ping <vm-lan-ip>
ssh -v root@<vm-lan-ip>
```

Check Proxmox firewall rules and VM network configuration.

## Security Best Practices

1. **Change Default Passwords**: Have students change password on first login
2. **Use SSH Keys**: Consider SSH key-based authentication instead of passwords
3. **Limit sudo Access**: Only give sudo to instructors/TAs
4. **Monitor Access**: Check `/var/log/auth.log` on jumpbox
5. **Keep Updated**: Regularly update jumpbox: `sudo apt update && sudo apt upgrade`

## Backup and Recovery

### Backup Jumpbox VM

```bash
# Create snapshot
ssh root@<proxmox-host> "qm snapshot <vmid> pre-changes"

# Or backup to storage
ssh root@<proxmox-host> "vzdump <vmid> --storage <storage>"
```

### Recreate from Scratch

If jumpbox is lost, just re-run the scripts:
```bash
./scripts/create_jumpbox.sh
./scripts/configure_jumpbox.sh <jumpbox-lan-ip>
```


## Comparison with Cloud-Init Approach

| Feature | Jumpbox | Cloud-Init Per-VM |
|---------|---------|-------------------|
| Setup Complexity | ⭐ Simple | ⭐⭐⭐ Complex |
| Per-Student Isolation | ❌ Shared | ✅ Isolated |
| Debugging | ⭐⭐⭐ Easy | ⭐ Hard |
| Production-Ready | ⭐ No | ⭐⭐⭐ Yes |
| Educational Value | ⭐⭐ Good | ⭐⭐⭐ Excellent |

For a **class environment**, jumpbox is recommended for simplicity.
