import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.models import VMRecord, db
from app import proxmox_service
from app.routes.vm_creation import DeployFailed, deploy_bundle


GIB = 1024 ** 3


class _Method:
    def __init__(self, fn):
        self._fn = fn

    def get(self, **kwargs):
        return self._fn(**kwargs)

    def post(self, **kwargs):
        return self._fn(**kwargs)

    def put(self, **kwargs):
        return self._fn(**kwargs)


class _TaskEndpoint:
    def __init__(self):
        self.status = _Method(
            lambda **kwargs: {'status': 'stopped', 'exitstatus': 'OK'})


class _VmStatus:
    def __init__(self):
        self.current = _Method(lambda **kwargs: {'status': 'running'})
        self.start = _Method(lambda **kwargs: 'UPID:start')
        self.stop = _Method(lambda **kwargs: 'UPID:stop')


class _QemuEndpoint:
    def __init__(self, cluster, vmid):
        self._cluster = cluster
        self._vmid = vmid
        self.clone = _Method(self._clone)
        self.config = _Method(lambda **kwargs: None)
        self.status = _VmStatus()

    def _clone(self, *, newid, name, full, storage=None):
        return self._cluster.clone(newid, name)

    def delete(self):
        self._cluster.delete(self._vmid)
        return 'UPID:delete'


class _QemuCollection:
    def __init__(self, cluster):
        self._cluster = cluster

    def __call__(self, vmid):
        return _QemuEndpoint(self._cluster, vmid)

    def get(self):
        return self._cluster.inventory()


class _LxcCollection:
    def __call__(self, vmid):
        raise AssertionError('test deploy only creates qemu VMs')

    def get(self):
        return []


class _NodeEndpoint:
    def __init__(self, cluster):
        self.qemu = _QemuCollection(cluster)
        self.lxc = _LxcCollection()
        self.status = _Method(self._status)

    @staticmethod
    def _status(**kwargs):
        # Keep every request between allocation and clone long enough for all
        # concurrent students to reach the old allocator race window.
        time.sleep(0.05)
        return {
            'cpu': 0.01,
            'uptime': 100,
            'cpuinfo': {'cpus': 32},
            'memory': {
                'total': 64 * GIB,
                'used': 4 * GIB,
                'free': 60 * GIB,
                'available': 60 * GIB,
            },
        }

    def tasks(self, upid):
        return _TaskEndpoint()


class _Nodes:
    def __init__(self, cluster):
        self._cluster = cluster

    def get(self):
        return [{'node': 'pve'}]

    def __call__(self, node):
        if node != 'pve':
            raise RuntimeError(f'node {node} is offline')
        return _NodeEndpoint(self._cluster)


class _ClusterResources:
    def __init__(self, cluster):
        self._cluster = cluster

    def get(self, *, type):
        if type != 'vm':
            raise AssertionError(type)
        return [dict(row, node='pve', type='qemu', status='running', mem=0)
                for row in self._cluster.inventory()]


class _ClusterEndpoint:
    def __init__(self, cluster):
        self.resources = _ClusterResources(cluster)


class FakeProxmox:
    """Thread-safe fake at the proxmoxer.ProxmoxAPI boundary."""

    def __init__(self):
        self._lock = threading.Lock()
        self._vms = {9706: 'windows-workstation-template'}
        self.clone_attempts = []
        self.nodes = _Nodes(self)
        self.cluster = _ClusterEndpoint(self)

    def inventory(self):
        with self._lock:
            return [{'vmid': vmid, 'name': name}
                    for vmid, name in self._vms.items()]

    def clone(self, vmid, name):
        self.clone_attempts.append(vmid)
        with self._lock:
            if vmid in self._vms:
                raise RuntimeError(
                    f'500 Internal Server Error: VM {vmid} already exists')
            self._vms[vmid] = name
        return f'UPID:clone:{vmid}'

    def delete(self, vmid):
        with self._lock:
            self._vms.pop(vmid, None)


class VMIDAllocatorTestCase(unittest.TestCase):
    def setUp(self):
        clear = getattr(proxmox_service, '_clear_vmid_reservations', None)
        if clear:
            clear()

    def tearDown(self):
        clear = getattr(proxmox_service, '_clear_vmid_reservations', None)
        if clear:
            clear()

    def test_released_top_vmid_is_not_reallocated_in_process(self):
        proxmox = FakeProxmox()

        first = proxmox_service.get_next_available_vmid(
            proxmox, count=3, reservation_id='bundle-a')
        second = proxmox_service.get_next_available_vmid(
            proxmox, count=2, reservation_id='bundle-b')
        proxmox_service.release_vmid_reservation('bundle-a')
        proxmox_service.release_vmid_reservation('bundle-b')
        next_block = proxmox_service.get_next_available_vmid(
            proxmox, count=3, reservation_id='bundle-c')

        self.assertEqual((first, second), (9707, 9710))
        self.assertEqual(next_block, 9712)

    def test_cluster_inventory_avoids_offline_per_node_listing(self):
        proxmox = FakeProxmox()

        class NodesWithOfflineListing(_Nodes):
            def get(self):
                raise RuntimeError('one node is offline')

        proxmox.nodes = NodesWithOfflineListing(proxmox)

        vmid = proxmox_service.get_next_available_vmid(
            proxmox, count=1, reservation_id='offline-node')

        self.assertEqual(vmid, 9707)

    def test_expired_reservation_does_not_lower_process_high_water_mark(self):
        proxmox = FakeProxmox()
        with patch('app.proxmox_service.time.monotonic',
                   return_value=10.0):
            first = proxmox_service.get_next_available_vmid(
                proxmox, count=1, reservation_id='leaked')
        with patch(
            'app.proxmox_service.time.monotonic',
            return_value=10.0 + proxmox_service.RESERVATION_TTL_SECONDS + 1,
        ):
            replacement = proxmox_service.get_next_available_vmid(
                proxmox, count=1, reservation_id='replacement')

        self.assertEqual(first, 9707)
        self.assertEqual(replacement, 9708)


class ConcurrentStudentDeployTestCase(unittest.TestCase):
    STUDENT_COUNT = 5

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        database_path = Path(self.tempdir.name) / 'concurrent.sqlite'
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=f'sqlite:///{database_path}',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.proxmox = FakeProxmox()
        self.template = {
            'name': 'windows',
            'type': 'qemu',
            'memory': 1024,
            'cores': 1,
            'storage': 20,
            'entry_point': True,
            'storage_locations': {'pve': 'pool'},
            'template_locations': {'pve': 9706},
        }

    def tearDown(self):
        clear = getattr(proxmox_service, '_clear_vmid_reservations', None)
        if clear:
            clear()
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.tempdir.cleanup()

    def _deploy(self, templates, owning_identifier='student'):
        return deploy_bundle(
            deployment_group_name='concurrent-lab',
            templates=templates,
            owning_identifier=owning_identifier,
            entry_vlan=101,
            internal_vlan=201,
            uptime_minutes=60,
            user_id=None,
            load_balancing_config={
                'enabled': True,
                'cpu_threshold': 80.0,
                'memory_threshold': 85.0,
            },
        )

    def test_multi_vm_bundle_uses_contiguous_block_and_releases_reservation(self):
        templates = [
            dict(self.template, name=f'windows-{index}')
            for index in range(3)
        ]

        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox):
            records = self._deploy(templates)

        self.assertEqual([record.vmid for record in records],
                         [9707, 9708, 9709])
        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_duplicate_clone_retries_once_with_fresh_reservation(self):
        original_clone = self.proxmox.clone
        rejected = False

        def reject_first_vmid(vmid, name):
            nonlocal rejected
            if not rejected:
                rejected = True
                self.proxmox.clone_attempts.append(vmid)
                # A VMID stays unique-indexed after its guest is destroyed.
                # Simulate that row appearing while this clone is in flight.
                now = datetime.now()
                with self.app.app_context():
                    db.session.add(VMRecord(
                        vmid=9708,
                        vm_name='destroyed-concurrent-vm',
                        vm_type='qemu',
                        node='pve',
                        template_name='other/lab',
                        student_name='other-student',
                        created_at=now,
                        expires_at=now + timedelta(hours=1),
                        destroyed=True,
                        destroyed_at=now,
                    ))
                    db.session.commit()
                raise RuntimeError(
                    f'500 Internal Server Error: VM {vmid} already exists')
            return original_clone(vmid, name)

        self.proxmox.clone = reject_first_vmid
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox):
            records = self._deploy([self.template])

        self.assertEqual([record.vmid for record in records], [9709])
        self.assertEqual(self.proxmox.clone_attempts, [9707, 9709])
        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_duplicate_clone_is_retried_only_once(self):
        def duplicate(vmid, name):
            self.proxmox.clone_attempts.append(vmid)
            raise RuntimeError(
                f'500 Internal Server Error: VM {vmid} already exists')

        self.proxmox.clone = duplicate
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox), \
             patch(
                 'app.routes.vm_creation._cleanup_with_sweep',
                 return_value=set(),
             ) as cleanup:
            with self.assertRaises(DeployFailed):
                self._deploy([self.template])

        failed_results = cleanup.call_args.args[0]
        self.assertNotIn('vmid', failed_results[0])
        self.assertEqual(VMRecord.query.count(), 0)
        self.assertEqual(self.proxmox.clone_attempts, [9707, 9708])
        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_failed_bundle_releases_vmid_reservation_after_cleanup(self):
        def fail_clone(vmid, name):
            self.proxmox.clone_attempts.append(vmid)
            raise RuntimeError('storage unavailable')

        self.proxmox.clone = fail_clone
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox):
            with self.assertRaises(DeployFailed):
                self._deploy([self.template])

        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_node_enumeration_failure_releases_vmid_reservation(self):
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox), \
             patch.object(
                 self.proxmox.nodes,
                 'get',
                 side_effect=RuntimeError('node inventory unavailable'),
             ):
            with self.assertRaisesRegex(
                    RuntimeError, 'node inventory unavailable'):
                self._deploy([self.template])

        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_executor_setup_failure_releases_vmid_reservation(self):
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox), \
             patch(
                 'app.routes.vm_creation.ThreadPoolExecutor',
                 side_effect=RuntimeError('executor unavailable'),
             ):
            with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
                self._deploy([self.template])

        self.assertEqual(proxmox_service._vmid_reservations, {})

    def test_five_students_deploy_concurrently_with_distinct_vmids(self):
        barrier = threading.Barrier(self.STUDENT_COUNT)
        results = {}

        def deploy(index):
            student = f'student-{index}'
            barrier.wait(timeout=5)
            with self.app.app_context():
                try:
                    records = deploy_bundle(
                        deployment_group_name='concurrent-lab',
                        templates=[self.template],
                        owning_identifier=student,
                        entry_vlan=101 + index,
                        internal_vlan=201 + index,
                        uptime_minutes=60,
                        user_id=None,
                        load_balancing_config={
                            'enabled': True,
                            'cpu_threshold': 80.0,
                            'memory_threshold': 85.0,
                        },
                    )
                    results[student] = {
                        'success': True,
                        'vmids': [record.vmid for record in records],
                    }
                except Exception as error:
                    results[student] = {
                        'success': False,
                        'error': str(error),
                    }

        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox):
            workers = [threading.Thread(target=deploy, args=(index,))
                       for index in range(self.STUDENT_COUNT)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertEqual(len(results), self.STUDENT_COUNT)
        failures = {student: result for student, result in results.items()
                    if not result['success']}
        self.assertEqual(failures, {})
        vmids = [result['vmids'][0] for result in results.values()]
        self.assertEqual(len(vmids), len(set(vmids)))
        self.assertEqual(VMRecord.query.filter_by(destroyed=False).count(),
                         self.STUDENT_COUNT)


if __name__ == '__main__':
    unittest.main()
