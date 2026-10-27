"""Regression tests for orphaned-VMRecord cleanup on `/list`.

The bug: `_verify_vm_exists` reported `exists=False` for ANY non-404
error — a timeout, a connection reset, a 401, a generic 5xx — and `/list`
treated that as "orphan" and deleted the `VMRecord`. The expiry daemon
only ever reads database rows (`app/tasks.py`), so once the row was gone
the VM ran in Proxmox until a human noticed. One transient API blip
during a class refresh was enough to leak a VM permanently.

The rule these tests pin: a row may only be deleted when the Proxmox
CLUSTER INVENTORY positively confirms the vmid is absent. Everything
else keeps the row.
"""

import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import update

from app import create_app
from app.models import db, AdminAlert, User, VMRecord
import app.routes.utils as route_utils
from app.routes.utils import (
    VM_CONFIG_ERROR, VM_MISSING, VM_PRESENT, VM_UNKNOWN, _verify_vm_exists,
)


class ProxmoxError(Exception):
    """Stand-in for proxmoxer's ResourceException, which carries a status."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _index(*vmids):
    return {v: {'vmid': v, 'name': 'vm', 'node': 'pve',
                'type': 'qemu', 'status': 'running'} for v in vmids}


class VerifyVmExistsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.proxmox = MagicMock()

    def tearDown(self):
        self.ctx.pop()

    def _config_raises(self, exc):
        self.proxmox.nodes.return_value.qemu.return_value.config.get.side_effect = exc

    def test_readable_config_is_present(self):
        self.proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {}
        self.assertEqual(
            _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu'),
            (VM_PRESENT, None))

    def test_timeout_is_unknown_not_missing(self):
        # THE bug. A read timeout used to mean "VM does not exist".
        self._config_raises(ProxmoxError('timed out'))
        state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)

    def test_connection_reset_is_unknown(self):
        self._config_raises(ConnectionResetError('connection reset by peer'))
        state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)

    def test_auth_failure_is_unknown(self):
        self._config_raises(ProxmoxError('401 authentication failure', 401))
        state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)

    def test_generic_500_is_unknown(self):
        self._config_raises(ProxmoxError('500 internal server error', 500))
        state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)

    def test_deleted_vm_is_missing_when_the_cluster_agrees(self):
        # Proxmox answers a deleted VM's config read with a 500 whose text
        # says "does not exist" — identical to a corrupted config. The
        # cluster inventory is what tells them apart.
        self._config_raises(ProxmoxError(
            "500 Configuration file 'nodes/pve/qemu-server/100.conf' "
            "does not exist", 500))
        with patch('app.proxmox_service.get_cluster_vm_index',
                   return_value=_index(101, 102)):
            state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_MISSING)

    def test_corrupt_config_on_a_live_vm_is_config_error_not_missing(self):
        # Same exception, but the cluster still lists the vmid: the VM is
        # real and running. Deleting its row would leak it.
        self._config_raises(ProxmoxError(
            "500 Configuration file 'nodes/pve/qemu-server/100.conf' "
            "does not exist", 500))
        with patch('app.proxmox_service.get_cluster_vm_index',
                   return_value=_index(100, 101)):
            state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_CONFIG_ERROR)

    def test_unreachable_cluster_downgrades_missing_to_unknown(self):
        # The config read says "gone" but we cannot corroborate it. During
        # a Proxmox outage BOTH calls fail, and that must never be read as
        # "every VM in the class has been deleted".
        self._config_raises(ProxmoxError('404 not found', 404))
        with patch('app.proxmox_service.get_cluster_vm_index',
                   side_effect=ProxmoxError('cluster unreachable')):
            state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)

    def test_decision_lookup_refuses_a_stale_snapshot(self):
        # A cached inventory from before an outage could "confirm" absence
        # for a VM created since. The delete path must ask for a fresh,
        # non-stale answer.
        self._config_raises(ProxmoxError('404 not found', 404))
        with patch('app.proxmox_service.get_cluster_vm_index',
                   return_value=_index()) as idx:
            _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu')
        kwargs = idx.call_args.kwargs
        self.assertFalse(kwargs.get('allow_stale', True),
                         'the delete decision must not accept stale data')
        self.assertLessEqual(kwargs.get('max_age', 999), 2.0)

    def test_a_transport_error_mentioning_404_is_not_a_missing_config(self):
        # proxmoxer puts the request URL in a transport error, and that URL
        # carries the vmid. A plain connection failure for vmid 1404 used
        # to read as "the config is gone" and reach the delete decision.
        # vmids walk a wide range here: rows are hard-deleted and
        # get_next_available_vmid is max+1.
        self._config_raises(ProxmoxError(
            "HTTPSConnectionPool(host='pve', port=8006): Max retries exceeded "
            "with url: /api2/json/nodes/pve/qemu/1404/config "
            "(Caused by NewConnectionError)"))
        with patch('app.proxmox_service.get_cluster_vm_index',
                   return_value=_index()) as idx:
            state, _ = _verify_vm_exists(self.proxmox, 'pve', 1404, 'qemu')
        self.assertEqual(state, VM_UNKNOWN)
        idx.assert_not_called()

    def test_an_index_snapshot_answers_present_without_a_per_vm_call(self):
        self.proxmox.nodes.return_value.qemu.return_value.config.get.side_effect = \
            AssertionError('must not make a per-VM call')
        state, _ = _verify_vm_exists(self.proxmox, 'pve', 100, 'qemu',
                                     index=_index(100))
        self.assertEqual(state, VM_PRESENT)


class ListOrphanCleanupTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.user = User(login_id='orphan-user',
                         password_hash=generate_password_hash('p'))
        db.session.add(self.user)
        db.session.commit()
        self.vm = VMRecord(
            vmid=700, vm_name='lab-01', vm_type='qemu', node='pve',
            template_name='grp/lab-01', student_name='orphan-user',
            created_by_user_id=self.user.id,
            expires_at=datetime.now() + timedelta(hours=3))
        db.session.add(self.vm)
        db.session.commit()
        with self.client.session_transaction() as s:
            s['user_id'] = self.user.id
            s['session_version'] = self.user.session_version

    def tearDown(self):
        route_utils._missing_since.clear()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _list(self, verify_result):
        with patch('app.routes.vm_listing.get_proxmox_connection',
                   return_value=MagicMock()), \
             patch('app.routes.vm_listing._verify_vm_exists',
                   return_value=verify_result), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=['10.0.0.9']), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value={'deployment_groups': []}):
            return self.client.get('/list')

    def _vm_entries(self, resp):
        body = resp.get_json()
        self.assertTrue(body['success'], body)
        return [vm
                for student in body['students']
                for group in student['deployment_groups']
                for vm in group['vms']]

    def test_transient_error_keeps_the_record_and_still_lists_the_vm(self):
        # The whole point: a blip must not cost us the only record that
        # the expiry daemon uses to destroy this VM later.
        resp = self._list((VM_UNKNOWN, 'read timed out'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(db.session.get(VMRecord, self.vm.id),
                             'a transient Proxmox error deleted a live VM record')
        entries = self._vm_entries(resp)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['status'], 'unknown')

    def test_config_error_keeps_the_record_and_flags_the_vm(self):
        with patch('app.routes.vm_listing._send_config_issue_webhook'):
            resp = self._list((VM_CONFIG_ERROR, 'config file unreadable'))
        self.assertIsNotNone(db.session.get(VMRecord, self.vm.id))
        entries = self._vm_entries(resp)
        self.assertTrue(entries[0]['has_config_issues'])
        self.assertEqual(entries[0]['status'], 'error')

    def test_one_missing_sighting_keeps_the_record(self):
        # The inventory is not an independent witness, so a single "gone"
        # answer must not be allowed to destroy the only record that keeps
        # the expiry daemon responsible for this VM.
        resp = self._list((VM_MISSING, 'no such vmid in the cluster'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(db.session.get(VMRecord, self.vm.id))
        # It is held back from the listing while it is under suspicion.
        self.assertEqual(self._vm_entries(resp), [])

    def test_a_confirmed_missing_vm_is_cleaned_up(self):
        # Cleanup still has to happen, or phantom rows pile up forever.
        import time as _time
        self._list((VM_MISSING, 'no such vmid in the cluster'))
        route_utils._missing_since[('pve', 700)] = (
            _time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        resp = self._list((VM_MISSING, 'no such vmid in the cluster'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(db.session.get(VMRecord, self.vm.id))
        self.assertEqual(self._vm_entries(resp), [])

    def test_a_blip_between_sightings_cancels_the_cleanup(self):
        import time as _time
        self._list((VM_MISSING, 'gone'))
        route_utils._missing_since[('pve', 700)] = (
            _time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        # One render where the VM is reachable again clears the suspicion.
        self._list((VM_PRESENT, None))
        self._list((VM_MISSING, 'gone'))
        self.assertIsNotNone(db.session.get(VMRecord, self.vm.id))

    def test_present_vm_is_listed_normally(self):
        resp = self._list((VM_PRESENT, None))
        self.assertIsNotNone(db.session.get(VMRecord, self.vm.id))
        entries = self._vm_entries(resp)
        self.assertEqual(entries[0]['status'], 'running')
        self.assertFalse(entries[0]['has_config_issues'])

    def _list_after_confirmed_absence(self, lock):
        """Render twice, ORPHAN_CONFIRM_SECONDS apart, the VM gone both times."""
        self._list((VM_MISSING, 'gone'))
        route_utils._missing_since[('pve', 700)] = (
            time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        with patch('app.tasks.vm_lifecycle_lock', lock):
            return self._list((VM_MISSING, 'gone'))

    def test_confirmed_missing_record_is_retired_under_its_lock_with_alert(self):
        """/list retires through the reconcile pass, not a second path."""
        acquired = []
        record_id = self.vm.id

        @contextmanager
        def recording_lock(vmid):
            acquired.append(vmid)
            yield

        resp = self._list_after_confirmed_absence(recording_lock)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(acquired, [700])
        self.assertIsNone(db.session.get(VMRecord, record_id))
        alerts = AdminAlert.query.filter_by(
            alert_type='vm_record_reconciled').all()
        self.assertEqual([alert.vmid for alert in alerts], [700])

    def test_record_changed_while_waiting_for_the_lock_is_kept(self):
        """The decision is made on the row as it stands under the lock."""
        record_id = self.vm.id

        @contextmanager
        def lock_won_by_a_replacement(vmid):
            # Another request re-pointed the record while /list waited.
            db.session.execute(update(VMRecord)
                               .where(VMRecord.id == record_id)
                               .values(vmid=701))
            db.session.commit()
            yield

        self._list_after_confirmed_absence(lock_won_by_a_replacement)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(VMRecord, record_id))
        self.assertEqual(AdminAlert.query.filter_by(
            alert_type='vm_record_reconciled').count(), 0)


class OrphanConfirmationTests(unittest.TestCase):
    """A single 'missing' sighting must never delete a row.

    Verified on a live cluster: the vmid set returned by
    `cluster/resources` is byte-identical to `/etc/pve/.vmlist`, which is
    the union of the `<vmid>.conf` files. That is the same artifact whose
    absence produces the config-read error. The inventory is therefore a
    corroborating witness, not an independent one, and time has to supply
    the second observation.
    """

    def setUp(self):
        route_utils._missing_since.clear()

    tearDown = setUp

    def test_first_missing_sighting_does_not_authorise_a_delete(self):
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_MISSING))

    def test_a_second_sighting_inside_the_window_still_does_not(self):
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_MISSING))

    def test_a_sighting_after_the_window_authorises_a_delete(self):
        import time as _time
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        route_utils._missing_since[('pve', 700)] = (
            _time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        self.assertTrue(route_utils.note_vm_state('pve', 700, VM_MISSING))

    def test_any_other_state_resets_the_clock(self):
        import time as _time
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        route_utils._missing_since[('pve', 700)] = (
            _time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        # One good sighting in between means the VM is not gone after all.
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_PRESENT))
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_MISSING))

    def test_unknown_also_resets_the_clock(self):
        import time as _time
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        route_utils._missing_since[('pve', 700)] = (
            _time.monotonic() - route_utils.ORPHAN_CONFIRM_SECONDS - 1)
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_UNKNOWN))
        self.assertFalse(route_utils.note_vm_state('pve', 700, VM_MISSING))

    def test_vmids_are_tracked_per_node(self):
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        self.assertFalse(route_utils.note_vm_state('pve2', 700, VM_MISSING))

    def test_forget_clears_the_record(self):
        route_utils.note_vm_state('pve', 700, VM_MISSING)
        route_utils.forget_vm_state('pve', 700)
        self.assertNotIn(('pve', 700), route_utils._missing_since)


if __name__ == '__main__':
    unittest.main()
