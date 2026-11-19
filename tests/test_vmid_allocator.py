import importlib
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from app.models import VMIDReservation, VMRecord, db
from app import proxmox_service
from app.routes.vm_creation import DeployFailed, deploy_bundle
from tests.test_live_contract_guards import FakeProxmox as ContractFakeProxmox

config = importlib.import_module('app.config')


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
        with patch('app.proxmox_service.time.time',
                   return_value=10.0):
            first = proxmox_service.get_next_available_vmid(
                proxmox, count=1, reservation_id='leaked')
        with patch(
            'app.proxmox_service.time.time',
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
        self.assertEqual(VMIDReservation.query.count(), 0)

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
        self.assertEqual(VMIDReservation.query.count(), 0)

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
        self.assertEqual(VMIDReservation.query.count(), 0)

    def test_failed_bundle_releases_vmid_reservation_after_cleanup(self):
        def fail_clone(vmid, name):
            self.proxmox.clone_attempts.append(vmid)
            raise RuntimeError('storage unavailable')

        self.proxmox.clone = fail_clone
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox):
            with self.assertRaises(DeployFailed):
                self._deploy([self.template])

        self.assertEqual(VMIDReservation.query.count(), 0)

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

        self.assertEqual(VMIDReservation.query.count(), 0)

    def test_executor_setup_failure_releases_vmid_reservation(self):
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=self.proxmox), \
             patch(
                 'app.routes.vm_creation.ThreadPoolExecutor',
                 side_effect=RuntimeError('executor unavailable'),
             ):
            with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
                self._deploy([self.template])

        self.assertEqual(VMIDReservation.query.count(), 0)

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


# Catches a cloud-only portal trying to contact a disabled Proxmox cluster.
def test_allocator_uses_database_and_reservations_without_proxmox(app):
    with app.app_context():
        db.session.add(VMRecord(
            vmid=9000, vm_name='student-test-cloud', vm_type='aws',
            node='zone-placeholder', template_name='Cloud Lab/vm', student_name='test',
            expires_at=datetime.now() + timedelta(hours=1), backend='aws'))
        db.session.commit()
        first = proxmox_service.get_next_available_vmid(
            None, count=2, reservation_id='cloud-bundle')
        second = proxmox_service.get_next_available_vmid(
            None, count=1, reservation_id='cloud-bundle-2')
    assert (first, second) == (9001, 9003)


# Catches a mixed portal colliding with a manually allocated Proxmox guest.
def test_allocator_still_observes_proxmox_inventory_when_available(app):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(9500, 'manual-guest')
    with app.app_context():
        base = proxmox_service.get_next_available_vmid(
            proxmox, count=1, reservation_id='mixed-bundle')
    assert base == 9501


# Fixture values are neutral (4000-4099), never a site's real range.
def _get(proxmox, count, rid):
    return proxmox_service.get_next_available_vmid(proxmox, count=count, reservation_id=rid)


def _record(vmid):
    return VMRecord(vmid=vmid, vm_name=f'student-x-vm{vmid}', vm_type='qemu', node='n',
                    template_name='Lab/vm', student_name='x',
                    expires_at=datetime.now() + timedelta(hours=1))


def _reservation(vmid, rid):
    return VMIDReservation(vmid=vmid, reservation_id=rid, expires_at=time.time() + 600)


# Catches a staging portal following production's VMIDs upward.
def test_range_ignores_cluster_guests_above_it(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(9500, 'production-guest')
    proxmox.add_guest(4004, 'in-range-guest')
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4099))
    with app.app_context():
        assert (_get(proxmox, 2, 'r1'), _get(proxmox, 1, 'r2')) == (4005, 4007)


def test_range_starts_at_its_minimum_whatever_the_database_holds(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4099))
    with app.app_context():
        db.session.add(_record(9000))
        db.session.commit()
        assert _get(ContractFakeProxmox(), 1, 'r') == 4000


# Catches the record maximum being taken whole instead of clipped to the range.
def test_range_advances_from_the_highest_in_range_record(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4099))
    with app.app_context():
        db.session.add_all([_record(9000), _record(4010)])
        db.session.commit()
        assert _get(ContractFakeProxmox(), 1, 'r') == 4011


# Catches the reservation maximum being taken whole instead of clipped to the range.
def test_range_advances_from_the_highest_in_range_reservation(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4099))
    with app.app_context():
        db.session.add_all([_reservation(9100, 'prod'), _reservation(4020, 'staging')])
        db.session.commit()
        assert _get(ContractFakeProxmox(), 1, 'r') == 4021


# Catches a range silently spilling past its maximum.
def test_range_refuses_when_exhausted_and_reserves_nothing(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(4001, 'in-range-guest')
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4002))
    with app.app_context():
        with pytest.raises(proxmox_service.VMIDRangeExhausted):
            _get(proxmox, 2, 'r')
        assert VMIDReservation.query.count() == 0


# Catches the exhaustion check running after the insert when reservations already exist.
def test_range_exhaustion_leaves_existing_reservations_untouched(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4002))
    with app.app_context():
        db.session.add_all([_reservation(9100, 'prod'), _reservation(4001, 'staging')])
        db.session.commit()
        with pytest.raises(proxmox_service.VMIDRangeExhausted):
            _get(ContractFakeProxmox(), 2, 'r')
        assert {r.vmid for r in VMIDReservation.query} == {9100, 4001}


# Catches range mode lowering the high-water mark an unranged portal relies on.
def test_range_never_lowers_the_stored_high_water_mark(app, monkeypatch):
    proxmox = ContractFakeProxmox()
    proxmox.add_guest(9600, 'guest')
    with app.app_context():
        assert _get(proxmox, 1, 'a') == 9601
        proxmox_service.release_vmid_reservation('a')
        monkeypatch.setattr(config, 'VMID_RANGE', (4000, 4099))
        assert _get(ContractFakeProxmox(), 1, 'b') == 4000
        proxmox_service.release_vmid_reservation('b')
        monkeypatch.setattr(config, 'VMID_RANGE', None)
        assert _get(ContractFakeProxmox(), 1, 'c') == 9602


def test_an_invalid_range_refuses_to_allocate(app, monkeypatch):
    monkeypatch.setattr(config, 'VMID_RANGE_ERROR', 'VMID_RANGE must be <min>-<max>')
    with app.app_context(), pytest.raises(ValueError):
        _get(ContractFakeProxmox(), 1, 'r')


if __name__ == '__main__':
    unittest.main()
