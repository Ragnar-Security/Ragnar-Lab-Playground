# VLAN Management and Database Migration Guide

## Overview

Each student is assigned **two unique VLAN tags** during database initialization:
- **Entry VLAN** (100-199): Network `10.<vlan>.0.0/24`, with the jumpbox as gateway and DHCP server (`docs/JUMPBOX.md`). The entry point VM's `net0` is on it.
- **Internal VLAN** (200-299): Network `10.<vlan-100>.1.0/24`, which the jumpbox also serves (`docs/JUMPBOX.md`). The entry point VM's `net1` and a plain VM's only NIC are on it.

## Architecture

### VLAN Assignment
- VLANs are pre-assigned when students are imported from CSV into the whitelist
- When a student registers, their VLANs are copied from the whitelist to the user account
- Student at index `i` gets: `entry_vlan = 100 + i`, `internal_vlan = 200 + i`

### VM Network Configuration
- **Entry point VMs**: `net0` uses the entry VLAN (100-199) and `net1` uses the internal VLAN (200-299). A QEMU template can set `internal_nic: false` to get `net0` only; an LXC entry point always gets both NICs.
- **Plain VMs** (every other machine of the group): `net0` uses the internal VLAN (200-299) and there is no `net1`.
- The entry point VM is marked with `entry_point: true` in `templates.yaml`

### IP Display
- Every VM shows the addresses its guest reports in the dashboard, entry network (`10.<vlan>.0.x`) and internal network (`10.<vlan-100>.1.x`) alike
- A VM with no usable address yet shows `N/A` (entry point) or `-` (other VMs)

## Database Schema

### Users Table
- `entry_vlan_tag` (integer, unique): Entry point VLAN (100-199)
- `internal_vlan_tag` (integer, unique): Internal VLAN (200-299)

### Student Whitelist Table
- `entry_vlan_tag` (integer, unique): Pre-assigned entry VLAN
- `internal_vlan_tag` (integer, unique): Pre-assigned internal VLAN

### VM Records Table
- `is_entry_point` (boolean): Whether this VM is the entry point in its deployment group

## Network Configuration Applied

- **LXC (entry point)**: `net0=name=eth0,bridge=vmbr0,tag={entry_vlan},ip=dhcp` and `net1=name=eth1,bridge=vmbr0,tag={internal_vlan},ip=dhcp`
- **LXC (plain machine)**: `net0=name=eth0,bridge=vmbr0,tag={internal_vlan},ip=dhcp` only
- **QEMU (entry point)**: `net0=virtio,bridge=vmbr0,tag={entry_vlan}` and `net1=virtio,bridge=vmbr0,tag={internal_vlan}`; with `internal_nic: false`, `net1` is deleted instead
- **QEMU (plain machine)**: `net0=virtio,bridge=vmbr0,tag={internal_vlan}`, and any `net1` on the template is deleted

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
    bridge-vids 100-500
```

Configure your physical switch trunk port to allow VLANs 100-500 (student VLANs 100-299, group VLANs 300-500).

## Verification

```sql
-- Check whitelist VLAN assignments
SELECT login_id, entry_vlan_tag, internal_vlan_tag FROM student_whitelist ORDER BY entry_vlan_tag;

-- Check user VLAN assignments
SELECT login_id, entry_vlan_tag, internal_vlan_tag FROM users ORDER BY entry_vlan_tag;

-- Check entry point VMs
SELECT vm_name, is_entry_point FROM vm_records WHERE is_entry_point = 1;
```
