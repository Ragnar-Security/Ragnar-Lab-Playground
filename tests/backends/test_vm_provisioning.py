"""Backend-generic provisioning (app.vm_provisioning) and the bundle orchestrator.

The cloud half runs against the in-memory FakeBackend; the Proxmox half runs
the production ProxmoxBackend against the stateful FakeProxmox, so the clone
order, leases and NIC layout are read back from provider state.
"""

import importlib
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app import coordination, proxmox_service
from app.backends import register_backend_factory
from app.backends.base import BACKEND_PROXMOX, DestroyPolicy, VMRef, VMSpec
from app.backends.cloud_admission import reserve_cloud_capacity
from app.backends.cloud_common import router_lock_key, slot_lock_key
from app.backends.proxmox import ProxmoxBackend
from app.models import NodeMemoryReservation, VMIDReservation, VMRecord, db
from app.proxmox_service import (
    RESERVATION_TTL_SECONDS, _clear_node_reservations, _clear_vmid_reservations,
    get_next_available_vmid,
)
from app.routes import vm_creation
from app.routes.vm_creation import (
    MAX_CONCURRENT_CLONES, DeployFailed, create_single_vm_task, deploy_bundle,
    extract_template_config,
)
from app.tasks import _reset_router_idle_state_for_tests, _stop_idle_cloud_routers
from app.vm_provisioning import (
    after_record_retired, create_vm, destroy_vm, release_slot_if_unused,
)
from tests.backends.fake_backend import (
    MEMORY_LOCATION, FakeBackend, make_memory_backend_case,
)
from tests.backends.proxmox_case import forget_proxmox_caches
from tests.test_live_contract_guards import FakeProxmox

config = importlib.import_module('app.config')


# --- cloud: the generic create order -----------------------------------------

# Catches create holding the router lock (serialized class launches, stalled expiry) or
# launching before the router/slot exist (C4).
def test_cloud_create_locks_only_router_start_and_slot_ensure(app, memory_backend_case):
    case = memory_backend_case
    location, launched = create_vm(
        case.spec, reservation_id='reservation-a', load_balancing_config=None,
        heartbeat=lambda: None)
    assert location == launched.location
    assert case.backend.router_running
    assert launched.vmid in case.backend.instances
    assert case.backend.slot_networks == {case.spec.network_slot}
    assert case.backend.observed_locks == {
        'ensure_router_running': {router_lock_key('aws')},
        'ensure_slot': {slot_lock_key('aws', case.spec.network_slot)},
        'create': set(),
    }


# Catches a slot released between ensure_slot and create failing the launch (I3).
def test_create_re_ensures_a_slot_lost_before_launch_once(app, memory_backend_case):
    case = memory_backend_case
    case.backend.lose_slot_once = True
    _location, launched = create_vm(
        case.spec, reservation_id='reservation-b', load_balancing_config=None,
        heartbeat=lambda: None)
    assert launched.vmid in case.backend.instances
    assert case.spec.network_slot in case.backend.slot_networks


# Catches retirement deleting a slot an in-flight admission is about to launch into (I3).
def test_slot_release_waits_while_backend_has_unexpired_reservation(app, memory_backend_case):
    case = memory_backend_case
    case.backend.ensure_slot(case.spec.network_slot)
    with app.app_context():
        db.session.add(NodeMemoryReservation(
            reservation_id='inflight', node='aws', memory_bytes=1,
            expires_at=coordination.lease_clock() + 600))
        db.session.commit()
        release_slot_if_unused('aws', case.spec.network_slot)
        assert case.spec.network_slot in case.backend.slot_networks
        db.session.delete(db.session.get(NodeMemoryReservation, 'inflight'))
        db.session.commit()
        release_slot_if_unused('aws', case.spec.network_slot)
    assert case.spec.network_slot not in case.backend.slot_networks


# Catches generic provisioning releasing admission before failure cleanup owns the guest.
def test_create_failure_keeps_admission_reserved_for_caller(
        app, memory_backend_case):
    case = memory_backend_case
    case.backend.fail_create_after_launch = True
    with pytest.raises(RuntimeError, match='injected create failure'):
        create_vm(case.spec, reservation_id='reservation-failed',
                  load_balancing_config=None, heartbeat=lambda: None)
    assert 'reservation-failed' in case.backend.reservations
    assert case.spec.vmid in case.backend.instances


# Catches a cloud VM being committed without enough identity to destroy after restart.
def test_deploy_bundle_persists_backend_identity(app, memory_backend_case):
    case = memory_backend_case
    with app.app_context():
        records = deploy_bundle(
            deployment_group_name='Cloud Lab',
            templates=[{'name': 'vm', 'backend': 'aws', 'entry_point': True,
                        'aws': dict(case.spec.template)}],
            owning_identifier=case.spec.owner,
            entry_vlan=case.spec.entry_slot,
            internal_vlan=case.spec.internal_slot,
            uptime_minutes=60,
            user_id=None,
            backend_name='aws')
        record = records[0]
        assert record.backend == 'aws'
        assert record.provider_id == case.backend.instances[record.vmid].provider_id
        assert record.deployment_id
        assert record.network_slot == case.spec.network_slot
        assert record.vm_type == 'aws'
        assert record.node == 'memory-zone'


# Catches a cloud deploy_bundle constructing ProxmoxAPI or reading Proxmox nodes (I19).
# Lives here, not in tests/test_vmid_allocator.py, because memory_backend_case is a
# tests/backends/ fixture.
def test_cloud_bundle_on_cloud_only_portal_never_constructs_proxmox(
        app, memory_backend_case, monkeypatch):
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    with patch('app.proxmox_service.ProxmoxAPI',
               side_effect=AssertionError('ProxmoxAPI constructed')), app.app_context():
        records = deploy_bundle(
            deployment_group_name='Cloud Lab',
            templates=[{'name': 'vm', 'backend': 'aws', 'entry_point': True,
                        'aws': dict(memory_backend_case.spec.template)}],
            owning_identifier='test-owner', entry_vlan=101, internal_vlan=201,
            uptime_minutes=60, user_id=None, backend_name='aws')
        # Inside the context: the commit expired the rows, so they reload.
        assert [r.backend for r in records] == ['aws']


# Catches a successful cloud bundle keeping its admission or VMID leases, which
# would count against the cap until the TTL ran out.
def test_cloud_bundle_releases_admission_and_vmids_after_commit(
        app, memory_backend_case):
    case = memory_backend_case
    with app.app_context():
        deploy_bundle(
            deployment_group_name='Cloud Lab',
            templates=[{'name': 'vm', 'backend': 'aws', 'entry_point': True,
                        'aws': dict(case.spec.template)}],
            owning_identifier='test-owner', entry_vlan=101, internal_vlan=201,
            uptime_minutes=60, user_id=None, backend_name='aws')
        assert VMIDReservation.query.count() == 0
    assert case.backend.reservations == set()


# --- cloud: the admission lease keeps the router up (C4) ----------------------

class _LedgerBackend(FakeBackend):
    """FakeBackend admitting through the real cloud ledger, so the router idle
    step sees its reservation, and whose create blocks after the launch the
    way AWS's running waiter does: no heartbeat until it returns."""

    def __init__(self):
        super().__init__('aws')
        self.launching = threading.Event()
        self.release_launch = threading.Event()

    def admit(self, spec, *, reservation_id, load_balancing_config):
        return reserve_cloud_capacity('aws', MEMORY_LOCATION, 5, reservation_id)

    def renew_admission(self, reservation_id):
        return proxmox_service.renew_node_reservation(reservation_id)

    def release_admission(self, reservation_id):
        return proxmox_service.release_node_reservation(reservation_id)

    def create(self, spec, location, *, heartbeat):
        launched = super().create(spec, location, heartbeat=heartbeat)
        self.launching.set()
        assert self.release_launch.wait(timeout=10), 'launch never released'
        return launched


@pytest.fixture
def ledger_cloud(app, monkeypatch):
    """A cloud-only portal on a hand-driven lease clock whose router stops at
    the first idle observation, so only a live lease or record keeps it up."""
    clock = {'now': 1_000_000.0}
    monkeypatch.setattr(coordination, 'lease_clock', lambda: clock['now'])
    monkeypatch.setattr(vm_creation, 'RESERVATION_HEARTBEAT_SECONDS', 0.01)
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    monkeypatch.setattr(config, 'AWS_ROUTER_IDLE_MINUTES', 0)
    backend = _LedgerBackend()
    register_backend_factory('aws', lambda: backend)
    _reset_router_idle_state_for_tests()
    yield backend, clock
    backend.release_launch.set()
    _reset_router_idle_state_for_tests()


def _renewed_at(clock, timeout=2.0):
    """Wait until every aws admission lease was renewed at the clock's now."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.session.rollback()
        leases = NodeMemoryReservation.query.filter_by(node='aws').all()
        if leases and all(lease.expires_at == clock['now'] + RESERVATION_TTL_SECONDS
                          for lease in leases):
            return True
        time.sleep(0.01)
    return False


def _router_survives_past_ttl(backend, clock):
    """Let a full TTL and more pass in heartbeat-sized steps; run the real
    router idle step at each one."""
    for _ in range(4):  # 4 x 300 s > the 900 s admission TTL
        clock['now'] += 300
        _renewed_at(clock)
        assert _stop_idle_cloud_routers() == []
        assert backend.router_running, 'router stopped under a live admission'


# Catches the admission lease lapsing during a long create or before the
# VMRecord commits, which lets the idle step stop the router under a launch (C4).
def test_cloud_admission_keeps_router_up_from_admit_through_record_commit(
        app, ledger_cloud, monkeypatch):
    backend, clock = ledger_cloud
    staging = threading.Event()
    release_staging = threading.Event()
    stage = vm_creation.log_vm_to_database

    def slow_stage(*args, **kwargs):
        staging.set()
        assert release_staging.wait(timeout=10), 'staging never released'
        return stage(*args, **kwargs)

    monkeypatch.setattr(vm_creation, 'log_vm_to_database', slow_stage)
    outcome = {}

    def deploy():
        with app.app_context():
            try:
                outcome['vmids'] = [record.vmid for record in deploy_bundle(
                    deployment_group_name='Cloud Lab',
                    templates=[{'name': 'vm', 'backend': 'aws', 'entry_point': True,
                                'aws': {'image_id': 'ami-placeholder',
                                        'instance_type': 'small', 'disk_gb': 20}}],
                    owning_identifier='test-owner', entry_vlan=101,
                    internal_vlan=201, uptime_minutes=60, user_id=None,
                    backend_name='aws')]
            except Exception as error:  # surfaced by the assertion below
                outcome['error'] = error

    deployer = threading.Thread(target=deploy)
    deployer.start()
    try:
        with app.app_context():
            assert backend.launching.wait(timeout=5), 'create never launched'
            _router_survives_past_ttl(backend, clock)  # inside a long create
            backend.release_launch.set()
            assert staging.wait(timeout=5), 'bundle never staged its record'
            _router_survives_past_ttl(backend, clock)  # before the commit
            release_staging.set()
            deployer.join(timeout=10)
            assert 'error' not in outcome, outcome.get('error')
            assert len(outcome['vmids']) == 1
            assert NodeMemoryReservation.query.count() == 0
            # The committed record protects the router from here on.
            assert _stop_idle_cloud_routers() == []
            assert backend.router_running
    finally:
        backend.release_launch.set()
        release_staging.set()
        deployer.join(timeout=10)


# Catches a create whose admission lease was lost mid-launch carrying on as if
# admitted (I16): nothing then holds its router up or its capacity.
def test_cloud_create_fails_when_its_admission_lease_is_lost_mid_create(
        app, ledger_cloud):
    backend, _clock = ledger_cloud
    spec = make_memory_backend_case(app).spec
    lost = threading.Event()

    def heartbeat():
        if not backend.renew_admission('r-lost'):
            lost.set()
            raise RuntimeError('Admission reservation expired')

    errors = []

    def create():
        with app.app_context():
            try:
                create_vm(spec, reservation_id='r-lost',
                          load_balancing_config=None, heartbeat=heartbeat)
            except Exception as error:
                errors.append(error)

    creator = threading.Thread(target=create)
    creator.start()
    try:
        assert backend.launching.wait(timeout=5), 'create never launched'
        with app.app_context():
            NodeMemoryReservation.query.filter_by(reservation_id='r-lost').delete()
            db.session.commit()
        assert lost.wait(timeout=5), 'nothing renewed the lease during create'
    finally:
        backend.release_launch.set()
        creator.join(timeout=10)
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError), errors
    assert 'Admission reservation expired' in str(errors[0])
    # The launched instance is left for the caller's failed-create cleanup.
    assert spec.vmid in backend.instances


# --- cloud: retirement --------------------------------------------------------

def _cloud_record(case, vmid, **fields):
    values = dict(
        vmid=vmid, vm_name=f'aws-memory-{vmid}', vm_type='aws', node='memory-zone',
        template_name='Cloud Lab/vm', student_name='student-a',
        expires_at=datetime.now() + timedelta(hours=1), backend='aws',
        provider_id=f'provider-{vmid}', deployment_id=case.spec.deployment_id,
        network_slot=case.spec.network_slot)
    values.update(fields)
    return VMRecord(**values)


def _retire(vmid):
    record = VMRecord.query.filter_by(vmid=vmid).one()
    record.destroyed = True
    record.destroyed_at = datetime.now()
    db.session.commit()
    return VMRef.from_record(record)


# Catches deployment networks deleted while a sibling VM of the bundle still
# runs, and a slot kept after its last VM is gone.
def test_after_record_retired_cleans_up_only_after_the_last_record(
        app, memory_backend_case):
    case = memory_backend_case
    case.backend.ensure_slot(case.spec.network_slot)
    case.backend.deployment_networks.add(case.spec.deployment_id)
    with app.app_context():
        db.session.add_all([_cloud_record(case, 100), _cloud_record(case, 101)])
        db.session.commit()
        after_record_retired(_retire(100))
        assert case.spec.deployment_id in case.backend.deployment_networks
        assert case.spec.network_slot in case.backend.slot_networks
        after_record_retired(_retire(101))
    assert case.backend.deployment_networks == set()
    assert case.backend.slot_networks == set()


# Catches cleanup racing a bundle that is still allocating into the deployment.
def test_after_record_retired_keeps_deployment_with_live_vmid_reservation(
        app, memory_backend_case):
    case = memory_backend_case
    case.backend.deployment_networks.add(case.spec.deployment_id)
    with app.app_context():
        db.session.add(_cloud_record(case, 100))
        db.session.add(VMIDReservation(
            vmid=101, reservation_id=case.spec.deployment_id,
            expires_at=coordination.lease_clock() + 600))
        db.session.commit()
        after_record_retired(_retire(100))
    assert case.spec.deployment_id in case.backend.deployment_networks


# Catches a provider error during cleanup failing the retirement that already
# committed; the sweeper owns leftovers (T15).
def test_after_record_retired_never_raises(app, memory_backend_case):
    case = memory_backend_case
    with app.app_context():
        db.session.add(_cloud_record(case, 100))
        db.session.commit()
        ref = _retire(100)
        with patch.object(case.backend, 'cleanup_deployment',
                          side_effect=RuntimeError('provider down')), \
             patch.object(case.backend, 'release_slot',
                          side_effect=RuntimeError('provider down')):
            after_record_retired(ref)


# Catches destroy_vm adding policy of its own instead of dispatching by record.
def test_destroy_vm_dispatches_to_the_ref_backend(app, memory_backend_case):
    case = memory_backend_case
    case.backend.ensure_slot(case.spec.network_slot)
    location, launched = create_vm(
        case.spec, reservation_id='reservation-d', load_balancing_config=None,
        heartbeat=lambda: None)
    destroy_vm(case.ref(launched, location), DestroyPolicy.STRICT)
    assert case.backend.destroyed == {case.spec.vmid: DestroyPolicy.STRICT}
    assert case.spec.vmid not in case.backend.instances


# --- Proxmox: the preserved order, read back from FakeProxmox -----------------

NODE = 'node-p'
TEMPLATE_VMID = 9000
OSTEMPLATE = 'local:vztmpl/placeholder.tar.zst'


def _qemu(name, **extra):
    return dict({'name': name, 'type': 'qemu', 'memory': 1024, 'cores': 1,
                 'storage': 20, 'template_locations': {NODE: TEMPLATE_VMID}},
                **extra)


def _lxc(name, **extra):
    return dict({'name': name, 'type': 'lxc', 'memory': 512, 'cores': 1,
                 'storage': 8, 'ostemplate': OSTEMPLATE}, **extra)


@pytest.fixture
def proxmox_portal(app, monkeypatch):
    """A Proxmox-only portal whose every client is one FakeProxmox."""
    _clear_node_reservations()
    _clear_vmid_reservations()
    forget_proxmox_caches()
    api = FakeProxmox()
    api.add_guest(TEMPLATE_VMID, 'template-base', node=NODE, template=1)
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', (BACKEND_PROXMOX,))
    register_backend_factory(BACKEND_PROXMOX, lambda: ProxmoxBackend(client=api))
    # Failed-deploy cleanup still opens its own connection (T8 moves it).
    with patch('app.proxmox_service.ProxmoxAPI', return_value=api):
        yield api
    forget_proxmox_caches()
    _clear_node_reservations()
    _clear_vmid_reservations()


def _deploy(app, templates):
    with app.app_context():
        records = deploy_bundle(
            deployment_group_name='Proxmox Lab', templates=templates,
            owning_identifier='student-a', entry_vlan=101, internal_vlan=201,
            uptime_minutes=60, user_id=None)
        # The commit expired the rows; load them while a session exists.
        for record in records:
            db.session.refresh(record)
        return records


# Catches the generic path reordering commit before start, splitting the VMID
# block, or leaving leases behind after a successful bundle.
def test_proxmox_bundle_takes_contiguous_vmids_and_starts_before_commit(
        app, proxmox_portal):
    api = proxmox_portal
    status_when_staged = {}
    stage = vm_creation.log_vm_to_database

    def staged(vm_info, *args, **kwargs):
        status_when_staged[vm_info['vmid']] = api.guests[vm_info['vmid']]['status']
        return stage(vm_info, *args, **kwargs)

    templates = [_qemu('web', entry_point=True), _qemu('db'), _qemu('cache')]
    with patch('app.routes.vm_creation.log_vm_to_database', side_effect=staged):
        records = _deploy(app, templates)

    vmids = [record.vmid for record in records]
    assert vmids == list(range(vmids[0], vmids[0] + 3))
    assert status_when_staged == {vmid: 'running' for vmid in vmids}
    assert [(r.backend, r.provider_id, r.network_slot) for r in records] == [
        ('proxmox', None, 101), ('proxmox', None, 201), ('proxmox', None, 201)]
    assert len({r.deployment_id for r in records}) == 1
    assert records[0].deployment_id
    with app.app_context():
        assert NodeMemoryReservation.query.count() == 0
        assert VMIDReservation.query.count() == 0


# Catches the per-node ZFS clone limit being lost or bypassed in the adapter.
def test_proxmox_clone_semaphore_admits_exactly_the_limit(app, proxmox_portal):
    api = proxmox_portal
    guard = threading.Lock()
    state = {'active': 0, 'peak': 0}
    full = threading.Event()

    def clone_in_flight(_newid):
        with guard:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
            if state['active'] >= MAX_CONCURRENT_CLONES:
                full.set()
        # Hold the clone open until the limit is reached, so a third clone
        # that got past the semaphore would be counted.
        full.wait(timeout=1)
        with guard:
            state['active'] -= 1

    api.before_clone = clone_in_flight
    records = _deploy(app, [_qemu(f'vm{index}') for index in range(3)])

    assert len(records) == 3
    assert state['peak'] == MAX_CONCURRENT_CLONES


# Catches the generic heartbeat no longer stopping a create whose admission
# lease is gone, so expired capacity would be handed out twice.
def test_create_task_stops_when_admission_renewal_fails(app, proxmox_portal):
    api = proxmox_portal
    with patch('app.proxmox_service.renew_node_reservation', return_value=False):
        result = create_single_vm_task(
            1, _qemu('web'), 500, 'student-a', None, 101, 201,
            datetime.now() + timedelta(hours=1), {}, flask_app=app)
    assert result['success'] is False
    assert result['error'] == 'Admission reservation expired before clone'
    assert result['_backend'] == 'proxmox'
    assert 500 not in api.guests


def _proxmox_spec(template, vmid, deployment_id, *, entry=True):
    config_block = extract_template_config(template)
    return VMSpec(
        vmid=vmid, vm_name=f'student-student-a-{template["name"]}',
        display_name=template['name'], backend=BACKEND_PROXMOX,
        template=config_block, memory_mb=config_block['memory'],
        cores=config_block['cores'], storage_gb=config_block['storage'],
        is_entry_point=entry, entry_slot=101 if entry else None,
        internal_slot=201, expires_at=datetime.now() + timedelta(hours=1),
        deployment_id=deployment_id, owner='student-a')


# Catches a heartbeat point dropped from the adapter, or a failed one leaking
# the node's clone slot or starting the guest anyway.
@pytest.mark.parametrize('template,with_semaphore,fail_at,message,guest_left', [
    (_lxc('ct'), False, 1, 'before create', False),
    (_lxc('ct'), False, 2, 'before VM start', True),
    (_qemu('vm'), True, 1, 'while waiting for clone capacity', False),
    (_qemu('vm'), False, 1, 'before clone', False),
    (_qemu('vm'), True, 2, 'before VM start', True),
])
def test_proxmox_create_heartbeats_at_each_lease_point(
        app, proxmox_portal, template, with_semaphore, fail_at, message,
        guest_left):
    api = proxmox_portal
    semaphore = threading.Semaphore(MAX_CONCURRENT_CLONES)
    beats = []

    def heartbeat():
        beats.append(True)
        if len(beats) == fail_at:
            raise RuntimeError('Admission reservation expired')

    with app.app_context():
        vmid = get_next_available_vmid(api, count=1, reservation_id='hb-bundle')
        spec = _proxmox_spec(template, vmid, 'hb-bundle')
        with pytest.raises(RuntimeError) as raised:
            ProxmoxBackend(client=api).create(
                spec, NODE, heartbeat=heartbeat,
                clone_semaphores={NODE: semaphore} if with_semaphore else {})

    assert str(raised.value) == f'Admission reservation expired {message}'
    assert (vmid in api.guests) is guest_left
    if guest_left:
        assert api.guests[vmid]['status'] == 'stopped'
    assert all(semaphore.acquire(blocking=False)
               for _ in range(MAX_CONCURRENT_CLONES))


# Catches a lapsed VMID lease no longer stopping the create before the clone.
def test_proxmox_create_stops_when_vmid_renewal_fails(app, proxmox_portal):
    api = proxmox_portal
    with app.app_context():
        spec = _proxmox_spec(_qemu('vm'), 600, 'lapsed-bundle')
        with pytest.raises(RuntimeError, match='VMID reservation expired before clone'):
            ProxmoxBackend(client=api).create(spec, NODE, heartbeat=lambda: None)
    assert 600 not in api.guests


# Catches the defensive duplicate-VMID retry being lost, or retrying onto a
# VMID outside the bundle's reservation.
def test_proxmox_create_retries_a_clone_collision_once(app, proxmox_portal):
    api = proxmox_portal
    claims = []
    with app.app_context():
        vmid = get_next_available_vmid(api, count=1, reservation_id='collide')

        def squat(newid):
            if newid == vmid:
                api.add_guest(newid, 'squatter', node=NODE)

        api.before_clone = squat
        spec = _proxmox_spec(_qemu('vm'), vmid, 'collide')
        launched = ProxmoxBackend(client=api).create(
            spec, NODE, heartbeat=lambda: None,
            on_claim=lambda claimed, node: claims.append((claimed, node)))
        reserved = {row.vmid for row in VMIDReservation.query.filter_by(
            reservation_id='collide')}

    assert launched.vmid > vmid
    assert launched.vmid in reserved
    assert api.guests[vmid]['name'] == 'squatter'
    assert api.guests[launched.vmid]['name'] == spec.vm_name
    assert api.guests[launched.vmid]['status'] == 'running'
    assert claims == [(vmid, NODE), (None, None), (launched.vmid, NODE)]


# Catches the adapter wiring the owner's VLAN tags onto the wrong NICs.
@pytest.mark.parametrize('template,entry,expected', [
    (_qemu('vm', entry_point=True), True,
     {'net0': 'virtio,bridge=vmbr0,tag=101', 'net1': 'virtio,bridge=vmbr0,tag=201'}),
    (_qemu('vm'), False,
     {'net0': 'virtio,bridge=vmbr0,tag=201', 'delete': 'net1'}),
    (_qemu('vm', entry_point=True, internal_nic=False), True,
     {'net0': 'virtio,bridge=vmbr0,tag=101', 'delete': 'net1'}),
    (_lxc('ct', entry_point=True), True,
     {'net0': 'name=eth0,bridge=vmbr0,tag=101,ip=dhcp',
      'net1': 'name=eth1,bridge=vmbr0,tag=201,ip=dhcp'}),
    (_lxc('ct'), False, {'net0': 'name=eth0,bridge=vmbr0,tag=201,ip=dhcp'}),
])
def test_proxmox_bundle_nic_layout(app, proxmox_portal, template, entry, expected):
    api = proxmox_portal
    [record] = _deploy(app, [template])
    guest_config = api.guests[record.vmid]['config']
    for key in ('net0', 'net1', 'delete'):
        assert guest_config.get(key) == expected.get(key), key
    assert record.network_slot == (101 if entry else 201)


# Catches a failed VMRecord commit leaving half a bundle recorded, or its
# guests and leases behind.
def test_proxmox_bundle_rolls_back_atomically_when_staging_fails(app, proxmox_portal):
    api = proxmox_portal
    stage = vm_creation.log_vm_to_database
    staged = []

    def fail_second(vm_info, *args, **kwargs):
        staged.append(vm_info['vmid'])
        if len(staged) == 2:
            raise RuntimeError('database refused the row')
        return stage(vm_info, *args, **kwargs)

    with patch('app.routes.vm_creation.log_vm_to_database', side_effect=fail_second):
        with pytest.raises(DeployFailed, match='DB logging failed'):
            _deploy(app, [_qemu('web'), _qemu('db')])

    assert set(api.guests) == {TEMPLATE_VMID}
    with app.app_context():
        assert VMRecord.query.count() == 0
        assert NodeMemoryReservation.query.count() == 0
        assert VMIDReservation.query.count() == 0


# Catches admission or VMID leases released before the records that replace
# them are committed, which would let a concurrent deploy over-admit.
def test_proxmox_bundle_releases_leases_only_after_commit(app, proxmox_portal):
    from app.proxmox_service import release_node_reservation, release_vmid_reservation
    seen = []

    def node_release(reservation_id):
        seen.append(('node', VMRecord.query.filter_by(destroyed=False).count()))
        return release_node_reservation(reservation_id)

    def vmid_release(reservation_id):
        seen.append(('vmid', VMRecord.query.filter_by(destroyed=False).count()))
        return release_vmid_reservation(reservation_id)

    with patch('app.proxmox_service.release_node_reservation', side_effect=node_release), \
         patch('app.routes.vm_creation.release_vmid_reservation', side_effect=vmid_release):
        _deploy(app, [_qemu('web'), _qemu('db')])

    assert seen == [('node', 2), ('node', 2), ('vmid', 2)]


# Catches a nested `proxmox:` block being ignored by the create path (T4).
def test_proxmox_bundle_resolves_the_backend_block(app, proxmox_portal):
    api = proxmox_portal
    template = _qemu('vm', memory=1024, proxmox={'memory': 2048})
    [record] = _deploy(app, [template])
    assert record.memory == 2048
    assert api.guests[record.vmid]['config']['memory'] == 2048


# --- Proxmox: one connection per operation (fix round 1) ----------------------
# proxmoxer cannot re-authenticate a password session whose ticket expired, so
# a cached client would go permanently dead after an idle spell. The patch
# boundary app.proxmox_service.ProxmoxAPI is the contract: every operation
# constructs its own client there.

class _Constructions:
    """Stands in for the ProxmoxAPI class; each call is a new connection."""

    def __init__(self, *apis):
        self.apis = list(apis)
        self.count = 0

    def __call__(self, *args, **kwargs):
        api = self.apis[min(self.count, len(self.apis) - 1)]
        self.count += 1
        return api


def _cluster_with_guest():
    forget_proxmox_caches()
    api = FakeProxmox()
    api.add_guest(100, 'student-a-vm', node=NODE, status='running')
    api.add_guest(TEMPLATE_VMID, 'template-base', node=NODE, template=1)
    return api


# Catches a backend that reuses one HTTP client across operations.
def test_each_proxmox_operation_constructs_its_own_client(app):
    api = _cluster_with_guest()
    ref = VMRef(vmid=100, vm_name='student-a-vm', vm_type='qemu',
                location=NODE, backend=BACKEND_PROXMOX)
    backend = ProxmoxBackend()
    constructions = _Constructions(api)
    with patch('app.proxmox_service.ProxmoxAPI', constructions):
        assert backend.inspect(ref) == 'match'
        assert constructions.count == 1
        backend.list_managed()
        assert constructions.count == 2
        # One operation, however many reads and mutations it makes inside.
        backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
        assert constructions.count == 3
    assert 100 not in api.guests
    forget_proxmox_caches()


# Catches an operation that dies for good once a previous client's session
# can no longer authenticate.
def test_operation_after_a_dead_client_uses_a_new_one(app):
    from proxmoxer.core import AuthenticationError
    dead = FakeProxmox()
    dead.cluster_error = AuthenticationError("Couldn't authenticate user")
    live = _cluster_with_guest()
    backend = ProxmoxBackend()
    with patch('app.proxmox_service.ProxmoxAPI', _Constructions(dead, live)):
        with pytest.raises(AuthenticationError):
            backend.list_managed()
        assert [r.vmid for r in backend.list_managed()] == [100, TEMPLATE_VMID]
    forget_proxmox_caches()


# Catches the bundle path sharing one client between deploys or create tasks,
# or opening more than the deploy's inventory connection plus one per task.
def test_proxmox_bundle_opens_one_client_per_deploy_and_per_create_task(
        app, monkeypatch):
    _clear_node_reservations()
    _clear_vmid_reservations()
    api = _cluster_with_guest()
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', (BACKEND_PROXMOX,))
    constructions = _Constructions(api)
    with patch('app.proxmox_service.ProxmoxAPI', constructions):
        _deploy(app, [_qemu('web'), _qemu('db')])
        assert constructions.count == 3
        _deploy(app, [_lxc('ct')])
        assert constructions.count == 5
    forget_proxmox_caches()
    _clear_node_reservations()
    _clear_vmid_reservations()
