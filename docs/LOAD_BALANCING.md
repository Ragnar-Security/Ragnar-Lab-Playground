# Dynamic Node Selection and Load Balancing

This document describes the dynamic node selection and load balancing feature that intelligently distributes VMs across Proxmox nodes based on resource availability.

## Overview

The system can automatically select the best node for VM deployment based on current CPU and memory usage across all nodes in your Proxmox cluster. This ensures optimal resource distribution and prevents overloading individual nodes.

**Configuration Levels:**
1. **Global (`.env` file)**: Default settings for all deployment groups
2. **Per-deployment-group**: The operator-owned YAML selected by
   `TEMPLATES_FILE` (defaults to untracked `templates.yaml`)

## Configuration

### Global Settings (`.env` file)

Add these settings to your `.env` file as defaults:

```env
# Enable intelligent load balancing globally
ENABLE_LOAD_BALANCING=false

# Optional: Specify a fixed node when load balancing is disabled
FIXED_NODE=node1

# Resource thresholds (nodes exceeding these will be avoided)
CPU_THRESHOLD=80.0
MEMORY_THRESHOLD=85.0
```

### Per-Deployment-Group Settings (`templates.yaml`)

Override global settings for specific deployment groups:

```yaml
deployment_groups:
  - display_name: "homework2"
    description: "Single VM - use fixed node"
    enabled: true
    uptime_minutes: 4320
    extend_time_minutes: 1440
    # Load balancing configuration for this group
    load_balancing:
      enabled: false  # Override global setting
      fixed_node: "node1"  # Deploy to a specific node
    templates:
      - name: "wordpress"
        # ... template config ...

  - display_name: "homework3"
    description: "Multi-VM - use load balancing"
    enabled: true
    uptime_minutes: 4320
    extend_time_minutes: 1440
    # Enable load balancing for this group
    load_balancing:
      enabled: true
      cpu_threshold: 75.0  # More conservative than global
      memory_threshold: 80.0
    templates:
      - name: "loadbalancer"
        # ... template config ...
      - name: "webserver1"
        # ... template config ...
```

### Configuration Priority

Settings are applied in this order (later overrides earlier):
1. Default values (hardcoded in `config.py`)
2. Global `.env` settings
3. Per-deployment-group settings in the file selected by `TEMPLATES_FILE`

### Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LOAD_BALANCING` | `false` | Enable/disable automatic load balancing |
| `FIXED_NODE` | `` | Fixed node name when load balancing is disabled (empty = use first available) |
| `CPU_THRESHOLD` | `80.0` | Maximum CPU usage % before avoiding a node |
| `MEMORY_THRESHOLD` | `85.0` | Maximum memory usage % before avoiding a node |

## How It Works

### Fixed Node Mode (Default)

When `ENABLE_LOAD_BALANCING=false`:
- If `FIXED_NODE` is specified, all VMs are deployed to that node
- If `FIXED_NODE` is empty, the highest-scoring node is used
- The memory admission check below still applies. A fixed node without room
  is refused; it is never overcommitted.

### Load Balancing Mode

When `ENABLE_LOAD_BALANCING=true`:

1. **Resource Discovery**: One `cluster/resources` call for guest memory, plus
   each node's `status`.
2. **Filtering**: Exclude nodes that:
   - Are offline
   - Do not hold a copy of the template (`template_locations`)
   - Exceed CPU threshold
   - Exceed memory threshold (`used / total`)
   - Have less effective available memory than the VM needs plus 10%
   - Don't have enough CPU cores for the VM
3. **Scoring**: Calculate a score for each suitable node based on:
   - CPU availability (30% weight)
   - Memory availability (30% weight)
   - Effective available memory in GB (40% weight)
4. **Selection**: Weighted-random choice among suitable nodes, favouring
   higher scores. The chosen node's memory is reserved immediately.

If no node passes, the deploy fails with "No suitable nodes available". There
is no fallback to an unsuitable node.

### Memory Admission

```
effective_available = available - committed - reserved

where:
  available = Proxmox status memory.available (Linux MemAvailable)
  committed = sum over this app's live VMRecords on the node of
              max(0, configured RAM - RAM the guest currently uses)
  reserved  = memory held for deploys that are selected but not yet recorded
```

- **available, not free.** `memory.free` is Linux MemFree, which leaves out the
  ZFS ARC and page cache that the kernel gives back under pressure. A node
  can report only a few GB free while still having tens of GB available; using
  MemFree incorrectly refuses VMs that the host can safely admit.
- **committed.** A guest that has just booted has not touched its RAM yet, so
  `available` overstates headroom for several minutes. Only this app's VMs are
  counted. Instructor VMs started by hand do not shrink student capacity.
- **reserved.** A class pressing Deploy together would otherwise all read the
  same snapshot. Selection and reservation happen in one database
  transaction that locks the admission row. A reservation is released when
  the VMRecord is committed or cleanup finishes, renewed every minute while a
  clone waits, and expires after 15 minutes if a code path leaks it. The
  ledger is the `node_memory_reservations` table, so every gunicorn worker
  admits against the same reservations.

Each selection logs one line per node:

```
Node node2 memory: free=4.76GB available=63.47GB committed=0.00GB reserved=6.00GB effective=57.47GB
```

### Scoring Algorithm

```
node_score = (cpu_availability × 0.3) + (memory_availability × 0.3) + (effective_memory_gb × 10 × 0.4)

where:
  cpu_availability = 100 - cpu_usage_percentage
  memory_availability = 100 - memory_usage_percentage
  effective_memory_gb = effective_available_bytes / (1024³)
```

## Usage Examples

### Example 1: Single Node Setup (Global)

For a single Proxmox node, use fixed node mode globally:

**`.env`:**
```env
ENABLE_LOAD_BALANCING=false
FIXED_NODE=node1
```

All deployment groups will use `node1` unless overridden.

### Example 2: Mixed Configuration

Use global defaults but override for specific assignments:

**`.env`:**
```env
ENABLE_LOAD_BALANCING=false  # Default to fixed node
FIXED_NODE=node1
CPU_THRESHOLD=80.0
MEMORY_THRESHOLD=85.0
```

**`templates.yaml`:**
```yaml
deployment_groups:
  # Simple homework - use fixed node (inherits global settings)
  - display_name: "homework1"
    enabled: true
    templates:
      - name: "simple-vm"
        # ... config ...

  # Complex homework - enable load balancing just for this
  - display_name: "homework2"
    enabled: true
    load_balancing:
      enabled: true  # Override global setting
      cpu_threshold: 70.0
      memory_threshold: 75.0
    templates:
      - name: "vm1"
        # ... config ...
      - name: "vm2"
        # ... config ...
```

### Example 3: Load Balancing for All Groups

Enable load balancing globally:

**`.env`:**
```env
ENABLE_LOAD_BALANCING=true
CPU_THRESHOLD=75.0
MEMORY_THRESHOLD=80.0
```

**`templates.yaml`:**
```yaml
deployment_groups:
  # Inherits global load balancing settings
  - display_name: "homework1"
    enabled: true
    templates:
      - name: "vm1"
        # ... config ...

  # Override with stricter thresholds
  - display_name: "homework2"
    enabled: true
    load_balancing:
      cpu_threshold: 60.0  # More conservative
      memory_threshold: 70.0
    templates:
      - name: "high-resource-vm"
        # ... config ...
```

### Example 4: Per-Node Assignment

Assign specific homeworks to specific nodes:

**`templates.yaml`:**
```yaml
deployment_groups:
  - display_name: "homework-basic"
    enabled: true
    load_balancing:
      enabled: false
      fixed_node: "node1"  # Lower-capacity node for simple VMs
    templates:
      - name: "basic-vm"
        memory: 1024
        cores: 1

  - display_name: "homework-advanced"
    enabled: true
    load_balancing:
      enabled: false
      fixed_node: "node2"  # Higher-capacity node for complex VMs
    templates:
      - name: "advanced-vm"
        memory: 8192
        cores: 4
```

## Monitoring Node Resources

### API Endpoint (Admin Only)

```bash
GET /nodes/resources
```

**Response:**
```json
{
  "success": true,
  "load_balancing_enabled": true,
  "fixed_node": "auto",
  "nodes": [
    {
      "node": "node1",
      "cpu_usage": 45.32,
      "memory_usage": 62.15,
      "memory_free_gb": 24.5,
      "memory_total_gb": 64.0,
      "cpu_count": 8,
      "online": true
    },
    {
      "node": "node2",
      "cpu_usage": 78.90,
      "memory_usage": 85.30,
      "memory_free_gb": 4.8,
      "memory_total_gb": 32.0,
      "cpu_count": 4,
      "online": true
    }
  ]
}
```

### Checking Logs

The system logs node selection decisions:

```bash
docker compose logs -f vm-automation
```

Example output:
```
Selecting best node for VM (Memory: 4096MB, Cores: 4)
Skipping node2: CPU usage 85.2% exceeds threshold 80.0%
Skipping node3: Memory usage 90.1% exceeds threshold 85.0%
Selected node: node1 (CPU: 45.3%, Memory: 62.1%, Score: 85.42)
```

## Benefits

1. **Automatic Load Distribution**: VMs are spread across nodes based on real-time resource availability
2. **Prevent Overload**: Nodes exceeding thresholds are automatically avoided
3. **Resource Optimization**: Nodes with more available resources are preferred
4. **No Overcommit**: If no node has room, the deploy is refused rather than placed on a full node
5. **Parallel Creation**: Concurrent deploys see each other's reservations

## Troubleshooting

### Issue: "No suitable nodes available"

**Symptoms**: Deploy fails with `Failed to create 1 VM(s): <template>: No suitable nodes available`.

**Diagnose first.** The app logs one line per skipped node with the reason:

```bash
docker logs --since 1h proxmox-vm-automation-prod 2>&1 | grep -E "Skipping|Node .* memory:|No suitable"
```

- `insufficient effective memory`: compare the `effective` figure with the VM's RAM
  plus 10%. If `available` is large but `committed` is large too, live app VMs
  have not ramped up yet, or there are stale VMRecords. Check All VMs for rows
  whose VM no longer exists. If `reserved` stays high with no deploys running,
  a reservation leaked; it clears itself within 15 minutes.
- If `available` itself is small, the node really is full. Stop idle VMs, or
  copy the template to the other node and add it to `template_locations`.
- `exceeds threshold`: raise `CPU_THRESHOLD` or `MEMORY_THRESHOLD`, or wait.
- Only nodes listed in the group's `template_locations` are considered.

**History**: the admission check once used `memory.free` (MemFree). On ZFS
hosts the ARC can keep MemFree low even when substantial memory is available,
causing large VMs to be refused unnecessarily.

### Issue: VMs still deploying to same node

**Symptoms**: Load balancing enabled but all VMs go to one node

**Solutions**:
1. Verify `ENABLE_LOAD_BALANCING=true` in `.env`
2. Restart the application: `docker compose restart vm-automation`
3. Check logs for node selection output
4. Ensure VM requirements don't exceed all nodes' available resources

### Issue: Permission denied when querying node resources

**Symptoms**: API endpoint returns error

**Solutions**:
1. Ensure Proxmox user/token has `Sys.Audit` permission
2. Grant permission via Proxmox UI: Datacenter → Permissions → Add
3. Assign `Sys.Audit` permission on `/` path

## Best Practices

1. **Set Appropriate Thresholds**: 
   - CPU: 70-80% for production workloads
   - Memory: 80-90% (leave buffer for system processes)

2. **Monitor Regularly**: Check node resources during peak usage times

3. **Test Before Deployment**: Test with a small number of VMs first

4. **Consider VM Diversity**: Large VMs benefit more from load balancing than small VMs

5. **Plan for Growth**: Set conservative thresholds if expecting increased load

## Performance Impact

- **Resource Query Overhead**: ~100-200ms per node during VM creation
- **Negligible Runtime Impact**: Only queries during VM creation, not during normal operation
- **Thread-Safe**: Works correctly with parallel VM creation (up to 5 concurrent)

## Future Enhancements

Potential improvements for future versions:
- Historical resource usage tracking
- Predictive node selection based on trends
- Custom scoring algorithms per deployment group
- Node affinity/anti-affinity rules
- Storage capacity consideration
- Network bandwidth monitoring
