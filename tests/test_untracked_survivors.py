"""Failed-deploy survivors must stay tracked through a database outage.

When cleanup cannot confirm a guest's deletion the deploy path commits a
promptly-expiring VMRecord so the expiry loop destroys it. If that commit
fails because the database is down, the only other trace is an in-process
lease that expires; afterwards nothing would ever delete the guest. These
tests drive the real deploy path with the database failing, then bring the
database back and check that the guest becomes tracked (and destroyed).
"""

import json
import os
import stat
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.models import VMRecord, db
from app.proxmox_service import _clear_node_reservations, _clear_vmid_reservations
from app.routes.vm_creation import deploy_bundle
from app.tasks import initialize_vm_monitoring
from tests.test_admission_control import LB_ON, _proxmox
from tests.test_vm_destroy_safety import (
    _proxmox_with_qemu_endpoints, _run_one_expiry_cycle, _set_task_results)


def recover_untracked_survivors():
    from app.untracked_survivors import recover_untracked_survivors as recover
    return recover()


SURVIVOR_NAME = 'student-alice-windows-0'


@pytest.fixture(autouse=True)
def _clean_ledgers():
    _clear_node_reservations()
    _clear_vmid_reservations()
    yield
    _clear_node_reservations()
    _clear_vmid_reservations()


@pytest.fixture
def survivors_file():
    return os.environ['UNTRACKED_SURVIVORS_FILE']


def _templates():
    return [
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


def _create_result(index, template, vmid, *args, **kwargs):
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
        'vm_name': SURVIVOR_NAME,
        'vm_display_name': template['name'],
        'vm_type': 'qemu',
        'node': 'pve',
        'memory': 3072,
        'cores': 1,
        'storage': 20,
        'is_entry_point': False,
        '_reservation_id': 'survivor-reservation',
    }


def _deploy_with_database_down(app):
    """Run a bundle whose second clone fails, cleanup cannot delete the
    first, and every commit fails. Returns the reservation ids released."""
    released = []
    outage = OperationalError('COMMIT', {}, Exception('server has gone away'))
    with app.app_context():
        with patch('app.proxmox_service.ProxmoxAPI',
                   return_value=_proxmox(available_gib=8)), \
             patch('app.routes.vm_creation.create_single_vm_task',
                   side_effect=_create_result), \
             patch('app.routes.vm_creation._cleanup_with_sweep',
                   return_value=set()), \
             patch('app.routes.vm_creation.release_node_reservation',
                   side_effect=lambda rid: released.append(rid) or True), \
             patch.object(db.session, 'commit', side_effect=outage):
            with pytest.raises(Exception):
                deploy_bundle(
                    deployment_group_name='windows-lab',
                    templates=_templates(),
                    owning_identifier='alice',
                    entry_vlan=101,
                    internal_vlan=201,
                    uptime_minutes=60,
                    user_id=None,
                    load_balancing_config=LB_ON,
                )
    return released


def _lines(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_database_outage_records_survivor_in_durable_fallback_file(
        app, survivors_file):
    released = _deploy_with_database_down(app)

    assert os.path.exists(survivors_file), (
        'a survivor that could not be committed must be written to disk')
    entries = _lines(survivors_file)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry['vmid'], int)
    assert entry['node'] == 'pve'
    assert entry['vm_name'] == SURVIVOR_NAME
    assert entry['vm_type'] == 'qemu'
    assert entry['reason']
    assert stat.S_IMODE(os.stat(survivors_file).st_mode) == 0o600
    # The lease stays held exactly as before: capacity is only released
    # once tracking is durable in the database.
    assert 'survivor-reservation' not in released
    with app.app_context():
        assert VMRecord.query.count() == 0


def test_startup_recovery_imports_survivor_exactly_once(app, survivors_file):
    _deploy_with_database_down(app)

    initialize_vm_monitoring(app)

    with app.app_context():
        record = VMRecord.query.one()
        assert record.vm_name == SURVIVOR_NAME
        assert record.node == 'pve'
        assert record.vm_type == 'qemu'
        assert record.memory == 3072
        assert record.student_name == 'alice'
        assert record.template_name.startswith('failed-deploy-leftover/')
        assert record.expires_at <= datetime.now()
        assert record.destroyed is False
    assert not os.path.exists(survivors_file) or not _lines(survivors_file)

    initialize_vm_monitoring(app)
    with app.app_context():
        report = recover_untracked_survivors()
        assert VMRecord.query.count() == 1
    assert report['imported'] == 0
    assert report['corrupt'] == []


def test_recovery_leaves_file_when_database_is_still_down(app, survivors_file):
    _deploy_with_database_down(app)
    outage = OperationalError('COMMIT', {}, Exception('server has gone away'))

    with app.app_context():
        with patch.object(db.session, 'commit', side_effect=outage):
            recover_untracked_survivors()
        assert VMRecord.query.count() == 0

        report = recover_untracked_survivors()
        assert report['imported'] == 1
        assert VMRecord.query.count() == 1


def test_recovery_skips_survivor_already_tracked(app, survivors_file):
    _deploy_with_database_down(app)
    entry = _lines(survivors_file)[0]
    with app.app_context():
        db.session.add(VMRecord(
            vmid=entry['vmid'], vm_name=SURVIVOR_NAME, vm_type='qemu',
            node='pve', template_name='windows-lab/windows-0',
            student_name='alice', expires_at=datetime(2030, 1, 1)))
        db.session.commit()

        report = recover_untracked_survivors()

        record = VMRecord.query.one()
        assert record.expires_at == datetime(2030, 1, 1), (
            'recovery must never rewrite a live record it did not create')
    assert report['imported'] == 0
    assert report['already_tracked'] == 1


def test_corrupt_line_is_skipped_and_reported_not_fatal(app, survivors_file):
    good = {
        'vmid': 4321, 'node': 'pve', 'vm_name': 'student-bob-web',
        'vm_type': 'lxc', 'reason': 'test', 'student_name': 'bob',
        'template_name': 'failed-deploy-leftover/lab/web',
    }
    with open(survivors_file, 'w') as handle:
        handle.write('{"vmid": 99, "node": "pv\n')        # torn write
        handle.write(json.dumps({'vmid': 'x', 'node': 'pve'}) + '\n')
        handle.write(json.dumps(good) + '\n')

    with app.app_context():
        report = recover_untracked_survivors()
        record = VMRecord.query.one()

    assert record.vmid == 4321
    assert record.vm_type == 'lxc'
    assert report['imported'] == 1
    assert report['corrupt'] == [1, 2]
    rejected = survivors_file + '.corrupt'
    assert os.path.exists(rejected), 'corrupt lines are kept for the operator'
    with open(rejected) as handle:
        assert len(handle.read().splitlines()) == 2
    assert stat.S_IMODE(os.stat(rejected).st_mode) == 0o600


@patch('app.proxmox_service.ProxmoxAPI')
def test_expiry_loop_destroys_survivor_recorded_during_outage(
        mock_api, app, survivors_file):
    _deploy_with_database_down(app)
    vmid = _lines(survivors_file)[0]['vmid']
    proxmox, endpoints = _proxmox_with_qemu_endpoints(
        mock_api, [vmid], names={vmid: SURVIVOR_NAME})
    endpoints[vmid].delete.return_value = f'delete-{vmid}'
    _set_task_results(proxmox, {})

    _run_one_expiry_cycle(app)

    endpoints[vmid].delete.assert_called_once_with()
    with app.app_context():
        assert VMRecord.query.filter_by(vmid=vmid).count() == 0
