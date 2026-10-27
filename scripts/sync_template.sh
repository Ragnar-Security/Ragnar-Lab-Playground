#!/bin/bash
#
# Cross-Node Template Sync Script
# Syncs a Proxmox VM/LXC template from one node to another using vzdump + restore.
# Auto-detects whether the source VMID is QEMU or LXC.
#
# Prerequisites:
#   - SSH key-based access configured between the machine running this script
#     and both Proxmox nodes (e.g., ssh root@node1, ssh root@node2)
#   - vzdump, qmrestore/pct, and qm/pct available on both nodes
#   - pvesh available on the target node (for /cluster/nextid)
#   - Sufficient disk space in /var/lib/vz/dump for the backup file
#
# The source/target nodes can be hostnames (node1, node2) or IP addresses.
# Hostnames must be resolvable via DNS, /etc/hosts, or ~/.ssh/config.
#
# Usage: ./scripts/sync_template.sh <source_node> <source_vmid> <target_node> [target_storage]
# Example (QEMU): ./scripts/sync_template.sh node1 137 node2 local-zfs
# Example (LXC):  ./scripts/sync_template.sh node1 135 node2 local-zfs
# Example (IPs):  ./scripts/sync_template.sh 192.0.2.10 135 192.0.2.20 local-zfs
#

set -euo pipefail

# --- Argument validation ---
if [ $# -lt 3 ]; then
    echo "Usage: $0 <source_node> <source_vmid> <target_node> [target_storage]"
    echo ""
    echo "Arguments:"
    echo "  source_node      Hostname or IP of the source Proxmox node (e.g., node1 or 192.0.2.10)"
    echo "  source_vmid      VMID of the template to sync on the source node"
    echo "  target_node      Hostname or IP of the target Proxmox node (e.g., node2 or 192.0.2.20)"
    echo "  target_storage   Storage pool on the TARGET node to restore into (default: local-zfs)"
    echo ""
    echo "Supports both QEMU VMs and LXC containers (auto-detected)."
    echo ""
    echo "Examples:"
    echo "  $0 node1 137 node2 local-zfs        # QEMU VM, using hostnames"
    echo "  $0 node1 135 node2 local-zfs        # LXC container, using hostnames"
    echo "  $0 192.0.2.10 135 192.0.2.20 local-zfs  # Using IP addresses"
    exit 1
fi

SOURCE_NODE="$1"
SOURCE_VMID="$2"
TARGET_NODE="$3"
TARGET_STORAGE="${4:-local-zfs}"
DUMP_DIR="/var/lib/vz/dump"

echo "========================================"
echo "  Cross-Node Template Sync"
echo "========================================"
echo ""
echo "  Source node:      $SOURCE_NODE"
echo "  Source VMID:      $SOURCE_VMID"
echo "  Target node:      $TARGET_NODE"
echo "  Target storage:   $TARGET_STORAGE  (storage pool on $TARGET_NODE where template will be restored)"
echo ""

# --- Step 0: Verify SSH connectivity ---
echo "[Step 0/8] Verifying SSH connectivity..."
echo "  → Testing SSH to source node ($SOURCE_NODE)..."
if ! ssh -o ConnectTimeout=5 "root@${SOURCE_NODE}" "echo 'OK'" &>/dev/null; then
    echo "  ERROR: Cannot SSH to root@${SOURCE_NODE}"
    echo "  Make sure SSH key-based auth is set up and the host is reachable."
    echo "  If using a hostname, ensure it resolves (check /etc/hosts or ~/.ssh/config)."
    exit 1
fi
echo "  ✓ Source node ($SOURCE_NODE) reachable"

echo "  → Testing SSH to target node ($TARGET_NODE)..."
if ! ssh -o ConnectTimeout=5 "root@${TARGET_NODE}" "echo 'OK'" &>/dev/null; then
    echo "  ERROR: Cannot SSH to root@${TARGET_NODE}"
    echo "  Make sure SSH key-based auth is set up and the host is reachable."
    echo "  If using a hostname, ensure it resolves (check /etc/hosts or ~/.ssh/config)."
    exit 1
fi
echo "  ✓ Target node ($TARGET_NODE) reachable"

# --- Step 1: Auto-detect VM type ---
echo ""
echo "[Step 1/8] Detecting VM type for VMID $SOURCE_VMID on $SOURCE_NODE..."
echo "  → Checking if VMID $SOURCE_VMID is a QEMU VM..."
if ssh "root@${SOURCE_NODE}" "qm status ${SOURCE_VMID}" &>/dev/null; then
    VM_TYPE="qemu"
    echo "  ✓ Detected: QEMU VM"
else
    echo "  → Not a QEMU VM. Checking if it's an LXC container..."
    if ssh "root@${SOURCE_NODE}" "pct status ${SOURCE_VMID}" &>/dev/null; then
        VM_TYPE="lxc"
        echo "  ✓ Detected: LXC container"
    else
        echo "  ERROR: VMID $SOURCE_VMID not found as QEMU or LXC on $SOURCE_NODE"
        echo "  Verify the VMID exists: ssh root@${SOURCE_NODE} 'qm list; pct list'"
        exit 1
    fi
fi

# Set type-specific variables
if [ "$VM_TYPE" = "qemu" ]; then
    DUMP_PATTERN="vzdump-qemu-${SOURCE_VMID}-*.vma.zst"
else
    DUMP_PATTERN="vzdump-lxc-${SOURCE_VMID}-*.tar.zst"
fi

# --- Step 2: Verify target storage exists ---
echo ""
echo "[Step 2/8] Verifying target storage '$TARGET_STORAGE' exists on $TARGET_NODE..."
if ssh "root@${TARGET_NODE}" "pvesm status --storage ${TARGET_STORAGE}" &>/dev/null; then
    echo "  ✓ Storage '$TARGET_STORAGE' found on $TARGET_NODE"
else
    echo "  ERROR: Storage '$TARGET_STORAGE' not found on $TARGET_NODE"
    echo "  Available storage pools on $TARGET_NODE:"
    ssh "root@${TARGET_NODE}" "pvesm status" 2>/dev/null | head -20 || true
    exit 1
fi

# --- Step 3: Check for existing backups or create new one ---
echo ""
echo "[Step 3/8] Checking for existing backup files..."

# Check if a backup already exists on the target node (from a previous interrupted run)
EXISTING_TARGET=$(ssh "root@${TARGET_NODE}" "ls -t ${DUMP_DIR}/${DUMP_PATTERN} 2>/dev/null | head -1" || true)
if [ -n "$EXISTING_TARGET" ]; then
    EXISTING_TARGET_NAME=$(basename "$EXISTING_TARGET")
    EXISTING_TARGET_SIZE=$(ssh "root@${TARGET_NODE}" "du -h '${EXISTING_TARGET}' | cut -f1")
    echo "  ⚠ Found existing backup on TARGET node ($TARGET_NODE):"
    echo "    $EXISTING_TARGET_NAME ($EXISTING_TARGET_SIZE)"
    echo ""
    read -p "  Use this existing backup and skip to restore? (y/n): " USE_TARGET_BACKUP
    if [[ "$USE_TARGET_BACKUP" =~ ^[Yy]$ ]]; then
        echo "  → Reusing existing backup on $TARGET_NODE"
        BACKUP_BASENAME="$EXISTING_TARGET_NAME"
        SKIP_BACKUP=true
        SKIP_TRANSFER=true
    fi
fi

# Check if a backup already exists on the source node
if [ "${SKIP_BACKUP:-}" != "true" ]; then
    EXISTING_SOURCE=$(ssh "root@${SOURCE_NODE}" "ls -t ${DUMP_DIR}/${DUMP_PATTERN} 2>/dev/null | head -1" || true)
    if [ -n "$EXISTING_SOURCE" ]; then
        EXISTING_SOURCE_NAME=$(basename "$EXISTING_SOURCE")
        EXISTING_SOURCE_SIZE=$(ssh "root@${SOURCE_NODE}" "du -h '${EXISTING_SOURCE}' | cut -f1")
        echo "  ⚠ Found existing backup on SOURCE node ($SOURCE_NODE):"
        echo "    $EXISTING_SOURCE_NAME ($EXISTING_SOURCE_SIZE)"
        echo ""
        read -p "  Reuse this backup instead of creating a new one? (y/n): " USE_SOURCE_BACKUP
        if [[ "$USE_SOURCE_BACKUP" =~ ^[Yy]$ ]]; then
            echo "  → Reusing existing backup on $SOURCE_NODE"
            BACKUP_FILE="$EXISTING_SOURCE"
            BACKUP_BASENAME="$EXISTING_SOURCE_NAME"
            SKIP_BACKUP=true
        fi
    fi
fi

# Create new backup if not reusing an existing one
if [ "${SKIP_BACKUP:-}" != "true" ]; then
    echo "  → No existing backups found (or chose not to reuse them)"
    echo "  → Creating new backup of $VM_TYPE VMID $SOURCE_VMID on $SOURCE_NODE..."
    echo "  → Running vzdump (this may take a few minutes for large VMs)..."
    echo "  → Command: vzdump ${SOURCE_VMID} --mode stop --compress zstd --dumpdir ${DUMP_DIR}"
    ssh "root@${SOURCE_NODE}" "vzdump ${SOURCE_VMID} --mode stop --compress zstd --dumpdir ${DUMP_DIR}"
    echo "  ✓ Backup complete"
fi

# --- Step 4: Find the backup file on source node ---
if [ "${SKIP_TRANSFER:-}" != "true" ]; then
    echo ""
    echo "[Step 4/8] Locating backup file on $SOURCE_NODE..."
    echo "  → Looking for: ${DUMP_DIR}/${DUMP_PATTERN}"

    if [ -z "${BACKUP_FILE:-}" ]; then
        BACKUP_FILE=$(ssh "root@${SOURCE_NODE}" "ls -t ${DUMP_DIR}/${DUMP_PATTERN} 2>/dev/null | head -1")
    fi

    if [ -z "$BACKUP_FILE" ]; then
        echo "  ERROR: Could not find backup file matching '${DUMP_PATTERN}' in ${DUMP_DIR} on $SOURCE_NODE"
        echo "  Check if vzdump completed successfully."
        exit 1
    fi

    BACKUP_BASENAME=$(basename "$BACKUP_FILE")
    BACKUP_SIZE=$(ssh "root@${SOURCE_NODE}" "du -h '${BACKUP_FILE}' | cut -f1")
    echo "  ✓ Found: $BACKUP_BASENAME ($BACKUP_SIZE)"
else
    echo ""
    echo "[Step 4/8] Skipping — using existing backup on $TARGET_NODE"
fi

# --- Step 5: Copy backup to target node ---
if [ "${SKIP_TRANSFER:-}" != "true" ]; then
    echo ""
    echo "[Step 5/8] Copying backup from $SOURCE_NODE to $TARGET_NODE..."
    echo "  → Transferring $BACKUP_BASENAME (${BACKUP_SIZE:-unknown size}) via SCP..."
    echo "  → This may take a while depending on file size and network speed..."
    ssh "root@${SOURCE_NODE}" "scp '${BACKUP_FILE}' 'root@${TARGET_NODE}:${DUMP_DIR}/${BACKUP_BASENAME}'"
    echo "  ✓ Transfer complete"
else
    echo ""
    echo "[Step 5/8] Skipping transfer — backup already exists on $TARGET_NODE"
fi

# --- Step 6: Get next available VMID and restore ---
echo ""
echo "[Step 6/8] Getting next available VMID on $TARGET_NODE..."
NEW_VMID=$(ssh "root@${TARGET_NODE}" "pvesh get /cluster/nextid")
echo "  ✓ Next available VMID: $NEW_VMID"

echo ""
echo "[Step 7/8] Restoring backup as $VM_TYPE VMID $NEW_VMID on $TARGET_NODE..."
echo "  → Storage: $TARGET_STORAGE"
if [ "$VM_TYPE" = "qemu" ]; then
    echo "  → Command: qmrestore '${DUMP_DIR}/${BACKUP_BASENAME}' ${NEW_VMID} --storage ${TARGET_STORAGE}"
    ssh "root@${TARGET_NODE}" "qmrestore '${DUMP_DIR}/${BACKUP_BASENAME}' ${NEW_VMID} --storage ${TARGET_STORAGE}"
else
    echo "  → Command: pct restore ${NEW_VMID} '${DUMP_DIR}/${BACKUP_BASENAME}' --storage ${TARGET_STORAGE}"
    ssh "root@${TARGET_NODE}" "pct restore ${NEW_VMID} '${DUMP_DIR}/${BACKUP_BASENAME}' --storage ${TARGET_STORAGE}"
fi
echo "  ✓ Restore complete"

# --- Step 8: Convert to template (if not already one) ---
echo ""
echo "[Step 8/8] Checking if VMID $NEW_VMID is already a template..."
if [ "$VM_TYPE" = "qemu" ]; then
    IS_TEMPLATE=$(ssh "root@${TARGET_NODE}" "qm config ${NEW_VMID} | grep -c '^template:' || true")
else
    IS_TEMPLATE=$(ssh "root@${TARGET_NODE}" "pct config ${NEW_VMID} | grep -c '^template:' || true")
fi

if [ "$IS_TEMPLATE" -gt 0 ]; then
    echo "  ✓ Already a template — skipping conversion"
else
    echo "  → Converting $VM_TYPE VMID $NEW_VMID to template on $TARGET_NODE..."
    if [ "$VM_TYPE" = "qemu" ]; then
        echo "  → Command: qm template ${NEW_VMID}"
        ssh "root@${TARGET_NODE}" "qm template ${NEW_VMID}"
    else
        echo "  → Command: pct template ${NEW_VMID}"
        ssh "root@${TARGET_NODE}" "pct template ${NEW_VMID}"
    fi
    echo "  ✓ Template conversion complete"
fi

# --- Cleanup ---
echo ""
echo "[Cleanup] Removing temporary backup files..."
echo "  → Removing backup from $SOURCE_NODE..."
ssh "root@${SOURCE_NODE}" "rm -f '${BACKUP_FILE}'" && echo "  ✓ Removed from $SOURCE_NODE"
ssh "root@${SOURCE_NODE}" "rm -f '${BACKUP_FILE%.*.zst}.log'" 2>/dev/null || true
echo "  → Removing backup from $TARGET_NODE..."
ssh "root@${TARGET_NODE}" "rm -f '${DUMP_DIR}/${BACKUP_BASENAME}'" && echo "  ✓ Removed from $TARGET_NODE"
ssh "root@${TARGET_NODE}" "rm -f '${DUMP_DIR}/${BACKUP_BASENAME%.*.zst}.log'" 2>/dev/null || true

# --- Done ---
echo ""
echo "========================================"
echo "  Sync Complete!"
echo "========================================"
echo ""
echo "  Type:         $VM_TYPE"
echo "  Source:        VMID $SOURCE_VMID on $SOURCE_NODE"
echo "  New template:  VMID $NEW_VMID on $TARGET_NODE (storage: $TARGET_STORAGE)"
echo ""
echo "Update your templates.yaml with:"
echo ""
echo "        template_locations:"
echo "          ${SOURCE_NODE}: ${SOURCE_VMID}"
echo "          ${TARGET_NODE}: ${NEW_VMID}"
echo ""
