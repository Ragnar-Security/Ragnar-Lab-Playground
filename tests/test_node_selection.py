"""
Node selection must size against reclaimable memory, not MemFree.

A 6 GB Windows VM was refused on both nodes because Proxmox
reported ~5-6 GB 'free' while ~60 GB was 'available' (ZFS ARC + page cache).
"""

import unittest
from unittest.mock import MagicMock

from app.proxmox_service import select_best_node

GIB = 1024 ** 3
LB_ON = {'enabled': True, 'cpu_threshold': 80.0, 'memory_threshold': 85.0}


def _proxmox_with_memory(memory):
    proxmox = MagicMock()
    proxmox.nodes.get.return_value = [{'node': 'pve'}]
    proxmox.nodes.return_value.status.get.return_value = {
        'cpu': 0.05,
        'uptime': 1000,
        'cpuinfo': {'cpus': 32},
        'memory': memory,
    }
    return proxmox


class TestNodeSelectionMemory(unittest.TestCase):

    def test_uses_available_memory_when_memfree_is_low(self):
        proxmox = _proxmox_with_memory({
            'total': 92 * GIB, 'used': 35 * GIB,
            'free': 2 * GIB, 'available': 56 * GIB,
        })
        node = select_best_node(proxmox, required_memory_mb=6144, required_cores=4,
                                load_balancing_config=LB_ON, available_nodes=['pve'])
        self.assertEqual(node, 'pve')

    def test_rejects_node_when_available_memory_is_short(self):
        proxmox = _proxmox_with_memory({
            'total': 92 * GIB, 'used': 30 * GIB,
            'free': 20 * GIB, 'available': 6 * GIB,
        })
        node = select_best_node(proxmox, required_memory_mb=6144, required_cores=4,
                                load_balancing_config=LB_ON, available_nodes=['pve'])
        self.assertIsNone(node)

    def test_falls_back_to_total_minus_used_without_available(self):
        proxmox = _proxmox_with_memory({
            'total': 92 * GIB, 'used': 35 * GIB, 'free': 6 * GIB,
        })
        node = select_best_node(proxmox, required_memory_mb=6144, required_cores=4,
                                load_balancing_config=LB_ON, available_nodes=['pve'])
        self.assertEqual(node, 'pve')


if __name__ == '__main__':
    unittest.main()
