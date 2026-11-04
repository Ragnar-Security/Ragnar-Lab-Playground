"""Every existing VM lifecycle path dispatches on the record's stored backend.

Expiry, student and admin destroy, extend, failed-deploy cleanup, reconcile,
listing and retirement cleanup run against the in-memory FakeBackend ('aws'),
and the assertions read what the fake provider and the database now hold.
"""

import importlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import current_app
from sqlalchemy.exc import OperationalError

import app.routes.utils as route_utils
import app.tasks as tasks
from app.alert_outbox import enqueue_alert
from app.backends import register_backend_factory
from app.backends.base import (
    GUEST_MISMATCH, GUEST_UNKNOWN, BackendError, DestroyPolicy, VMRef,
)
from app.backends.proxmox import ProxmoxBackend
from app.models import AlertOutbox, KothGame, StudentGroup, User, VMRecord, db
from app.routes.koth_admin import _destroy_game_vms
from app.routes.vm_creation import (
    DeployFailed, _cleanup_with_sweep, cleanup_vms, deploy_bundle,
)
from app.untracked_survivors import recover_untracked_survivors
from app.tasks import reconcile_missing_vm_records
from app.vm_provisioning import after_record_retired, create_vm
from tests.backends.fake_backend import (
    StaticInventoryBackend, registered_fake_backend,
)
from tests.backends.proxmox_case import forget_proxmox_caches
from tests.test_live_contract_guards import FakeProxmox, _missing

config = importlib.import_module('app.config')


@pytest.fixture(autouse=True)
def _app_context(app):
    # The helpers and assertions read the database directly; requests and
    # the expiry pass open contexts (and sessions) of their own.
    with app.app_context():
        yield


@pytest.fixture
def admin_client(app):
    with app.app_context():
        admin = User(login_id='nodes-admin', is_admin=True, session_version=1)
        admin.set_password('Correct-Horse-9')
        db.session.add(admin)
        db.session.commit()
        user_id = admin.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['session_version'] = 1
    return client


@pytest.fixture
def queued_alert(app, monkeypatch):
    class Queued:
        def __init__(self, alert_id):
            self.alert_id = alert_id

        @property
        def delivered(self):
            db.session.expire_all()
            return db.session.get(AlertOutbox, self.alert_id).status == 'sent'

    # The database channel only: the file channel prints a VM this alert
    # does not describe.
    row = enqueue_alert(alert_type='test', severity='info', message='queued',
                        channels=('database',))
    db.session.commit()
    return Queued(row.id)


def _owner(login_id):
    user = User.query.filter_by(login_id=login_id).first()
    if user is None:
        user = User(login_id=login_id, full_name=login_id, session_version=1)
        user.set_password('Correct-Horse-9')
        db.session.add(user)
        db.session.commit()
    return user


def persist_case_record(case, *, expired=False, expires_in=timedelta(hours=1),
                        backend=None, spec=None):
    """Commit one live record of `spec`, launched for real unless `backend`."""
    spec = spec or case.spec
    owner = _owner(spec.owner)
    if backend is None:
        reservation_id = f'persist-{spec.vmid}'
        _location, launched = create_vm(
            spec, reservation_id=reservation_id, load_balancing_config=None,
            heartbeat=lambda: None)
        case.backend.release_admission(reservation_id)
        identity = dict(backend=spec.backend, vm_type=launched.vm_type,
                        node=launched.location, provider_id=launched.provider_id)
    else:
        identity = dict(backend=backend, vm_type=backend, node=f'{backend}-zone',
                        provider_id=None)
    expires_at = (datetime.now() - timedelta(minutes=5) if expired
                  else datetime.now() + expires_in)
    record = VMRecord(
        vmid=spec.vmid, vm_name=spec.vm_name,
        template_name=f'Cloud Lab/{spec.display_name}',
        student_name=spec.owner, memory=spec.memory_mb, cores=spec.cores,
        storage=spec.storage_gb, created_at=datetime.now(),
        expires_at=expires_at, created_by_user_id=owner.id,
        is_entry_point=spec.is_entry_point, deployment_id=spec.deployment_id,
        network_slot=spec.network_slot, **identity)
    db.session.add(record)
    db.session.commit()
    # Detached with every column loaded: the test keeps reading its identity
    # while each lookup through the session sees the database as it is.
    db.session.refresh(record)
    db.session.expunge(record)
    return record


def persist_two_case_records(case):
    second = replace(case.spec, vmid=case.spec.vmid + 1,
                     vm_name=case.spec.vm_name + '-2')
    return (persist_case_record(case),
            persist_case_record(case, spec=second))


def _retire(record):
    ref = VMRef.from_record(record)
    db.session.rollback()
    tasks._retire_vm_record(db.session.get(VMRecord, record.id))
    db.session.commit()
    return ref


def retire_and_finalize(record):
    after_record_retired(_retire(record))


def retire_rows_together_then_finalize(first, second):
    app = current_app._get_current_object()
    barrier = threading.Barrier(2)
    errors = []

    def retire(record):
        try:
            with app.app_context():
                barrier.wait(timeout=5)
                after_record_retired(_retire(record))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=retire, args=(record,))
               for record in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return errors


class _StopExpiryLoop(BaseException):
    pass


def run_one_expiry_pass(app, enabled=None):
    minute_sleeps = 0

    def fake_sleep(seconds):
        nonlocal minute_sleeps
        if seconds == 60:
            minute_sleeps += 1
            if minute_sleeps == 2:
                raise _StopExpiryLoop

    enabled_patch = (patch.object(config, 'ENABLED_BACKENDS', tuple(enabled))
                     if enabled is not None else _nothing())
    with enabled_patch, patch('app.tasks.time.sleep', side_effect=fake_sleep):
        try:
            tasks.check_and_destroy_expired_vms(app)
        except _StopExpiryLoop:
            pass


@contextmanager
def _nothing():
    yield


def login_owner(client, user_id):
    user = db.session.get(User, user_id)
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['session_version'] = user.session_version


def _machine(case, **extra):
    return {'name': case.spec.display_name, 'backend': 'aws',
            'entry_point': True, 'aws': dict(case.spec.template), **extra}


def deploy_case_bundle(case, failure):
    if failure == 'second':
        templates = [_machine(case),
                     _machine(case, name='memory-internal', entry_point=False)]
        case.backend.fail_next_create = True
    elif failure == 'after_launch':
        templates = [_machine(case)]
        case.backend.fail_create_after_launch = True
    else:
        raise ValueError(failure)
    return deploy_bundle(
        deployment_group_name='Cloud Lab', templates=templates,
        owning_identifier=case.spec.owner, entry_vlan=case.spec.entry_slot,
        internal_vlan=case.spec.internal_slot, uptime_minutes=60, user_id=None,
        backend_name='aws')


def _catalog(backend, block, **group):
    return {'deployment_groups': [{
        'display_name': 'Cloud Lab', 'enabled': True, 'backend': backend,
        'templates': [{'name': 'memory-100', backend: block}], **group}]}


def valid_aws_catalog(extend_time_minutes=1440):
    return _catalog('aws', {'image_id': 'ami-placeholder',
                            'instance_type': 'small'},
                    extend_time_minutes=extend_time_minutes)


def valid_gcp_catalog():
    return _catalog('gcp', {'image': 'image-placeholder',
                            'machine_type': 'small'})


# Catches expiry using Proxmox regardless of the record's persisted backend.
def test_expiry_dispatches_strict_to_record_backend(app, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case, expired=True)
    run_one_expiry_pass(app)
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.STRICT
    assert db.session.get(VMRecord, record.id) is None
    # The last record of its deployment and slot takes their networks along.
    assert case.backend.deployment_networks == set()
    assert case.backend.slot_networks == set()


# Catches student destroy losing its corrupt-config force behavior.
def test_student_destroy_dispatches_force_corrupt(app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    response = client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is True
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.FORCE_CORRUPT
    assert db.session.get(VMRecord, record.id) is None


# Catches admin bulk/failed deploy accidentally becoming strict.
def test_admin_bulk_dispatches_best_effort(app, admin_client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case)
    body = admin_client.post(
        '/admin/bulk-destroy', json={'vm_ids': [record.id]}).get_json()
    assert body['destroyed'] == 1
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.BEST_EFFORT


# Catches a failed deploy leaking its deployment network or its slot subnet (I4).
def test_failed_deploy_dispatches_best_effort_and_cleans_network_and_slot(
        app, memory_backend_case):
    case = memory_backend_case
    with pytest.raises(DeployFailed):
        deploy_case_bundle(case, failure='second')
    assert set(case.backend.destroyed.values()) == {DestroyPolicy.BEST_EFFORT}
    assert case.backend.instances == {}
    assert case.spec.deployment_id not in case.backend.deployment_networks
    assert case.backend.deployment_networks == set()
    assert case.spec.network_slot not in case.backend.slot_networks
    assert case.backend.slot_networks == set()
    assert VMRecord.query.filter_by(destroyed=False).count() == 0


# Catches a launch-then-raise cloud instance with no provider id leaking forever (C7).
def test_failed_deploy_destroys_instance_created_before_create_raised(
        app, memory_backend_case):
    case = memory_backend_case
    with pytest.raises(DeployFailed):
        deploy_case_bundle(case, failure='after_launch')
    assert case.spec.vmid not in case.backend.instances
    assert case.backend.instances == {}
    [(vmid, policy)] = case.backend.destroyed.items()
    assert policy == DestroyPolicy.BEST_EFFORT
    assert VMRecord.query.filter_by(vmid=vmid, destroyed=False).count() == 0
    assert case.backend.deployment_networks == set()
    assert case.backend.slot_networks == set()


# Catches deleting a DB row after mismatch, unknown, timeout, or provider error.
@pytest.mark.parametrize('identity', [GUEST_MISMATCH, GUEST_UNKNOWN])
def test_no_lifecycle_path_retires_unverified_guest(
        app, client, memory_backend_case, identity):
    case = memory_backend_case
    record = persist_case_record(case)
    case.backend.inspect_result = identity
    login_owner(client, record.created_by_user_id)
    response = client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is False
    assert db.session.get(VMRecord, record.id) is not None
    assert record.vmid in case.backend.instances


# Catches premature SG/firewall/subnet deletion while a sibling or reservation exists.
def test_after_retirement_cleans_only_last_deployment_and_slot(
        app, memory_backend_case):
    case = memory_backend_case
    first, second = persist_two_case_records(case)
    retire_and_finalize(first)
    assert case.spec.deployment_id in case.backend.deployment_networks
    assert case.spec.network_slot in case.backend.slot_networks
    retire_and_finalize(second)
    assert case.spec.deployment_id not in case.backend.deployment_networks
    assert case.spec.network_slot not in case.backend.slot_networks


# Catches a manually deleted cloud VM being retired on one observation.
def test_reconcile_requires_two_positive_absent_sightings(
        app, memory_backend_case, monkeypatch):
    case = memory_backend_case
    record = persist_case_record(case)
    case.backend.instances.pop(record.vmid)
    monkeypatch.setattr(route_utils, 'ORPHAN_CONFIRM_SECONDS', 300.0)
    assert reconcile_missing_vm_records() == []
    assert db.session.get(VMRecord, record.id) is not None
    monkeypatch.setitem(route_utils._missing_since, (record.node, record.vmid),
                        time.monotonic() - 301)
    assert reconcile_missing_vm_records() == [record.vmid]
    assert db.session.get(VMRecord, record.id) is None


# Catches cloud listing applying Proxmox's 10/8 guest-agent filter.
def test_list_uses_backend_private_ip_and_status(app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    response = client.get('/list').get_json()
    vm = response['students'][0]['deployment_groups'][0]['vms'][0]
    assert vm['ip_address'] == '192.0.2.10'
    assert vm['status'] == 'running'


# Catches a record of a disabled backend vanishing from the dashboard or being
# put up for retirement by /list (I18).
def test_list_shows_disabled_backend_record_as_unknown(
        app, client, memory_backend_case):
    record = persist_case_record(memory_backend_case, backend='gcp')
    login_owner(client, record.created_by_user_id)
    response = client.get('/list').get_json()
    vm = response['students'][0]['deployment_groups'][0]['vms'][0]
    assert vm['status'] == 'unknown'
    assert db.session.get(VMRecord, record.id) is not None


# Catches the VM detail page searching Proxmox for a VM its record puts elsewhere.
def test_detail_reads_identity_from_record_and_state_from_its_backend(
        app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    with patch('app.proxmox_service.ProxmoxAPI',
               side_effect=AssertionError('detail asked Proxmox')):
        body = client.get(f'/api/vms/{record.vmid}').get_json()
    assert body['success'] is True
    vm = body['vm']
    assert (vm['name'], vm['node'], vm['type']) == (
        record.vm_name, record.node, 'aws')
    assert (vm['memory'], vm['cores']) == (record.memory, record.cores)
    assert vm['status'] == 'running'
    assert vm['ip_addresses'] == ['192.0.2.10']


# Catches concurrent retirement making idempotent cleanup fail or leave a network.
def test_concurrent_last_retirements_converge_without_error(
        app, memory_backend_case):
    case = memory_backend_case
    first, second = persist_two_case_records(case)
    errors = retire_rows_together_then_finalize(first, second)
    assert errors == []
    assert case.spec.deployment_id not in case.backend.deployment_networks
    assert case.spec.network_slot not in case.backend.slot_networks


# Catches an operator YAML edit redirecting a running VM to another provider (M6).
def test_destroy_uses_record_backend_not_current_template(
        app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case)
    login_owner(client, record.created_by_user_id)
    yaml_read = AssertionError('/destroy resolved a backend from YAML')
    with registered_fake_backend('gcp') as gcp, patch(
            'app.template_manager.load_template_config',
            return_value=valid_gcp_catalog()), patch(
            'app.backends.template_spec.resolve_group_backend', side_effect=yaml_read), patch(
            'app.backends.template_spec.backend_block', side_effect=yaml_read):
        response = client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    assert response.get_json()['success'] is True
    assert case.backend.destroyed[record.vmid] == DestroyPolicy.FORCE_CORRUPT
    assert gcp.destroyed == {}


# Catches a failed provider expiry update being counted as extended (I2).
def test_extend_counts_only_successful_provider_updates(
        app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case, expires_in=timedelta(minutes=30))
    record_id, original_expiry = record.id, record.expires_at  # scalars, before the request
    case.backend.fail_set_expiry = True
    login_owner(client, record.created_by_user_id)
    with patch('app.template_manager.load_template_config',
               return_value=valid_aws_catalog(extend_time_minutes=60)):
        body = client.post('/extend', data={'deployment_group': 'Cloud Lab'}).get_json()
    assert body['success'] is True and body['extended_count'] == 0
    db.session.expire_all()
    assert db.session.get(VMRecord, record_id).expires_at == original_expiry + timedelta(minutes=60)


# Catches extend counting a VM whose provider copy of the expiry it never wrote,
# or never writing it.
def test_extend_writes_the_committed_expiry_to_the_provider(
        app, client, memory_backend_case):
    case = memory_backend_case
    record = persist_case_record(case, expires_in=timedelta(minutes=30))
    login_owner(client, record.created_by_user_id)
    with patch('app.template_manager.load_template_config',
               return_value=valid_aws_catalog(extend_time_minutes=60)):
        body = client.post('/extend', data={'deployment_group': 'Cloud Lab'}).get_json()
    assert body['extended_count'] == 1
    db.session.expire_all()
    committed = db.session.get(VMRecord, record.id).expires_at
    assert case.backend.expiry[record.vmid] == int(committed.timestamp())


# Catches a backend removed from ENABLED_BACKENDS leaving live VMs undestroyable
# and silent (I18), or stopping the pass before another backend's record.
def test_record_of_disabled_backend_is_retained_retried_and_alerted(
        app, memory_backend_case, monkeypatch):
    case = memory_backend_case
    record = persist_case_record(
        case, expired=True, backend='gcp',
        spec=replace(case.spec, vmid=case.spec.vmid + 1,
                     vm_name=case.spec.vm_name + '-gcp'))
    neighbour = persist_case_record(case, expired=True)
    monkeypatch.setattr(tasks, 'VM_DELETION_MAX_RETRIES', 1)
    run_one_expiry_pass(app)
    row = db.session.get(VMRecord, record.id)
    assert row is not None
    assert (row.deletion_retry_count, row.deletion_alert_sent) == (1, True)
    assert row.deletion_last_attempt_at is not None
    assert AlertOutbox.query.count() == 1
    # The same pass still destroyed the enabled backend's expired record.
    assert case.backend.destroyed == {neighbour.vmid: DestroyPolicy.STRICT}
    assert db.session.get(VMRecord, neighbour.id) is None


# Catches an empty cloud-only expiry pass trying to open Proxmox before alerts.
def test_cloud_only_empty_pass_delivers_alert_without_proxmox(
        app, memory_backend_case, queued_alert):
    attempted = {'proxmox': False}
    register_backend_factory(
        'proxmox',
        lambda: attempted.__setitem__('proxmox', True))
    run_one_expiry_pass(app, enabled=('aws',))
    assert queued_alert.delivered is True
    assert attempted['proxmox'] is False


# Catches a cloud instance that would not die being left untracked, or its
# deployment network deleted under it (cleanup_deployment runs only when every
# launched instance is confirmed absent).
def test_failed_deploy_tracks_a_stuck_cloud_instance_with_its_identity(
        app, memory_backend_case):
    case = memory_backend_case
    with patch.object(case.backend, 'destroy',
                      side_effect=BackendError('terminate refused')), \
            pytest.raises(DeployFailed):
        deploy_case_bundle(case, failure='after_launch')
    [vmid] = case.backend.instances
    survivor = VMRecord.query.filter_by(vmid=vmid, destroyed=False).one()
    assert (survivor.backend, survivor.vm_type, survivor.node) == (
        'aws', 'aws', 'memory-zone')
    assert survivor.provider_id == case.backend.instances[vmid].provider_id
    assert survivor.network_slot == case.spec.network_slot
    assert survivor.deployment_id in case.backend.deployment_networks
    assert survivor.expires_at <= datetime.now()
    assert case.spec.network_slot in case.backend.slot_networks


# Catches a cascade group delete leaving a cloud group's VM, its deployment
# network or its slot behind.
def test_group_delete_destroys_cloud_vms_and_their_networks(
        app, admin_client, memory_backend_case):
    case = memory_backend_case
    group = StudentGroup(name='team-cloud', display_name='Team Cloud',
                         entry_vlan_tag=331, internal_vlan_tag=431)
    db.session.add(group)
    db.session.commit()
    group_id = group.id
    record = persist_case_record(case)
    db.session.get(VMRecord, record.id).owner_group_id = group_id
    db.session.commit()

    with patch('app.routes.student_groups.get_backend',
               return_value=StaticInventoryBackend([])):
        response = admin_client.delete(
            f'/admin/groups/{group_id}?confirm_destroy=1')

    assert response.status_code == 200, response.data
    assert case.backend.destroyed == {record.vmid: DestroyPolicy.BEST_EFFORT}
    db.session.expire_all()
    assert db.session.get(VMRecord, record.id).destroyed is True
    assert db.session.get(StudentGroup, group_id) is None
    assert case.backend.deployment_networks == set()
    assert case.backend.slot_networks == set()


# Catches the KOTH destroy sweep losing a stranded guest (no record) or taking
# another game's guest, now that it reads the Proxmox backend's inventory.
def test_koth_destroy_sweeps_stranded_proxmox_guests_of_its_game_only(
        app, memory_backend_case):
    proxmox = FakeProxmox()
    register_backend_factory('proxmox', lambda: ProxmoxBackend(client=proxmox))
    game = KothGame(name='Game', deployment_group_name='koth-lab',
                    entry_vlan_tag=300, internal_vlan_tag=400)
    db.session.add(game)
    db.session.commit()
    proxmox.add_guest(9501, f'koth-{game.id}-box', node='node-a',
                      status='running')
    proxmox.add_guest(9502, f'koth-{game.id + 1}-box', node='node-a',
                      status='running')
    forget_proxmox_caches()

    with patch('app.proxmox_service.wait_for_proxmox_task',
               return_value={'success': True, 'status': 'stopped'}):
        destroyed = _destroy_game_vms(game.id)

    assert destroyed == 1
    assert 9501 not in proxmox.guests
    assert 9502 in proxmox.guests
    forget_proxmox_caches()


# --- fix round 1: Proxmox display and outage semantics ------------------------

def _proxmox_portal(api):
    """Serve Proxmox from `api` on the mixed portal, with fresh caches."""
    register_backend_factory('proxmox', lambda: ProxmoxBackend(client=api))
    forget_proxmox_caches()


def _proxmox_record(*, vmid=500, expired=False, api=None, status='running'):
    owner = _owner('student-a')
    name = f'student-student-a-pve-{vmid}'
    if api is not None:
        api.add_guest(vmid, name, node='node-a', status=status,
                      ips=['10.1.0.5'])
    record = VMRecord(
        vmid=vmid, vm_name=name, vm_type='qemu', node='node-a',
        template_name='Proxmox Lab/pve', student_name='student-a',
        memory=1024, cores=1, created_at=datetime.now(),
        created_by_user_id=owner.id,
        expires_at=(datetime.now() - timedelta(minutes=5) if expired
                    else datetime.now() + timedelta(hours=1)))
    db.session.add(record)
    db.session.commit()
    db.session.refresh(record)
    db.session.expunge(record)
    return record


# Catches the Proxmox VM detail page losing the uptime it always reported.
def test_proxmox_detail_reports_guest_uptime(app, client, memory_backend_case):
    api = FakeProxmox()
    _proxmox_portal(api)
    record = _proxmox_record(api=api)
    api.guests[record.vmid]['uptime'] = 4242
    login_owner(client, record.created_by_user_id)
    body = client.get(f'/api/vms/{record.vmid}').get_json()
    assert body['success'] is True, body
    assert body['vm']['uptime'] == 4242
    forget_proxmox_caches()


# Catches the /list config-error webhook losing Proxmox's own diagnostic.
def test_list_config_error_webhook_carries_the_provider_diagnostic(
        app, client, memory_backend_case):
    api = FakeProxmox()
    _proxmox_portal(api)
    # The cluster lists it in no healthy state and its config read fails.
    record = _proxmox_record(api=api, status='unknown')
    api.corrupt_config.add(record.vmid)
    login_owner(client, record.created_by_user_id)
    with patch('app.routes.vm_listing._send_config_issue_webhook') as hook:
        body = client.get('/list').get_json()
    vm = body['students'][0]['deployment_groups'][0]['vms'][0]
    assert vm['ip_address'] == 'CONFIG ERROR'
    [(alerted, diagnostic)] = [c.args for c in hook.call_args_list]
    assert alerted.vmid == record.vmid
    assert diagnostic == str(_missing('node-a', record.vmid))
    forget_proxmox_caches()


# Catches a Proxmox outage at the start of a pass being counted as failed
# deletes (and alerted) for every Proxmox VM, or stopping cloud expiry and
# alert delivery.
def test_proxmox_connect_failure_skips_proxmox_records_without_counting(
        app, memory_backend_case, queued_alert, monkeypatch):
    case = memory_backend_case
    register_backend_factory('proxmox', lambda: ProxmoxBackend())
    proxmox_record = _proxmox_record(expired=True)
    cloud_record = persist_case_record(case, expired=True)
    monkeypatch.setattr(tasks, 'VM_DELETION_MAX_RETRIES', 1)
    with patch('app.proxmox_service.get_proxmox_connection',
               side_effect=RuntimeError('Proxmox unreachable')):
        run_one_expiry_pass(app)
    row = db.session.get(VMRecord, proxmox_record.id)
    assert (row.deletion_retry_count, row.deletion_last_attempt_at,
            row.deletion_alert_sent) == (0, None, False)
    assert AlertOutbox.query.filter_by(
        alert_type='vm_deletion_failed').count() == 0
    assert case.backend.destroyed == {cloud_record.vmid: DestroyPolicy.STRICT}
    assert db.session.get(VMRecord, cloud_record.id) is None
    assert queued_alert.delivered is True


# --- fix round 1: adversarial safety pinning -----------------------------------

def _group_owning(record):
    group = StudentGroup(name='team-fail', display_name='Team Fail',
                         entry_vlan_tag=332, internal_vlan_tag=432)
    db.session.add(group)
    db.session.commit()
    db.session.get(VMRecord, record.id).owner_group_id = group.id
    db.session.commit()
    return group.id


def _run_path(path, app, client, admin_client, record):
    if path == 'expiry':
        run_one_expiry_pass(app)
    elif path == 'student':
        login_owner(client, record.created_by_user_id)
        client.post('/destroy', data={'deployment_group': 'Cloud Lab'})
    elif path == 'admin':
        admin_client.post('/admin/bulk-destroy', json={'vm_ids': [record.id]})
    elif path == 'group':
        group_id = _group_owning(record)
        with patch('app.routes.student_groups.get_backend',
                   return_value=StaticInventoryBackend([])):
            admin_client.delete(f'/admin/groups/{group_id}?confirm_destroy=1')
    else:
        raise ValueError(path)


# Catches any lifecycle path retiring a record after its provider raised.
@pytest.mark.parametrize('failing', ['inspect', 'destroy'])
@pytest.mark.parametrize('path', ['expiry', 'student', 'admin', 'group'])
def test_provider_exception_never_retires_a_record(
        app, client, admin_client, memory_backend_case, path, failing):
    case = memory_backend_case
    record = persist_case_record(case, expired=(path == 'expiry'))
    with patch.object(case.backend, failing,
                      side_effect=BackendError('provider down')):
        _run_path(path, app, client, admin_client, record)
    db.session.expire_all()
    row = db.session.get(VMRecord, record.id)
    assert row is not None and row.destroyed is False
    assert record.vmid in case.backend.instances


# Catches reconcile retiring a record, or aborting, when its inventory or
# its confirmation raised; a failed confirmation is no sighting at all.
@pytest.mark.parametrize('failing', ['list_managed', 'confirm_absent'])
def test_reconcile_provider_exception_keeps_the_record(
        app, memory_backend_case, monkeypatch, failing):
    case = memory_backend_case
    record = persist_case_record(case)
    case.backend.instances.pop(record.vmid)
    key = (record.node, record.vmid)
    # A confirming sighting now would complete the window.
    monkeypatch.setitem(route_utils._missing_since, key, time.monotonic() - 301)
    with patch.object(case.backend, failing,
                      side_effect=BackendError('provider down')):
        assert reconcile_missing_vm_records() == []
    assert db.session.get(VMRecord, record.id) is not None
    if failing == 'confirm_absent':
        assert key not in route_utils._missing_since


# Catches reconcile reading inventory per record instead of once per backend
# per pass (ruling C5).
def test_reconcile_reads_inventory_once_per_backend(
        app, memory_backend_case):
    case = memory_backend_case
    api = FakeProxmox()
    _proxmox_portal(api)
    for vmid in (500, 501):
        _proxmox_record(vmid=vmid, api=api)
    records = [persist_case_record(case, spec=replace(
        case.spec, vmid=case.spec.vmid + n, vm_name=f'{case.spec.vm_name}-{n}'))
        for n in range(3)]
    case.backend.instances.pop(records[2].vmid)   # one is missing
    with patch.object(case.backend, 'list_managed',
                      wraps=case.backend.list_managed) as listed:
        assert reconcile_missing_vm_records() == []
    assert listed.call_count == 1
    assert [c for c in api.calls if c[1] == ('cluster', 'resources')] \
        == [('GET', ('cluster', 'resources'), {'type': 'vm'})]
    forget_proxmox_caches()


# Catches an intervening present sighting failing to restart the count.
def test_reconcile_present_sighting_restarts_the_count(
        app, memory_backend_case, monkeypatch):
    case = memory_backend_case
    record = persist_case_record(case)
    launched = case.backend.instances.pop(record.vmid)
    key = (record.node, record.vmid)
    assert reconcile_missing_vm_records() == []
    monkeypatch.setitem(route_utils._missing_since, key, time.monotonic() - 301)
    case.backend.instances[record.vmid] = launched
    assert reconcile_missing_vm_records() == []
    case.backend.instances.pop(record.vmid)
    assert reconcile_missing_vm_records() == []
    assert db.session.get(VMRecord, record.id) is not None


# Catches an intervening unknown confirmation failing to restart the count.
def test_reconcile_unknown_confirmation_restarts_the_count(
        app, memory_backend_case, monkeypatch):
    case = memory_backend_case
    record = persist_case_record(case)
    case.backend.instances.pop(record.vmid)
    key = (record.node, record.vmid)
    assert reconcile_missing_vm_records() == []
    monkeypatch.setitem(route_utils._missing_since, key, time.monotonic() - 301)
    case.backend.inspect_result = GUEST_UNKNOWN
    assert reconcile_missing_vm_records() == []
    case.backend.inspect_result = None
    assert reconcile_missing_vm_records() == []
    assert db.session.get(VMRecord, record.id) is not None


# Catches the failed-deploy cloud sweep matching by name or by deployment
# alone: it must take the bundle's instance whatever the provider named it,
# and nothing that only shares a name or a deployment (C7).
def test_failed_deploy_cloud_sweep_matches_deployment_and_vmid_only(
        app, memory_backend_case):
    case = memory_backend_case
    backend, spec = case.backend, case.spec
    portal_name = spec.vm_name
    backend.ensure_slot(spec.network_slot)
    # The bundle's own instance, under a provider-side name.
    backend.create(replace(spec, vm_name=f'lp-site-{spec.vmid}'),
                   'memory-zone', heartbeat=lambda: None)
    # The bundle's deployment, but a VMID no result names.
    backend.create(replace(spec, vmid=spec.vmid + 1), 'memory-zone',
                   heartbeat=lambda: None)
    # The result's name, in another deployment.
    backend.create(replace(spec, vmid=spec.vmid + 2, deployment_id='e' * 32),
                   'memory-zone', heartbeat=lambda: None)
    result = {'vmid': spec.vmid, 'vm_name': portal_name, 'vm_type': 'aws',
              'node': 'aws', '_backend': 'aws', 'provider_id': None,
              'deployment_id': spec.deployment_id,
              'network_slot': spec.network_slot}

    destroyed = _cleanup_with_sweep(
        [result], templates=[_machine(case)], owning_identifier=spec.owner,
        backend_name='aws')

    assert destroyed == {spec.vmid}
    assert backend.destroyed == {spec.vmid: DestroyPolicy.BEST_EFFORT}
    assert set(backend.instances) == {spec.vmid + 1, spec.vmid + 2}
    assert (result['provider_id'], result['node']) == (
        f'provider-{spec.vmid}', 'memory-zone')


# Catches a cloud survivor recorded while the provider inventory was down
# (no provider id, no zone) being unrecoverable after a restart.
def test_survivor_recorded_during_cloud_outage_is_destroyed_after_restart(
        app, memory_backend_case):
    case = memory_backend_case
    outage = OperationalError('COMMIT', {}, Exception('server has gone away'))
    down = BackendError('inventory down')
    with patch.object(case.backend, 'list_managed', side_effect=down), \
            patch.object(case.backend, 'inspect', side_effect=down), \
            patch('app.routes.vm_creation.track_survivor', side_effect=outage), \
            pytest.raises(OperationalError):
        deploy_case_bundle(case, failure='after_launch')
    [vmid] = case.backend.instances
    with open(os.environ['UNTRACKED_SURVIVORS_FILE']) as handle:
        [line] = [json.loads(text) for text in handle if text.strip()]
    assert (line['vmid'], line['backend'], line['node'], line['provider_id']) \
        == (vmid, 'aws', 'aws', None)

    recover_untracked_survivors()   # what startup does after a restart
    survivor = VMRecord.query.filter_by(vmid=vmid).one()
    assert (survivor.backend, survivor.provider_id) == ('aws', None)

    run_one_expiry_pass(app)
    assert case.backend.destroyed == {vmid: DestroyPolicy.STRICT}
    assert vmid not in case.backend.instances
    assert VMRecord.query.filter_by(vmid=vmid).count() == 0


# Catches one entry whose provider raised aborting the cleanup of the rest.
@pytest.mark.parametrize('failure', ['session', 'inspect', 'confirm_absent'])
def test_cleanup_contains_a_failing_entry_and_cleans_the_next(
        app, memory_backend_case, monkeypatch, failure):
    case = memory_backend_case
    backend, spec = case.backend, case.spec
    backend.ensure_slot(spec.network_slot)
    first = backend.create(spec, 'memory-zone', heartbeat=lambda: None)
    second = backend.create(replace(spec, vmid=spec.vmid + 1), 'memory-zone',
                            heartbeat=lambda: None)
    entries = [{'vmid': launched.vmid, 'vm_name': spec.vm_name,
                'vm_type': 'aws', 'node': launched.location, 'backend': 'aws',
                'provider_id': launched.provider_id,
                'deployment_id': spec.deployment_id,
                'network_slot': launched.network_slot}
               for launched in (first, second)]
    down = BackendError('provider down')
    inspect, destroy = backend.inspect, backend.destroy

    if failure == 'session':
        opened = []

        @contextmanager
        def session():
            opened.append(True)
            if len(opened) == 1:
                raise down
            yield
        monkeypatch.setattr(backend, 'session', session, raising=False)
    elif failure == 'inspect':
        monkeypatch.setattr(backend, 'inspect', lambda ref: (
            (_ for _ in ()).throw(down) if ref.vmid == first.vmid
            else inspect(ref)))
    else:
        def failing_destroy(ref, policy):
            if ref.vmid == first.vmid:
                raise down
            destroy(ref, policy)

        def failing_confirm(ref, error=None):
            raise down
        monkeypatch.setattr(backend, 'destroy', failing_destroy)
        monkeypatch.setattr(backend, 'confirm_absent', failing_confirm)

    assert cleanup_vms(entries) == {second.vmid}
    assert first.vmid in backend.instances
    assert second.vmid not in backend.instances
