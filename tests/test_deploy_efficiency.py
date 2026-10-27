"""Tests for the two classroom-scale deployment-path fixes.

D3 — the ZFS clone limit has to be per NODE, across every request. It used
to be rebuilt inside each one, so it only ever limited a single deploy.

D2 — `/list` used to make two Proxmox round trips per VM. At 25 groups of
7 that is ~1,400 calls when a class refreshes together, and every one of
them is a chance for the transient error that used to delete a VMRecord.
"""

import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import app.proxmox_service as proxmox_service
import app.routes.utils as route_utils
from app import create_app
from app.models import db, User, VMRecord
from app.routes.vm_creation import (
    MAX_CONCURRENT_CLONES, get_node_clone_semaphores,
)


class CloneSemaphoreTests(unittest.TestCase):
    """D3: one allowance per node, shared by every concurrent deploy."""

    def test_same_node_yields_the_same_semaphore_across_calls(self):
        # Two deploys running at once must contend with each other. When
        # each built its own dict, 25 simultaneous deploys meant ~25x the
        # intended clone concurrency and clones timing out at 300s.
        first = get_node_clone_semaphores(['pve', 'pve2'])
        second = get_node_clone_semaphores(['pve', 'pve2'])
        self.assertIs(first['pve'], second['pve'])
        self.assertIs(first['pve2'], second['pve2'])

    def test_different_nodes_do_not_share_an_allowance(self):
        sems = get_node_clone_semaphores(['pve', 'pve2'])
        self.assertIsNot(sems['pve'], sems['pve2'])

    def test_a_node_seen_later_still_gets_an_allowance(self):
        sems = get_node_clone_semaphores(['pve'])
        self.assertIn('pve', sems)
        later = get_node_clone_semaphores(['pve', 'pve3'])
        self.assertIs(later['pve'], sems['pve'])
        self.assertIn('pve3', later)

    def test_accepts_a_generator_of_node_names(self):
        # Both call sites pass `(n['node'] for n in proxmox.nodes.get())`.
        # Iterating that twice internally returned an EMPTY mapping, which
        # makes create_single_vm_task take its "no semaphore for this
        # node" fallback and clone with no limit at all — the opposite of
        # what this function is for.
        sems = get_node_clone_semaphores(n for n in ['gen-a', 'gen-b'])
        self.assertEqual(set(sems), {'gen-a', 'gen-b'})

    def test_concurrency_is_capped_across_simulated_deploys(self):
        sem = get_node_clone_semaphores(['cap-node'])['cap-node']
        in_flight = 0
        peak = 0
        guard = threading.Lock()
        release = threading.Event()

        def clone():
            nonlocal in_flight, peak
            with sem:
                with guard:
                    in_flight += 1
                    peak = max(peak, in_flight)
                release.wait(2)
                with guard:
                    in_flight -= 1

        # Twice the cap, as if two requests each tried to run the limit.
        workers = [threading.Thread(target=clone)
                   for _ in range(MAX_CONCURRENT_CLONES * 2)]
        for w in workers:
            w.start()
        try:
            # Give the first wave time to pile up before letting go.
            threading.Event().wait(0.2)
            self.assertLessEqual(
                peak, MAX_CONCURRENT_CLONES,
                'more clones ran at once than the per-node ZFS limit allows')
        finally:
            release.set()
            for w in workers:
                w.join(3)


class ClusterIndexTests(unittest.TestCase):
    def setUp(self):
        proxmox_service._vm_index_cache = None
        proxmox_service._vm_index_fetched_at = 0.0

    tearDown = setUp

    def _proxmox(self, rows):
        p = MagicMock()
        p.cluster.resources.get.return_value = rows
        return p

    def test_index_is_keyed_by_int_vmid(self):
        p = self._proxmox([{'vmid': '101', 'name': 'a', 'node': 'pve',
                            'type': 'qemu', 'status': 'running'}])
        index = proxmox_service.get_cluster_vm_index(p)
        self.assertIn(101, index)
        self.assertEqual(index[101]['node'], 'pve')

    def test_repeat_calls_within_the_ttl_hit_the_api_once(self):
        p = self._proxmox([])
        proxmox_service.get_cluster_vm_index(p)
        proxmox_service.get_cluster_vm_index(p)
        proxmox_service.get_cluster_vm_index(p)
        self.assertEqual(p.cluster.resources.get.call_count, 1)

    def test_reset_forces_a_refetch(self):
        # vmids get reused, so a destroy must not leave a stale entry that
        # makes the next VM look present.
        p = self._proxmox([])
        proxmox_service.get_cluster_vm_index(p)
        proxmox_service.reset_cluster_vm_index()
        proxmox_service.get_cluster_vm_index(p)
        self.assertEqual(p.cluster.resources.get.call_count, 2)

    def test_stale_fallback_is_refused_when_the_caller_forbids_it(self):
        p = self._proxmox([{'vmid': 1, 'status': 'running'}])
        proxmox_service.get_cluster_vm_index(p)
        p.cluster.resources.get.side_effect = Exception('cluster down')
        # Display callers tolerate stale data...
        self.assertEqual(
            proxmox_service.get_cluster_vm_index(p, max_age=0),
            {1: {'vmid': 1, 'name': None, 'node': None,
                 'type': None, 'status': 'running'}})
        # ...the delete decision must not.
        with self.assertRaises(Exception):
            proxmox_service.get_cluster_vm_index(
                p, max_age=0, allow_stale=False)


class ListCallCountTests(unittest.TestCase):
    """D2: a student's /list must not scale Proxmox calls with VM count."""

    def setUp(self):
        proxmox_service._vm_index_cache = None
        proxmox_service._vm_index_fetched_at = 0.0
        route_utils._IP_CACHE.clear()
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.user = User(login_id='eff-user',
                         password_hash=generate_password_hash('p'))
        db.session.add(self.user)
        db.session.commit()
        for i in range(7):
            db.session.add(VMRecord(
                vmid=800 + i, vm_name=f'lab-{i}', vm_type='qemu', node='pve',
                template_name='grp/lab', student_name='eff-user',
                created_by_user_id=self.user.id,
                expires_at=datetime.now() + timedelta(hours=3)))
        db.session.commit()
        with self.client.session_transaction() as s:
            s['user_id'] = self.user.id
            s['session_version'] = self.user.session_version

    def tearDown(self):
        route_utils._IP_CACHE.clear()
        proxmox_service._vm_index_cache = None
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _proxmox(self):
        """A mock that counts per-VM config reads and guest-agent calls."""
        counts = {'config': 0, 'agent': 0}
        p = MagicMock()
        p.cluster.resources.get.return_value = [
            {'vmid': 800 + i, 'name': f'lab-{i}', 'node': 'pve',
             'type': 'qemu', 'status': 'running'} for i in range(7)]

        def qemu(vmid):
            handle = MagicMock()
            handle.config.get.side_effect = lambda: counts.__setitem__(
                'config', counts['config'] + 1)

            def agent(_cmd):
                counts['agent'] += 1
                res = MagicMock()
                res.get.return_value = {'result': [
                    {'name': 'eth0', 'ip-addresses': [
                        {'ip-address': '10.0.0.5', 'ip-address-type': 'ipv4'}]}]}
                return res

            handle.agent.side_effect = agent
            return handle

        p.nodes.return_value.qemu.side_effect = qemu
        return p, counts

    def _list(self, proxmox):
        with patch('app.routes.vm_listing.get_proxmox_connection',
                   return_value=proxmox), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value={'deployment_groups': []}):
            return self.client.get('/list')

    def test_seven_vms_cost_one_cluster_call_and_no_per_vm_config_reads(self):
        proxmox, counts = self._proxmox()
        r = self._list(proxmox)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(proxmox.cluster.resources.get.call_count, 1)
        self.assertEqual(
            counts['config'], 0,
            'the cluster inventory already proved these VMs exist; a '
            'per-VM config read is 7 avoidable round trips')

    def test_a_second_render_reuses_the_cached_ips(self):
        # The guest-agent call is the remaining per-VM cost. It must not
        # repeat on every refresh.
        proxmox, counts = self._proxmox()
        self._list(proxmox)
        first = counts['agent']
        self.assertEqual(first, 7)
        self._list(proxmox)
        self.assertEqual(counts['agent'], first,
                         'IP lookups must come from cache on a refresh')

    def test_all_seven_vms_are_still_listed_with_their_ips(self):
        # The call reduction must not cost correctness.
        proxmox, _ = self._proxmox()
        body = self._list(proxmox).get_json()
        vms = [vm for s in body['students']
               for g in s['deployment_groups'] for vm in g['vms']]
        self.assertEqual(len(vms), 7)
        self.assertTrue(all(vm['ip_address'] == '10.0.0.5' for vm in vms))
        self.assertEqual(VMRecord.query.count(), 7)


class IpCacheTests(unittest.TestCase):
    def setUp(self):
        route_utils._IP_CACHE.clear()

    tearDown = setUp

    def test_a_negative_entry_expires_while_a_good_one_survives(self):
        # The load-bearing part is the sentinel comparison inside
        # _ip_cache_get. Comparing the two constants would still pass if
        # that line were deleted, so age two entries identically and check
        # that only the 'N/A' one is discarded. A booting VM must be
        # re-asked quickly; a known address must not be.
        import time as _time
        aged = _time.monotonic() - route_utils._IP_CACHE_NEGATIVE_TTL - 1
        route_utils._IP_CACHE[('pve', 10)] = (aged, ['N/A'])
        route_utils._IP_CACHE[('pve', 11)] = (aged, ['10.0.0.7'])
        self.assertIsNone(route_utils._ip_cache_get(('pve', 10)))
        self.assertEqual(route_utils._ip_cache_get(('pve', 11)), ['10.0.0.7'])

    def test_cache_is_bounded(self):
        for i in range(route_utils._IP_CACHE_MAX + 50):
            route_utils._ip_cache_put(('pve', i), ['10.0.0.1'])
        self.assertLessEqual(len(route_utils._IP_CACHE),
                             route_utils._IP_CACHE_MAX)
        # Oldest evicted first, newest retained.
        self.assertIsNone(route_utils._ip_cache_get(('pve', 0)))
        self.assertIsNotNone(
            route_utils._ip_cache_get(('pve', route_utils._IP_CACHE_MAX + 49)))

    def test_invalidate_drops_one_entry(self):
        route_utils._ip_cache_put(('pve', 900), ['10.0.0.9'])
        route_utils.invalidate_vm_ip_cache('pve', 900)
        self.assertIsNone(route_utils._ip_cache_get(('pve', 900)))

    def test_expired_entry_is_refetched(self):
        import time as _time
        route_utils._IP_CACHE[('pve', 901)] = (
            _time.monotonic() - route_utils._IP_CACHE_TTL - 1, ['10.0.0.1'])
        self.assertIsNone(route_utils._ip_cache_get(('pve', 901)))


if __name__ == '__main__':
    unittest.main()
