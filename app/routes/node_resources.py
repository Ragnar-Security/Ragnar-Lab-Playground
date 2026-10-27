"""
Node Resources routes module
Handles cluster node resource monitoring (admin-only)
"""

from flask import Blueprint, jsonify

from ..proxmox_service import get_proxmox_connection, get_all_nodes_resources
from ..config import ENABLE_LOAD_BALANCING, FIXED_NODE
from ..auth import login_required, admin_required

# Create blueprint
node_resources_bp = Blueprint('node_resources', __name__)


@node_resources_bp.route('/nodes/resources', methods=['GET'])
@login_required
@admin_required
def get_nodes_resources():
    """
    Get resource usage information for all nodes in the cluster.
    Only available to admin users.

    Returns:
        JSON: {
            success: bool,
            load_balancing_enabled: bool,
            fixed_node: str,
            nodes: [
                {
                    node: str,
                    cpu_usage: float,
                    memory_usage: float,
                    memory_available_gb: float,  # Linux MemAvailable
                    memory_free_gb: float,  # DEPRECATED alias, see below
                    memory_linux_free_gb: float,  # Linux MemFree
                    memory_total_gb: float,
                    cpu_count: int,
                    online: bool
                }
            ],
            deprecated_fields: {memory_free_gb: 'memory_available_gb'}
        }

    ``memory_free_gb`` is deprecated despite its name: it is NOT Linux
    MemFree. It always equals ``memory_available_gb``, which is Proxmox
    ``memory.available`` (Linux MemAvailable; includes reclaimable page cache
    and ZFS ARC) or, when the node does not report it, ``total - used``. The
    value is clamped to [0, memory_total_gb]. Use ``memory_available_gb``;
    real MemFree is ``memory_linux_free_gb``. The alias is kept only for
    existing clients. Capacity decisions use the internal
    ``memory_effective_available`` (available minus app-owned commitments and
    pending reservations), which this endpoint does not report.
    """
    try:
        proxmox = get_proxmox_connection()
        nodes_data = get_all_nodes_resources(proxmox)

        # Format the response
        formatted_nodes = []
        for node in nodes_data:
            available = node.get('memory_available', node['memory_free'])
            formatted_nodes.append({
                'node': node['node'],
                'cpu_usage': node['cpu_usage'],
                'memory_usage': node['memory_usage'],
                'memory_available_gb': round(available / (1024**3), 2),
                # DEPRECATED alias kept for existing clients. Despite the
                # name it is MemAvailable, not Linux MemFree. Pinned by
                # tests/test_node_resources_api.py.
                'memory_free_gb': round(available / (1024**3), 2),
                'memory_linux_free_gb': round(
                    node.get('memory_linux_free', 0) / (1024**3), 2),
                'memory_total_gb': round(node['memory_total'] / (1024**3), 2),
                'cpu_count': node['cpu_count'],
                'online': node['online']
            })

        return jsonify({
            'success': True,
            'load_balancing_enabled': ENABLE_LOAD_BALANCING,
            'fixed_node': FIXED_NODE or 'auto',
            'nodes': formatted_nodes,
            'deprecated_fields': {'memory_free_gb': 'memory_available_gb'},
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
