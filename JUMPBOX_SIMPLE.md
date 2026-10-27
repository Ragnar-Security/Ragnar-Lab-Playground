# Jumpbox Setup - Simple Guide

## Step 1: Create Ubuntu VM in Proxmox (Your Way)

Create a VM however you prefer:
- **Option A**: Download Ubuntu 24.04 ISO, create VM, install normally
- **Option B**: Use existing template
- **Option C**: Import cloud image

### Recommended Settings:
- **VM ID**: 9999 (or any available)
- **Name**: jumpbox
- **Memory**: 4096 MB
- **Cores**: 2
- **Disk**: 32 GB
- **Network**: Bridge vmbr0, VLAN tag 20
- **OS**: Ubuntu 22.04 or 24.04

### Quick Console Command (if using ISO):
```bash
# In Proxmox shell
qm create 9999 \
  --name jumpbox \
  --memory 4096 \
  --cores 2 \
  --net0 virtio,bridge=vmbr0,tag=20 \
  --scsi0 <storage>:32 \
  --ide2 local:iso/ubuntu-24.04-live-server-amd64.iso,media=cdrom \
  --boot order=scsi0 \
  --ostype l26

qm start 9999
```

Then install Ubuntu via console (choose OpenSSH server during install).

## Step 2: Get the VM's IP Address

After installation, find the IP:
```bash
# In Proxmox shell
qm guest cmd 9999 network-get-interfaces

# Or check in Proxmox web UI: VM > Summary
```

## Step 3: Run Configuration Script

From your workstation checkout:
```bash
cd /opt/lab-playground
cp scripts/site.env.example scripts/site.env
# Set HEADSCALE_URL, JUMPBOX_NODE, and JUMPBOX_STORAGE in scripts/site.env.

# Run the config script with the VM address reported by Proxmox
./scripts/configure_jumpbox.sh <jumpbox-lan-ip>

# Or with a custom SSH user
./scripts/configure_jumpbox.sh <jumpbox-lan-ip> root
```

The script will:
1. ✅ Update the system
2. ✅ Install Tailscale
3. ✅ Prompt you to register with Headscale
4. ✅ Install useful tools
5. ✅ Create student accounts from `data/students.txt`
6. ✅ Configure SSH

## Step 4: Manual Tailscale Registration

When prompted, SSH to the jumpbox:
```bash
ssh ubuntu@<jumpbox-lan-ip>  # or root@<jumpbox-lan-ip>

sudo tailscale up --login-server=https://vpn.example.edu --hostname=jumpbox --accept-routes
```

Follow the authentication instructions, then get the VPN IP:
```bash
tailscale ip -4
# Example: <jumpbox-vpn-ip>
```

## Step 5: Share Info with Students

Give students:
1. **Jumpbox VPN IP**: the address from `tailscale ip -4`
2. **Their username**: From `data/students.txt`
3. **Initial password**: `<choose-a-password>`

### Student Workflow:
```bash
# 1. Install Tailscale
# Download from https://tailscale.com/download

# 2. Connect to Headscale
tailscale up --login-server=https://vpn.example.edu

# 3. SSH to jumpbox
ssh <username>@<jumpbox-vpn-ip>

# 4. From jumpbox, SSH to their VMs
ssh root@<vm-lan-ip>  # Internal IP from web interface
```

## Troubleshooting

### Can't SSH to jumpbox
```bash
# Try from Proxmox console
qm terminal 9999

# Or check if SSH is running
systemctl status sshd
```

### Tailscale not connecting
```bash
sudo tailscale status
sudo journalctl -u tailscaled -n 50
```

### Add more students later
```bash
# SSH to jumpbox
ssh ubuntu@<jumpbox_ip>

# Create account
sudo useradd -m -s /bin/bash newstudent
sudo passwd newstudent
```

## That's It!

No complicated scripts. Just:
1. Create VM (any way you want)
2. Run `configure_jumpbox.sh`
3. Register Tailscale manually
4. Done!
