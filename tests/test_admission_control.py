import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from flask import Flask
from sqlalchemy import event

from app.models import VMRecord, db
from app.routes.vm_creation import (
    DeployFailed, create_single_vm_task, deploy_bundle)
from app.routes import vm_creation as vm_creation_module

from app.proxmox_service import (
    RESERVATION_TTL_SECONDS,
    _apply_memory_admission,
    _clear_node_reservations,
    _clear_vmid_reservations,
    get_next_available_vmid,
    get_node_resources,
    release_node_reservation,
    release_vmid_reservation,
    renew_node_reservation,
    renew_vmid_reservation,
    select_best_node,
)

GIB = 1024 ** 3
LB_ON = {'enabled': True, 'cpu_threshold': 80.0, 'memory_threshold': 85.0}




def _proxmox(*, available_gib, guest_rows=()):
    proxmox = MagicMock()
    proxmox.nodes.get.return_value = [{'node': 'pve'}]
    proxmox.nodes.return_value.status.get.return_value = {
        'cpu': 0.05,
        'uptime': 1000,
        'cpuinfo': {'cpus': 32},
        'memory': {
            'total': 16 * GIB,
            'used': (16 - available_gib) * GIB,
            'free': 1 * GIB,
            'available': available_gib * GIB,
        },
    }
    proxmox.cluster.resources.get.return_value = list(guest_rows)
    return proxmox


class AdmissionMemoryMathTestCase(unittest.TestCase):
    def setUp(self):
        _clear_node_reservations()

    def tearDown(self):
        _clear_node_reservations()

    def test_effective_available_subtracts_only_unrealized_live_memory(self):
        nodes = [{
            'node': 'pve',
            'memory_available': 10 * GIB,
            'memory_free': 10 * GIB,
        }]
        records = [
            SimpleNamespace(vmid=101, node='pve', memory=4096, destroyed=False),
            SimpleNamespace(vmid=102, node='pve', memory=2048, destroyed=False),
            SimpleNamespace(vmid=103, node='pve', memory=4096, destroyed=True),
        ]
        guests = [
            {'vmid': 101, 'node': 'pve', 'mem': 1 * GIB},
            # This guest has already ramped beyond its configured allocation.
            {'vmid': 102, 'node': 'pve', 'mem': 3 * GIB},
            {'vmid': 103, 'node': 'pve', 'mem': 0},
        ]

        result = _apply_memory_admission(nodes, records, guests, {'pve': 1 * GIB})

        self.assertEqual(result[0]['memory_committed'], 3 * GIB)
        self.assertEqual(result[0]['memory_reserved'], 1 * GIB)
        self.assertEqual(result[0]['memory_effective_available'], 6 * GIB)

    def test_selection_fetches_guest_inventory_once_for_any_vm_count(self):
        records = [
            SimpleNamespace(
                vmid=vmid, node='pve', memory=1024, destroyed=False)
            for vmid in range(100, 200)
        ]
        proxmox = _proxmox(
            available_gib=10,
            guest_rows=[{'vmid': vmid, 'node': 'pve', 'mem': GIB}
                        for vmid in range(100, 200)],
        )

        with patch(
            'app.proxmox_service._load_live_vm_records',
            return_value=records,
        ):
            selected = select_best_node(
                proxmox,
                required_memory_mb=1024,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
            )

        self.assertEqual(selected, 'pve')
        proxmox.cluster.resources.get.assert_called_once_with(type='vm')

    def test_node_resources_names_memavailable_without_breaking_legacy_key(self):
        proxmox = _proxmox(available_gib=10)

        resources = get_node_resources(proxmox, 'pve')

        self.assertIn('memory_available', resources)
        self.assertIn('memory_linux_free', resources)
        self.assertEqual(resources['memory_available'], 10 * GIB)
        self.assertEqual(resources['memory_free'], 10 * GIB)
        self.assertEqual(resources['memory_linux_free'], 1 * GIB)


class ReservationLedgerTestCase(unittest.TestCase):
    def setUp(self):
        _clear_node_reservations()

    def tearDown(self):
        _clear_node_reservations()

    def test_concurrent_selections_never_admit_more_memory_than_available(self):
        proxmox = _proxmox(available_gib=10)
        start = threading.Barrier(8)

        def select(index):
            start.wait()
            reservation_id = f'concurrent-{index}'
            node = select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id=reservation_id,
            )
            return reservation_id, node

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(select, range(8)))

        admitted = [(reservation_id, node) for reservation_id, node in results if node]
        self.assertEqual(len(admitted), 3)
        self.assertLessEqual(len(admitted) * 3 * GIB, 10 * GIB)

        # Both terminal outcomes use the same release operation: a successful
        # create hands capacity to its VMRecord, while a failed create simply
        # returns it to the pool.
        success_id, _ = admitted[0]
        failure_id, _ = admitted[1]
        self.assertTrue(release_node_reservation(success_id))
        self.assertTrue(release_node_reservation(failure_id))

        for suffix in ('after-success', 'after-failure'):
            self.assertEqual(
                select_best_node(
                    proxmox,
                    required_memory_mb=3072,
                    required_cores=1,
                    load_balancing_config=LB_ON,
                    available_nodes=['pve'],
                    reservation_id=suffix,
                ),
                'pve',
            )

    def test_expired_reservation_no_longer_consumes_capacity(self):
        proxmox = _proxmox(available_gib=4)

        with patch('app.proxmox_service.time.time', return_value=10.0):
            first = select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id='abandoned',
            )
        with patch(
            'app.proxmox_service.time.time',
            return_value=10.0 + RESERVATION_TTL_SECONDS + 1,
        ):
            replacement = select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id='replacement',
            )

        self.assertEqual(first, 'pve')
        self.assertEqual(replacement, 'pve')

    def test_renewal_keeps_an_active_reservation_past_its_original_ttl(self):
        proxmox = _proxmox(available_gib=4)

        with patch('app.proxmox_service.time.time', return_value=10.0):
            self.assertEqual(
                select_best_node(
                    proxmox,
                    required_memory_mb=3072,
                    required_cores=1,
                    load_balancing_config=LB_ON,
                    available_nodes=['pve'],
                    reservation_id='long-running',
                ),
                'pve',
            )
        with patch(
            'app.proxmox_service.time.time',
            return_value=10.0 + RESERVATION_TTL_SECONDS - 1,
        ):
            self.assertTrue(renew_node_reservation('long-running'))
        with patch(
            'app.proxmox_service.time.time',
            return_value=10.0 + RESERVATION_TTL_SECONDS + 1,
        ):
            contender = select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id='contender',
            )

        self.assertIsNone(contender)
        release_node_reservation('long-running')




class CreationReservationLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        _clear_node_reservations()
        _clear_vmid_reservations()
        # A file, not create_app(testing=True)'s in-memory database: that one
        # is a single connection shared by every thread, so the failed-deploy
        # heartbeat's rollback-on-return could discard the request thread's
        # uncommitted rows. Production pools give each thread its own
        # connection, and so does this.
        self.tempdir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=(
                f"sqlite:///{Path(self.tempdir.name) / 'admission.sqlite'}"),
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        _clear_node_reservations()
        _clear_vmid_reservations()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self.tempdir.cleanup()

    def _proxmox_for_create(self, clone_side_effect):
        proxmox = _proxmox(available_gib=4)
        proxmox.nodes.return_value.qemu.get.return_value = []
        proxmox.nodes.return_value.lxc.get.return_value = []
        proxmox.nodes.return_value.qemu.return_value.clone.post.side_effect = (
            clone_side_effect)
        proxmox.nodes.return_value.tasks.return_value.status.get.return_value = {
            'status': 'stopped',
            'exitstatus': 'OK',
        }
        return proxmox

    def test_waiting_for_clone_slot_renews_reservation(self):
        class DelayedSemaphore:
            def __init__(self):
                self.acquire_calls = 0

            def acquire(self, timeout=None):
                self.acquire_calls += 1
                return self.acquire_calls > 1

            def release(self):
                return None

        proxmox = self._proxmox_for_create(
            lambda **kwargs: 'UPID:clone')
        semaphore = DelayedSemaphore()
        template = {
            'name': 'windows',
            'type': 'qemu',
            'memory': 3072,
            'cores': 1,
            'storage': 20,
            'template_locations': {'pve': 9706},
        }

        vmid_reservation_id = 'queued-bundle'
        assigned_vmid = get_next_available_vmid(
            proxmox, count=1, reservation_id=vmid_reservation_id)
        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.renew_node_reservation',
                 create=True,
                 side_effect=renew_node_reservation,
             ) as renew, \
             patch(
                 'app.routes.vm_creation.renew_vmid_reservation',
                 side_effect=renew_vmid_reservation,
             ) as renew_vmid:
            result = create_single_vm_task(
                1, template, assigned_vmid, 'student', LB_ON, 101, 201,
                datetime.now() + timedelta(hours=1),
                {'pve': semaphore},
                flask_app=self.app,
                vmid_reservation_id=vmid_reservation_id,
            )

        self.assertTrue(result['success'])
        self.assertGreaterEqual(renew.call_count, 2)
        self.assertGreaterEqual(renew_vmid.call_count, 2)
        self.assertEqual(semaphore.acquire_calls, 2)
        release_node_reservation(result['_reservation_id'])
        release_vmid_reservation(vmid_reservation_id)

    def test_expired_reservation_does_not_leak_clone_slot(self):
        class TrackingSemaphore:
            def __init__(self):
                self.release_calls = 0

            def acquire(self, timeout=None):
                return True

            def release(self):
                self.release_calls += 1

        proxmox = self._proxmox_for_create(
            lambda **kwargs: 'UPID:clone')
        semaphore = TrackingSemaphore()
        template = {
            'name': 'windows',
            'type': 'qemu',
            'memory': 3072,
            'cores': 1,
            'storage': 20,
            'template_locations': {'pve': 9706},
        }

        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.renew_node_reservation',
                 return_value=False,
             ):
            result = create_single_vm_task(
                1, template, 100, 'student', LB_ON, 101, 201,
                datetime.now() + timedelta(hours=1),
                {'pve': semaphore},
                flask_app=self.app,
            )

        self.assertFalse(result['success'])
        self.assertEqual(semaphore.release_calls, 1)

    def _deploy_in_thread(self, proxmox, outcome):
        template = {
            'name': 'windows',
            'type': 'qemu',
            'memory': 3072,
            'cores': 1,
            'storage': 20,
            'template_locations': {'pve': 9706},
        }
        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox):
            with self.app.app_context():
                try:
                    outcome['records'] = deploy_bundle(
                        deployment_group_name='windows-lab',
                        templates=[template],
                        owning_identifier='student',
                        entry_vlan=101,
                        internal_vlan=201,
                        uptime_minutes=60,
                        user_id=None,
                        load_balancing_config=LB_ON,
                    )
                except Exception as error:
                    outcome['error'] = error

    def test_success_holds_reservation_until_record_is_committed_then_releases(self):
        clone_started = threading.Event()
        finish_clone = threading.Event()

        def blocking_clone(**kwargs):
            clone_started.set()
            finish_clone.wait(timeout=5)
            return 'UPID:clone'

        proxmox = self._proxmox_for_create(blocking_clone)
        outcome = {}
        release_saw_committed_record = []

        def checked_release(reservation_id):
            with self.app.app_context():
                release_saw_committed_record.append(
                    VMRecord.query.filter_by(destroyed=False).count() == 1)
            return release_node_reservation(reservation_id)

        worker = threading.Thread(
            target=self._deploy_in_thread, args=(proxmox, outcome))
        with patch(
            'app.routes.vm_creation.release_node_reservation',
            side_effect=checked_release,
        ):
            worker.start()
            self.assertTrue(clone_started.wait(timeout=5))

            contender = select_best_node(
                proxmox,
                required_memory_mb=3072,
                required_cores=1,
                load_balancing_config=LB_ON,
                available_nodes=['pve'],
                reservation_id='while-cloning',
            )
            release_node_reservation('while-cloning')
            finish_clone.set()
            worker.join(timeout=5)

        self.assertIsNone(contender)
        self.assertFalse(worker.is_alive())
        self.assertNotIn('error', outcome)
        self.assertEqual(len(outcome['records']), 1)
        self.assertEqual(release_saw_committed_record, [True])

        committed_vmid = VMRecord.query.one().vmid
        proxmox.cluster.resources.get.return_value = [{
            'vmid': committed_vmid,
            'node': 'pve',
            'mem': 3072 * 1024 * 1024,
        }]
        replacement = select_best_node(
            proxmox,
            required_memory_mb=3072,
            required_cores=1,
            load_balancing_config=LB_ON,
            available_nodes=['pve'],
            reservation_id='after-commit',
        )
        self.assertEqual(replacement, 'pve')
        release_node_reservation('after-commit')

    def test_failed_create_releases_reservation(self):
        def failed_clone(**kwargs):
            raise RuntimeError('clone failed')

        proxmox = self._proxmox_for_create(failed_clone)
        outcome = {}
        worker = threading.Thread(
            target=self._deploy_in_thread, args=(proxmox, outcome))
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome.get('error'), DeployFailed)
        replacement = select_best_node(
            proxmox,
            required_memory_mb=3072,
            required_cores=1,
            load_balancing_config=LB_ON,
            available_nodes=['pve'],
            reservation_id='after-failure',
        )
        self.assertEqual(replacement, 'pve')
        release_node_reservation('after-failure')

    def test_failed_bundle_tracks_survivor_before_releasing_capacity(self):
        proxmox = _proxmox(available_gib=4)
        templates = [
            {
                'name': f'windows-{index}',
                'type': 'qemu',
                'memory': 3072,
                'cores': 1,
                'storage': 20,
                'template_locations': {'pve': 9706},
            }
            for index in range(2)
        ]
        release_saw_record = []

        def create_result(index, template, vmid, *args, **kwargs):
            if index == 2:
                return {
                    'success': False,
                    'template_name': template['name'],
                    'error': 'clone failed',
                    'vm_index': index,
                    '_reservation_id': 'failed-reservation',
                }
            return {
                'success': True,
                'vm_index': index,
                'vmid': vmid,
                'vm_name': 'student-student-windows-0',
                'vm_display_name': template['name'],
                'vm_type': 'qemu',
                'node': 'pve',
                'memory': 3072,
                'cores': 1,
                'storage': 20,
                'is_entry_point': False,
                '_reservation_id': 'survivor-reservation',
            }

        def checked_release(reservation_id):
            release_saw_record.append(
                reservation_id,
            )
            if reservation_id == 'survivor-reservation':
                self.assertEqual(
                    VMRecord.query.filter_by(
                        vm_name='student-student-windows-0',
                        destroyed=False,
                    ).count(),
                    1,
                )
            return True

        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.create_single_vm_task',
                 side_effect=create_result,
             ), patch(
                 'app.routes.vm_creation._cleanup_with_sweep',
                 return_value=set(),
             ), patch(
                 'app.routes.vm_creation.release_node_reservation',
                 side_effect=checked_release,
             ):
            with self.assertRaises(DeployFailed):
                deploy_bundle(
                    deployment_group_name='windows-lab',
                    templates=templates,
                    owning_identifier='student',
                    entry_vlan=101,
                    internal_vlan=201,
                    uptime_minutes=60,
                    user_id=None,
                    load_balancing_config=LB_ON,
                )

        survivor = VMRecord.query.one()
        self.assertLessEqual(survivor.expires_at, datetime.now())
        self.assertTrue(
            survivor.template_name.startswith('failed-deploy-leftover/'))
        self.assertIn('survivor-reservation', release_saw_record)

        contender = select_best_node(
            proxmox,
            required_memory_mb=3072,
            required_cores=1,
            load_balancing_config=LB_ON,
            available_nodes=['pve'],
            reservation_id='after-partial-failure',
        )
        self.assertIsNone(contender)

    def test_cleanup_exception_still_tracks_created_guest_before_release(self):
        proxmox = _proxmox(available_gib=4)
        templates = [
            {
                'name': f'windows-{index}',
                'type': 'qemu',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'template_locations': {'pve': 9706},
            }
            for index in range(2)
        ]
        release_saw_record = []

        def create_result(index, template, vmid, *args, **kwargs):
            if index == 2:
                return {
                    'success': False,
                    'template_name': template['name'],
                    'error': 'clone failed',
                    'vm_index': index,
                    '_reservation_id': 'failed-reservation',
                }
            return {
                'success': True,
                'vm_index': index,
                'vmid': vmid,
                'vm_name': 'student-student-windows-0',
                'vm_display_name': template['name'],
                'vm_type': 'qemu',
                'node': 'pve',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'is_entry_point': False,
                '_reservation_id': 'survivor-reservation',
            }

        def checked_release(reservation_id):
            if reservation_id == 'survivor-reservation':
                release_saw_record.append(
                    VMRecord.query.filter_by(
                        vm_name='student-student-windows-0',
                        destroyed=False,
                    ).count() == 1)
            return True

        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.create_single_vm_task',
                 side_effect=create_result,
             ), patch(
                 'app.routes.vm_creation._cleanup_with_sweep',
                 side_effect=RuntimeError('cleanup connection failed'),
             ), patch(
                 'app.routes.vm_creation.release_node_reservation',
                 side_effect=checked_release,
             ):
            with self.assertRaises(DeployFailed):
                deploy_bundle(
                    deployment_group_name='windows-lab',
                    templates=templates,
                    owning_identifier='student',
                    entry_vlan=101,
                    internal_vlan=201,
                    uptime_minutes=60,
                    user_id=None,
                    load_balancing_config=LB_ON,
                )

        self.assertEqual(release_saw_record, [True])
        self.assertEqual(VMRecord.query.filter_by(destroyed=False).count(), 1)

    def test_partial_executor_submission_tracks_completed_guest(self):
        proxmox = _proxmox(available_gib=4)
        templates = [
            {
                'name': f'windows-{index}',
                'type': 'qemu',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'template_locations': {'pve': 9706},
            }
            for index in range(2)
        ]

        class PartiallyFailingExecutor:
            def __init__(self, max_workers):
                self.submit_count = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def submit(self, function, *args):
                self.submit_count += 1
                if self.submit_count == 2:
                    raise RuntimeError('executor rejected submission')
                future = Future()
                future.set_result(function(*args))
                return future

        def create_result(index, template, vmid, *args, **kwargs):
            return {
                'success': True,
                'vm_index': index,
                'vmid': vmid,
                'vm_name': f"student-student-{template['name']}",
                'vm_display_name': template['name'],
                'vm_type': 'qemu',
                'node': 'pve',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'is_entry_point': False,
                '_reservation_id': f'reservation-{index}',
            }

        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.ThreadPoolExecutor',
                 PartiallyFailingExecutor,
             ), patch(
                 'app.routes.vm_creation.create_single_vm_task',
                 side_effect=create_result,
             ), patch(
                 'app.routes.vm_creation._cleanup_with_sweep',
                 return_value=set(),
             ):
            with self.assertRaises(DeployFailed):
                deploy_bundle(
                    deployment_group_name='windows-lab',
                    templates=templates,
                    owning_identifier='student',
                    entry_vlan=101,
                    internal_vlan=201,
                    uptime_minutes=60,
                    user_id=None,
                    load_balancing_config=LB_ON,
                )

        survivor = VMRecord.query.one()
        self.assertEqual(survivor.vm_name, 'student-student-windows-0')
        self.assertLessEqual(survivor.expires_at, datetime.now())

    def test_heartbeat_connection_return_cannot_discard_survivor_insert(self):
        # Replays the interleaving that made the survivor tests flaky on a
        # single shared connection: the failed-deploy heartbeat commits a
        # lease renewal, the request thread INSERTs the survivor row, and the
        # heartbeat's rollback-on-return runs before the request commits.
        proxmox = _proxmox(available_gib=4)
        templates = [
            {
                'name': f'windows-{index}',
                'type': 'qemu',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'template_locations': {'pve': 9706},
            }
            for index in range(2)
        ]

        def create_result(index, template, vmid, *args, **kwargs):
            if index == 2:
                return {
                    'success': False,
                    'template_name': template['name'],
                    'error': 'clone failed',
                    'vm_index': index,
                    '_reservation_id': None,
                }
            return {
                'success': True,
                'vm_index': index,
                'vmid': vmid,
                'vm_name': 'student-student-windows-0',
                'vm_display_name': template['name'],
                'vm_type': 'qemu',
                'node': 'pve',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'is_entry_point': False,
                '_reservation_id': None,
            }

        heartbeat_returning = threading.Event()
        survivor_inserted = threading.Event()
        heartbeat_returned = threading.Event()

        def on_heartbeat():
            return (threading.current_thread().name
                    == 'failed-deploy-reservation-heartbeat')

        def is_survivor_insert(statement):
            return statement.lstrip().upper().startswith(
                'INSERT INTO VM_RECORDS')

        def pause_heartbeat_return(dbapi_connection, record, reset_state):
            if on_heartbeat() and not heartbeat_returning.is_set():
                heartbeat_returning.set()
                survivor_inserted.wait(timeout=5)

        def heartbeat_checked_in(dbapi_connection, record):
            if on_heartbeat() and heartbeat_returning.is_set():
                heartbeat_returned.set()

        def insert_after_heartbeat_commit(
                conn, cursor, statement, parameters, context, executemany):
            if is_survivor_insert(statement) and not on_heartbeat():
                heartbeat_returning.wait(timeout=5)

        def commit_after_heartbeat_return(
                conn, cursor, statement, parameters, context, executemany):
            if is_survivor_insert(statement) and not on_heartbeat():
                survivor_inserted.set()
                heartbeat_returned.wait(timeout=5)

        listeners = [
            (db.engine.pool, 'reset', pause_heartbeat_return),
            (db.engine.pool, 'checkin', heartbeat_checked_in),
            (db.engine, 'before_cursor_execute',
             insert_after_heartbeat_commit),
            (db.engine, 'after_cursor_execute',
             commit_after_heartbeat_return),
        ]
        for target, name, listener in listeners:
            event.listen(target, name, listener)
        try:
            with patch('app.proxmox_service.ProxmoxAPI',
                       return_value=proxmox), \
                 patch(
                     'app.routes.vm_creation.create_single_vm_task',
                     side_effect=create_result,
                 ), patch(
                     'app.routes.vm_creation._cleanup_with_sweep',
                     return_value=set(),
                 ):
                with self.assertRaises(DeployFailed):
                    deploy_bundle(
                        deployment_group_name='windows-lab',
                        templates=templates,
                        owning_identifier='student',
                        entry_vlan=101,
                        internal_vlan=201,
                        uptime_minutes=60,
                        user_id=None,
                        load_balancing_config=LB_ON,
                    )
        finally:
            for target, name, listener in listeners:
                event.remove(target, name, listener)

        self.assertTrue(survivor_inserted.is_set())
        self.assertTrue(heartbeat_returned.is_set())
        survivor = VMRecord.query.one()
        self.assertEqual(survivor.vm_name, 'student-student-windows-0')

    def test_db_rollback_leaves_no_partially_committed_bundle_records(self):
        proxmox = self._proxmox_for_create(
            lambda **kwargs: 'UPID:clone')
        proxmox.nodes.return_value.status.get.return_value['memory'] = {
            'total': 16 * GIB,
            'used': 6 * GIB,
            'free': 1 * GIB,
            'available': 10 * GIB,
        }
        templates = [
            {
                'name': f'windows-{index}',
                'type': 'qemu',
                'memory': 1024,
                'cores': 1,
                'storage': 20,
                'template_locations': {'pve': 9706},
            }
            for index in range(2)
        ]
        real_log = vm_creation_module.log_vm_to_database
        log_calls = 0

        def fail_second_log(*args, **kwargs):
            nonlocal log_calls
            log_calls += 1
            if log_calls == 2:
                raise RuntimeError('database write failed')
            return real_log(*args, **kwargs)

        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox), \
             patch(
                 'app.routes.vm_creation.log_vm_to_database',
                 side_effect=fail_second_log,
             ):
            with self.assertRaises(DeployFailed):
                deploy_bundle(
                    deployment_group_name='windows-lab',
                    templates=templates,
                    owning_identifier='student',
                    entry_vlan=101,
                    internal_vlan=201,
                    uptime_minutes=60,
                    user_id=None,
                    load_balancing_config=LB_ON,
                )

        self.assertEqual(VMRecord.query.count(), 0)


    def test_db_outage_during_cleanup_preserves_deploy_failure_response(self):
        proxmox = self._proxmox_for_create(
            lambda **kwargs: 'UPID:clone')
        template = {
            'name': 'windows',
            'type': 'qemu',
            'memory': 1024,
            'cores': 1,
            'storage': 20,
            'template_locations': {'pve': 9706},
        }
        query_patcher = None

        def fail_log_and_queries(*args, **kwargs):
            nonlocal query_patcher
            query_patcher = patch.object(
                VMRecord,
                'query',
                new_callable=PropertyMock,
                side_effect=RuntimeError('database unavailable'),
            )
            query_patcher.start()
            raise RuntimeError('database unavailable')

        try:
            with patch(
                'app.proxmox_service.ProxmoxAPI',
                return_value=proxmox,
            ), patch(
                'app.routes.vm_creation.log_vm_to_database',
                side_effect=fail_log_and_queries,
            ):
                with self.assertRaises(DeployFailed):
                    deploy_bundle(
                        deployment_group_name='windows-lab',
                        templates=[template],
                        owning_identifier='student',
                        entry_vlan=101,
                        internal_vlan=201,
                        uptime_minutes=60,
                        user_id=None,
                        load_balancing_config=LB_ON,
                    )
        finally:
            if query_patcher is not None:
                query_patcher.stop()
if __name__ == '__main__':

    unittest.main()
