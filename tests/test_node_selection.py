"""
Node selection must size against reclaimable memory, not MemFree.

A 6 GB Windows VM was refused on both nodes because Proxmox
reported ~5-6 GB 'free' while ~60 GB was 'available' (ZFS ARC + page cache).
"""

import unittest
from unittest.mock import MagicMock

from app.proxmox_service import (
    _clear_node_reservations,
    _filter_suitable_nodes,
    get_node_resources,
    select_best_node,
)

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


def _proxmox_multi(statuses):
    """Cluster whose per-node status payloads come from ``statuses``."""
    proxmox = MagicMock()
    proxmox.nodes.get.return_value = [{'node': name} for name in statuses]
    proxmox.nodes.side_effect = lambda name: MagicMock(
        **{'status.get.return_value': statuses[name]})
    return proxmox


def _status(memory, *, cpu=0.05, uptime=1000, cpuinfo=None):
    status = {'cpu': cpu, 'uptime': uptime, 'memory': memory}
    status['cpuinfo'] = {'cpus': 32} if cpuinfo is None else cpuinfo
    return status


# Deterministic: load balancing off and no fixed node means "best score wins".
LB_OFF = {'enabled': False, 'fixed_node': '',
          'cpu_threshold': 80.0, 'memory_threshold': 85.0}


def _select(proxmox, mem_mb=6144, cores=4, config=LB_ON, **kwargs):
    return select_best_node(proxmox, required_memory_mb=mem_mb,
                            required_cores=cores,
                            load_balancing_config=config, **kwargs)


class _NodeSelectionEdgeCase(unittest.TestCase):
    def setUp(self):
        _clear_node_reservations()

    def tearDown(self):
        _clear_node_reservations()


class TestAvailableMemoryEdgeCases(_NodeSelectionEdgeCase):

    def test_missing_available_falls_back_to_total_minus_used(self):
        proxmox = _proxmox_with_memory({'total': 92 * GIB, 'used': 35 * GIB})
        resources = get_node_resources(proxmox, 'pve')
        self.assertEqual(resources['memory_available'], 57 * GIB)

    def test_null_available_is_treated_as_missing(self):
        proxmox = _proxmox_with_memory(
            {'total': 92 * GIB, 'used': 35 * GIB, 'available': None})
        resources = get_node_resources(proxmox, 'pve')
        self.assertTrue(resources['online'])
        self.assertEqual(resources['memory_available'], 57 * GIB)

    def test_zero_available_is_real_zero_not_a_missing_value(self):
        # total - used would be 62 GiB; an `or` fallback would admit this.
        proxmox = _proxmox_with_memory({
            'total': 92 * GIB, 'used': 30 * GIB,
            'free': 20 * GIB, 'available': 0})
        self.assertEqual(
            get_node_resources(proxmox, 'pve')['memory_available'], 0)
        self.assertIsNone(_select(proxmox, available_nodes=['pve']))

    def test_available_above_total_is_clamped_to_total(self):
        proxmox = _proxmox_with_memory({
            'total': 16 * GIB, 'used': 2 * GIB, 'available': 500 * GIB})
        resources = get_node_resources(proxmox, 'pve')
        self.assertEqual(resources['memory_available'], 16 * GIB)
        self.assertEqual(resources['memory_free'], 16 * GIB)

    def test_bad_available_cannot_admit_a_vm_larger_than_the_node(self):
        proxmox = _proxmox_with_memory({
            'total': 16 * GIB, 'used': 2 * GIB, 'available': 500 * GIB})
        self.assertIsNone(
            _select(proxmox, mem_mb=64 * 1024, available_nodes=['pve']))

    def test_negative_available_is_clamped_to_zero(self):
        proxmox = _proxmox_with_memory({
            'total': 16 * GIB, 'used': 2 * GIB, 'available': -5 * GIB})
        self.assertEqual(
            get_node_resources(proxmox, 'pve')['memory_available'], 0)

    def test_used_above_total_fallback_is_clamped_to_zero(self):
        proxmox = _proxmox_with_memory({'total': 16 * GIB, 'used': 20 * GIB})
        self.assertEqual(
            get_node_resources(proxmox, 'pve')['memory_available'], 0)

    def test_explicit_available_beats_total_minus_used_when_scoring(self):
        # Equal memory_usage, so only the available figure separates them.
        # total - used ties (52 GiB each) and the stable sort would pick
        # node-a; explicit available says node-b has more headroom.
        proxmox = _proxmox_multi({
            'node-a': _status({'total': 92 * GIB, 'used': 40 * GIB,
                               'available': 40 * GIB}),
            'node-b': _status({'total': 92 * GIB, 'used': 40 * GIB,
                               'available': 60 * GIB}),
        })
        self.assertEqual(
            _select(proxmox, config=LB_OFF,
                    available_nodes=['node-a', 'node-b']), 'node-b')

    def test_scoring_prefers_available_over_larger_total_minus_used(self):
        # node-a looks roomier by total - used but its available is small.
        proxmox = _proxmox_multi({
            'node-a': _status({'total': 92 * GIB, 'used': 30 * GIB,
                               'available': 10 * GIB}),
            'node-b': _status({'total': 92 * GIB, 'used': 30 * GIB,
                               'available': 40 * GIB}),
        })
        self.assertEqual(
            _select(proxmox, config=LB_OFF,
                    available_nodes=['node-a', 'node-b']), 'node-b')


class TestBufferBoundary(_NodeSelectionEdgeCase):
    """A VM needs its memory plus a 10% buffer of *effective* availability."""

    @staticmethod
    def _node(effective_bytes):
        return {'node': 'pve', 'online': True, 'cpu_usage': 5.0,
                'memory_usage': 10.0, 'cpu_count': 8,
                'memory_effective_available': effective_bytes}

    def _suitable(self, effective_bytes, mem_mb):
        return _filter_suitable_nodes(
            [self._node(effective_bytes)], mem_mb, 1, 80.0, 85.0)

    def test_node_exactly_at_the_buffer_is_admitted(self):
        for mem_mb in (1000, 1024, 4096, 6144, 7777, 10240):
            with self.subTest(mem_mb=mem_mb):
                exact = -(-mem_mb * 1024 * 1024 * 11 // 10)  # ceil, integer
                self.assertEqual(len(self._suitable(exact, mem_mb)), 1)

    def test_node_one_byte_below_the_buffer_is_refused(self):
        for mem_mb in (1000, 1024, 4096, 6144, 7777, 10240):
            with self.subTest(mem_mb=mem_mb):
                exact = -(-mem_mb * 1024 * 1024 * 11 // 10)
                self.assertEqual(self._suitable(exact - 1, mem_mb), [])

    def test_exact_boundary_through_select_best_node(self):
        # 10 GiB VM needs exactly 11 GiB; 11 GiB available is enough.
        ok = _proxmox_with_memory({'total': 64 * GIB, 'used': 53 * GIB,
                                   'available': 11 * GIB})
        self.assertEqual(
            _select(ok, mem_mb=10240, available_nodes=['pve']), 'pve')
        short = _proxmox_with_memory({'total': 64 * GIB, 'used': 53 * GIB,
                                      'available': 11 * GIB - 1})
        self.assertIsNone(
            _select(short, mem_mb=10240, available_nodes=['pve']))

    def test_negative_request_cannot_admit_an_overcommitted_node(self):
        self.assertEqual(self._suitable(-5 * GIB, -10 * 1024), [])


class TestNodeAvailability(_NodeSelectionEdgeCase):

    def test_all_nodes_offline_selects_nothing(self):
        proxmox = _proxmox_multi({
            'node-a': _status({'total': 92 * GIB, 'used': 1 * GIB,
                               'available': 90 * GIB}, uptime=0),
            'node-b': _status({'total': 92 * GIB, 'used': 1 * GIB,
                               'available': 90 * GIB}, uptime=0),
        })
        self.assertIsNone(
            _select(proxmox, available_nodes=['node-a', 'node-b']))

    def test_unreachable_node_is_offline_and_other_node_is_used(self):
        proxmox = _proxmox_multi({
            'node-b': _status({'total': 92 * GIB, 'used': 1 * GIB,
                               'available': 90 * GIB}),
        })
        proxmox.nodes.get.return_value = [{'node': 'node-a'},
                                          {'node': 'node-b'}]
        real = proxmox.nodes.side_effect

        def flaky(name):
            if name == 'node-a':
                raise ConnectionError('node down')
            return real(name)
        proxmox.nodes.side_effect = flaky
        self.assertEqual(
            _select(proxmox, available_nodes=['node-a', 'node-b']), 'node-b')

    def test_missing_cpu_count_counts_as_one_core(self):
        proxmox = _proxmox_with_memory({'total': 92 * GIB, 'used': 1 * GIB,
                                        'available': 90 * GIB})
        proxmox.nodes.return_value.status.get.return_value = {
            'cpu': 0.05, 'uptime': 1000,
            'memory': {'total': 92 * GIB, 'used': 1 * GIB,
                       'available': 90 * GIB}}
        self.assertEqual(get_node_resources(proxmox, 'pve')['cpu_count'], 1)
        self.assertIsNone(_select(proxmox, cores=2, available_nodes=['pve']))
        self.assertEqual(
            _select(proxmox, cores=1, available_nodes=['pve']), 'pve')

    def test_null_cpuinfo_counts_as_one_core(self):
        proxmox = _proxmox_with_memory({'total': 92 * GIB, 'used': 1 * GIB,
                                        'available': 90 * GIB})
        proxmox.nodes.return_value.status.get.return_value['cpuinfo'] = None
        self.assertEqual(get_node_resources(proxmox, 'pve')['cpu_count'], 1)

    def test_equal_nodes_tie_goes_to_first_listed_when_balancing_is_off(self):
        memory = {'total': 92 * GIB, 'used': 30 * GIB, 'available': 62 * GIB}
        for order in (['node-a', 'node-b'], ['node-b', 'node-a']):
            with self.subTest(order=order):
                proxmox = _proxmox_multi(
                    {name: _status(dict(memory)) for name in order})
                self.assertEqual(
                    _select(proxmox, config=LB_OFF, available_nodes=order),
                    order[0])

    def test_equal_nodes_both_reachable_when_balancing_is_on(self):
        memory = {'total': 92 * GIB, 'used': 30 * GIB, 'available': 62 * GIB}
        proxmox = _proxmox_multi({
            'node-a': _status(dict(memory)), 'node-b': _status(dict(memory))})
        picked = {
            _select(proxmox, available_nodes=['node-a', 'node-b'])
            for _ in range(200)}
        self.assertEqual(picked, {'node-a', 'node-b'})


class TestCachedResources(_NodeSelectionEdgeCase):

    @staticmethod
    def _cached(**overrides):
        node = {'node': 'pve', 'cpu_usage': 5.0, 'memory_usage': 40.0,
                'memory_available': 40 * GIB, 'memory_free': 40 * GIB,
                'memory_linux_free': 1 * GIB, 'memory_total': 92 * GIB,
                'cpu_count': 32, 'online': True}
        node.update(overrides)
        return [node]

    def test_cached_snapshot_is_used_without_re_reading_node_status(self):
        proxmox = _proxmox_with_memory({'total': 1, 'used': 1, 'available': 0})
        self.assertEqual(
            _select(proxmox, cached_resources=self._cached(),
                    available_nodes=['pve']), 'pve')
        proxmox.nodes.return_value.status.get.assert_not_called()

    def test_selection_does_not_mutate_the_cached_snapshot(self):
        cached = self._cached()
        before = [dict(node) for node in cached]
        _select(MagicMock(), cached_resources=cached, available_nodes=['pve'])
        self.assertEqual(cached, before)

    def test_reservations_are_applied_fresh_to_a_stale_cached_snapshot(self):
        # The cache says 40 GiB available forever; the reservation ledger is
        # live. Each 16 GiB request needs 17.6 GiB, so the third must see the
        # first two reservations (40 - 32 = 8 GiB left).
        cached = self._cached()
        picks = [
            _select(MagicMock(), mem_mb=16 * 1024, cores=1,
                    cached_resources=cached, available_nodes=['pve'],
                    reservation_id=f'r{i}')
            for i in range(3)]
        self.assertEqual(picks, ['pve', 'pve', None])

    def test_cached_legacy_memory_free_only_snapshot_still_works(self):
        cached = self._cached()
        del cached[0]['memory_available']
        self.assertEqual(
            _select(MagicMock(), cached_resources=cached,
                    available_nodes=['pve']), 'pve')

    def test_cached_offline_node_stays_excluded(self):
        self.assertIsNone(
            _select(MagicMock(), cached_resources=self._cached(online=False),
                    available_nodes=['pve']))


if __name__ == '__main__':
    unittest.main()
