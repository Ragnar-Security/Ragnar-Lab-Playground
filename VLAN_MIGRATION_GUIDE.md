# VLAN Management and Database Migration Guide

## Overview

Each student is assigned **two unique VLAN tags** during database initialization:
- **Entry VLAN** (100-200): Used for the entry point VM's second NIC. IP pattern: `10.0.<vlan>.x`
- **Internal VLAN** (200-300): Used for all VMs' primary NIC. IP pattern: `10.1.<vlan>.x`

## Architecture

### VLAN Assignment
- VLANs are pre-assigned when students are imported from CSV into the whitelist
- When a student registers, their VLANs are copied from the whitelist to the user account
- Student at index `i` gets: `entry_vlan = 100 + i`, `internal_vlan = 200 + i`

### VM Network Configuration
- **All VMs**: `net0` uses the internal VLAN (200-300)
- **Entry point VMs only**: `net1` additionally uses the entry VLAN (100-200)
- The entry point VM is marked with `entry_point: true` in `templates.yaml`

### IP Display
- Only the entry point VM's `10.0.x.x` IP is shown to students
- Non-entry-point VMs show `-` for IP in the dashboard

## Database Schema

### Users Table
- `entry_vlan_tag` (integer, unique): Entry point VLAN (100-200)
- `internal_vlan_tag` (integer, unique): Internal VLAN (200-300)

### Student Whitelist Table
- `entry_vlan_tag` (integer, unique): Pre-assigned entry VLAN
- `internal_vlan_tag` (integer, unique): Pre-assigned internal VLAN

### VM Records Table
- `is_entry_point` (boolean): Whether this VM is the entry point in its deployment group

## Network Configuration Applied

- **LXC (all VMs)**: `net0=name=eth0,bridge=vmbr0,tag={internal_vlan},ip=dhcp`
- **LXC (entry point)**: additionally `net1=name=eth1,bridge=vmbr0,tag={entry_vlan},ip=dhcp`
- **QEMU (all VMs)**: `net0=virtio,bridge=vmbr0,tag={internal_vlan}`
- **QEMU (entry point)**: additionally `net1=virtio,bridge=vmbr0,tag={entry_vlan}`

## Migration

### Option 1: Fresh Start (Recommended for development)

```bash
docker compose down -v
docker compose up -d --build
```

### Option 2: Migrate Existing Database

```bash
docker exec proxmox-vm-automation python scripts/migrate_add_vlan.py
```

## Proxmox Host Configuration

Ensure `vmbr0` bridge is VLAN-aware:
```bash
auto vmbr0
iface vmbr0 inet static
    address YOUR_IP
    netmask YOUR_NETMASK
    bridge-ports YOUR_INTERFACE
    bridge-stp off
    bridge-fd 0
    bridge-vlan-aware yes
    bridge-vids 100-300
```

Configure your physical switch trunk port to allow VLANs 100-300.

## Verification

```sql
-- Check whitelist VLAN assignments
SELECT login_id, entry_vlan_tag, internal_vlan_tag FROM student_whitelist ORDER BY entry_vlan_tag;

-- Check user VLAN assignments
SELECT login_id, entry_vlan_tag, internal_vlan_tag FROM users ORDER BY entry_vlan_tag;

-- Check entry point VMs
SELECT vm_name, is_entry_point FROM vm_records WHERE is_entry_point = 1;
```
