"""GCPBackend instance lifecycle and capped admission against the stateful
FakeGCESession.

Every provider assertion reads the fake's dictionaries and resource fields;
no test asserts the order of requests.
"""

import importlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import requests

from app import coordination
from app.backends import get_backend
from app.backends import gcp as gcp_module
from app.backends.base import (
    BACKEND_GCP, GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, GUEST_UNKNOWN,
    AdmissionRefused, BackendError, DestroyPolicy, DestroyRefused, LaunchedVM,
    ManagedResource, VMRef, VMSpec,
)
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    expires_epoch,
)
from app.backends.gcp import GCPBackend, OperationFailed, _request_id
from app.models import NodeMemoryReservation, VMRecord, db
from app.routes.vm_creation import named_instances
from tests.backends.fake_gce import CREATION_TIMESTAMP, FakeGCESession

PROJECT = 'example-project'
REGION = 'us-central1'
ZONE = 'us-central1-a'
SITE = 'test-site'
DEPLOYMENT = '0123456789abcdef0123456789abcdef'
VMID = 101
SLOT = 100
IMAGE = 'projects/example-project/global/images/lab-image'
NAME = 'lp-test-site-101'
SUBNET = 'lp-test-site-s-100'
GCP_TEST_CONFIG = {
    'LAB_SITE_ID': SITE, 'GCP_PROJECT': PROJECT, 'GCP_REGION': REGION,
    'GCP_ZONE': ZONE, 'GCP_NETWORK': 'lab-network', 'GCP_MAX_VMS': 2,
    'GCP_OPERATION_TIMEOUT_SECONDS': 10,
}
INSTANCE_PATH = r'zones/[^/]+/instances/[^/]+'


@pytest.fixture
def gce():
    session = FakeGCESession(PROJECT)
    session.add_subnetwork(REGION, SUBNET, '172.21.100.0/24')
    return session


@pytest.fixture
def backend(app, monkeypatch, gce):
    config = importlib.import_module('app.config')
    for key, value in GCP_TEST_CONFIG.items():
        monkeypatch.setattr(config, key, value)
    with app.app_context():
        yield GCPBackend(session=gce)


@pytest.fixture
def spec():
    return VMSpec(
        vmid=VMID, vm_name='test-gcp-101', display_name='Test GCP VM',
        backend=BACKEND_GCP,
        template={'image': IMAGE, 'machine_type': 'e2-small', 'disk_gb': 30},
        memory_mb=2048, cores=1, storage_gb=30, is_entry_point=True,
        entry_slot=SLOT, internal_slot=200,
        expires_at=datetime.now() + timedelta(hours=1),
        deployment_id=DEPLOYMENT, owner='test-owner')


def _ref(spec, launched):
    return VMRef(vmid=spec.vmid, vm_name=spec.vm_name, vm_type=BACKEND_GCP,
                 location=launched.location, backend=BACKEND_GCP,
                 provider_id=launched.provider_id, deployment_id=spec.deployment_id,
                 network_slot=launched.network_slot)


def _launch(backend, spec):
    return _ref(spec, backend.create(spec, ZONE, heartbeat=lambda: None))


def _labels(gce):
    return dict(gce.instances[(ZONE, NAME)]['labels'])


# Create

def test_create_has_no_external_ip_and_owns_disk_labels_and_tags(backend, gce, spec):
    # Catches a public-IP or detached-boot-disk billing leak in the insert body.
    launched = backend.create(spec, ZONE, heartbeat=lambda: None)
    instance = gce.instances[(ZONE, 'lp-test-site-101')]
    assert launched == LaunchedVM(101, 'gcp', ZONE, 100, 'lp-test-site-101')
    assert instance['disks'][0]['boot'] is True
    assert instance['disks'][0]['autoDelete'] is True
    assert instance['disks'][0]['initializeParams'] == {
        'sourceImage': 'projects/example-project/global/images/lab-image',
        'diskSizeGb': '30',
    }
    nic = instance['networkInterfaces'][0]
    assert nic['subnetwork'].endswith('/regions/us-central1/subnetworks/lp-test-site-s-100')
    assert 'accessConfigs' not in nic
    assert instance['labels'] == {
        TAG_SITE: SITE, TAG_VMID: '101', TAG_DEPLOYMENT: spec.deployment_id,
        TAG_EXPIRES: str(expires_epoch(spec.expires_at)), TAG_SLOT: '100',
        TAG_KIND: 'instance',
    }
    assert instance['tags']['items'] == [
        'lp-test-site-d-0123456789abcdef', 'lp-test-site-lab']


def test_create_body_is_one_private_disposable_machine(backend, gce, spec):
    # Catches an insert body growing a second disk, NIC or public address,
    # or launching into another project's subnetwork.
    backend.create(spec, ZONE, heartbeat=lambda: None)
    instance = gce.instances[(ZONE, NAME)]
    assert instance['machineType'] == f'zones/{ZONE}/machineTypes/e2-small'
    assert len(instance['disks']) == 1 and len(instance['networkInterfaces']) == 1
    assert instance['networkInterfaces'][0]['subnetwork'] == (
        f'projects/{PROJECT}/regions/{REGION}/subnetworks/{SUBNET}')


def test_create_disk_size_defaults_to_twenty_gb(backend, gce, spec):
    # Catches a template without disk_gb launching with a provider-chosen disk.
    template = {'image': IMAGE, 'machine_type': 'e2-small'}
    backend.create(replace(spec, template=template), ZONE, heartbeat=lambda: None)
    assert gce.instances[(ZONE, NAME)]['disks'][0]['initializeParams']['diskSizeGb'] == '20'


def test_create_retry_uses_request_id_once(backend, gce, spec):
    # Catches a retried insert starting a second billing instance.
    first = backend.create(spec, ZONE, heartbeat=lambda: None)
    second = backend.create(spec, ZONE, heartbeat=lambda: None)
    assert list(gce.instances) == [(ZONE, 'lp-test-site-101')]
    assert first.provider_id == second.provider_id == NAME
    request_id = _request_id('instance-insert', DEPLOYMENT, VMID)
    assert ('POST', f'zones/{ZONE}/instances', request_id) in gce.request_results


def test_request_ids_are_stable_nonzero_rfc4122_uuids():
    # Catches requestId changing across processes (Python's randomized
    # hash()) so a retry is not deduplicated, or a value Compute rejects.
    value = _request_id('instance-insert', DEPLOYMENT, VMID)
    parsed = uuid.UUID(value)
    assert value == _request_id('instance-insert', DEPLOYMENT, VMID)
    assert parsed.version == 5 and parsed.variant == uuid.RFC_4122 and parsed.int != 0
    assert value != _request_id('instance-insert', DEPLOYMENT, VMID + 1)
    assert value != _request_id('instance-delete', DEPLOYMENT, VMID)


def test_create_heartbeats_while_the_operation_runs(backend, gce, spec):
    # Catches a slow insert letting the admission and VMID leases expire.
    seen = []
    gce.stall_operations = True

    def heartbeat():
        seen.append(len(gce.instances))
        if len(seen) == 2:  # the first beat inside the wait loop
            gce.stall_operations = False  # so the next /wait completes the insert

    launched = backend.create(spec, ZONE, heartbeat=heartbeat)
    assert launched.provider_id == NAME
    # Before the insert, between waits, and after the instance exists.
    assert seen == [0, 0, 1]


def test_create_into_a_missing_subnetwork_raises(backend, gce, spec):
    # Catches a refused insert being reported as a launched VM.
    del gce.subnetworks[(REGION, SUBNET)]
    with pytest.raises(BackendError):
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert gce.instances == {}


# Identity, status and addresses

def test_get_operations_map_identity_status_and_private_ips(backend, gce, spec):
    # Catches a relabelled instance passing identity, or a public address
    # reaching the UI as the VM's IP.
    ref = _launch(backend, spec)
    instance = gce.instances[(ZONE, NAME)]
    assert backend.inspect(ref) == GUEST_MATCH
    assert backend.status(ref) == 'running'
    instance['networkInterfaces'][0]['accessConfigs'] = [{'natIP': '203.0.113.9'}]
    assert backend.ip_addresses(ref) == [instance['networkInterfaces'][0]['networkIP']]
    assert backend.ip_addresses(ref) == ['172.21.100.2']
    instance['labels'][TAG_VMID] = '999'
    assert backend.inspect(ref) == GUEST_MISMATCH
    assert backend.status(ref) == 'unknown'
    assert backend.ip_addresses(ref) == []


@pytest.mark.parametrize('label, value', [
    (TAG_SITE, 'other-site'), (TAG_VMID, '999'), (TAG_DEPLOYMENT, 'f' * 32),
    (TAG_SLOT, '7'), (TAG_KIND, 'router'),
])
def test_inspect_checks_every_identity_label(backend, gce, spec, label, value):
    # Catches a relabelled instance passing identity on the label not compared.
    ref = _launch(backend, spec)
    gce.instances[(ZONE, NAME)]['labels'][label] = value
    assert backend.inspect(ref) == GUEST_MISMATCH


@pytest.mark.parametrize('provider_state, status', [
    ('RUNNING', 'running'), ('TERMINATED', 'stopped'), ('PROVISIONING', 'starting'),
    ('STAGING', 'starting'), ('SUSPENDED', 'suspended'), ('STOPPING', 'stopping'),
])
def test_status_maps_provider_states(backend, gce, spec, provider_state, status):
    # Catches the UI showing a raw provider enum or a stopped VM as running.
    ref = _launch(backend, spec)
    gce.instances[(ZONE, NAME)]['status'] = provider_state
    assert backend.status(ref) == status


def test_ref_without_provider_id_uses_the_derived_instance_name(backend, gce, spec):
    # Catches a launch-then-crash survivor never being destroyed.
    ref = _launch(backend, spec)
    # A survivor may carry location='gcp' (T8): it names no zone.
    survivor = replace(ref, provider_id=None, location='gcp')
    assert backend.inspect(survivor) == GUEST_MATCH
    backend.destroy(survivor, DestroyPolicy.BEST_EFFORT)
    assert (ZONE, 'lp-test-site-101') not in gce.instances
    assert backend.confirm_absent(survivor) is True


def test_absent_is_positive_but_transport_failure_is_unknown(backend, gce, spec):
    # Catches a transient Compute failure retiring a record whose VM still bills.
    ref = _launch(backend, spec)
    gce.fail('GET', INSTANCE_PATH, status=500, times=2)
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False
    del gce.instances[(ZONE, NAME)]
    assert backend.inspect(ref) == GUEST_ABSENT
    assert backend.confirm_absent(ref) is True


@pytest.mark.parametrize('fault', [
    {'status': 403}, {'status': 429}, {'status': 503},
    {'invalid_json': True}, {'transport': True},
])
def test_every_ambiguous_read_is_unknown_never_absent(backend, gce, spec, fault):
    # Catches a 403/429/5xx, garbage body or dropped connection read as absence.
    ref = _launch(backend, spec)
    gce.fail('GET', INSTANCE_PATH, times=2, **fault)
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False


def test_confirm_absent_trusts_only_a_fresh_404(backend, gce, spec):
    # Catches a 404 from some other call (a subnetwork, an operation)
    # retiring a record whose instance is still there.
    ref = _launch(backend, spec)
    stale = requests.HTTPError('404', response=type('R', (), {'status_code': 404})())
    assert backend.confirm_absent(ref, stale) is False
    del gce.instances[(ZONE, NAME)]
    assert backend.confirm_absent(ref, BackendError('delete failed')) is True


def test_malformed_provider_id_is_unknown_not_absent(backend, gce, spec):
    # Catches a garbled provider id 404ing and retiring a live record.
    ref = _launch(backend, spec)
    broken = replace(ref, provider_id='Not/A-Name')
    assert backend.inspect(broken) == GUEST_UNKNOWN
    assert backend.confirm_absent(broken) is False


# Destroy

@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_destroy_all_policies_delete_the_instance_and_auto_deleted_disk(
        backend, gce, spec, policy):
    # Catches a policy that leaves the instance (and its boot disk) billing.
    ref = _launch(backend, spec)
    assert gce.instances[(ZONE, NAME)]['disks'][0]['autoDelete'] is True
    backend.destroy(ref, policy)
    assert gce.instances == {}
    assert backend.inspect(ref) == GUEST_ABSENT


def test_destroy_refuses_mismatch_raises_unknown_and_accepts_absence(backend, gce, spec):
    # Catches a stale destroy deleting an instance another record or portal
    # owns now, or an unverifiable identity being read as permission.
    ref = _launch(backend, spec)
    gce.instances[(ZONE, NAME)]['labels'][TAG_DEPLOYMENT] = 'f' * 32
    with pytest.raises(DestroyRefused):
        backend.destroy(ref, DestroyPolicy.FORCE_CORRUPT)
    assert (ZONE, NAME) in gce.instances
    gce.instances[(ZONE, NAME)]['labels'][TAG_DEPLOYMENT] = DEPLOYMENT
    gce.fail('GET', INSTANCE_PATH, status=500)
    with pytest.raises(BackendError) as raised:
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert not isinstance(raised.value, DestroyRefused)
    assert (ZONE, NAME) in gce.instances
    del gce.instances[(ZONE, NAME)]
    backend.destroy(ref, DestroyPolicy.STRICT)  # absent: idempotent success


def test_destroy_delete_failure_raises(backend, gce, spec):
    # Catches a refused delete being reported as a destroyed VM.
    ref = _launch(backend, spec)
    gce.fail('DELETE', INSTANCE_PATH, status=500)
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert (ZONE, NAME) in gce.instances


# Expiry

def test_set_expiry_retries_one_412_and_preserves_labels(backend, gce, spec):
    # Catches a lost fingerprint race failing the extend, or the retry
    # dropping a label another writer set meanwhile.
    ref = _launch(backend, spec)
    gce.instances[(ZONE, NAME)]['labels']['operator-note'] = 'keep'
    gce.label_conflicts_remaining = 1
    gce.concurrent_labels = {'concurrent-writer': 'yes'}
    before = _labels(gce)
    new_expiry = spec.expires_at + timedelta(hours=2)
    backend.set_expiry(ref, new_expiry)
    after = _labels(gce)
    assert after[TAG_EXPIRES] == str(expires_epoch(new_expiry))
    assert {k: v for k, v in after.items() if k != TAG_EXPIRES} == dict(
        {k: v for k, v in before.items() if k != TAG_EXPIRES}, **{'concurrent-writer': 'yes'})
    assert gce.label_conflicts_remaining == 0


@pytest.mark.parametrize('conflicts, succeeds', [(2, True), (3, False)])
def test_set_expiry_stops_after_three_conflicts(backend, gce, spec, conflicts, succeeds):
    # Catches an unbounded retry loop, or a persistent conflict reported as success.
    ref = _launch(backend, spec)
    original = _labels(gce)[TAG_EXPIRES]
    new_expiry = spec.expires_at + timedelta(hours=2)
    gce.label_conflicts_remaining = conflicts
    if succeeds:
        backend.set_expiry(ref, new_expiry)
        assert _labels(gce)[TAG_EXPIRES] == str(expires_epoch(new_expiry))
    else:
        with pytest.raises(BackendError):
            backend.set_expiry(ref, new_expiry)
        assert _labels(gce)[TAG_EXPIRES] == original
    assert gce.label_conflicts_remaining == 0


@pytest.mark.parametrize('method, path', [
    ('GET', INSTANCE_PATH), ('POST', INSTANCE_PATH + '/setLabels'),
])
def test_set_expiry_transport_failure_raises_backend_error(backend, gce, spec, method, path):
    # Catches a failed provider label update being counted as extended (contract I2).
    ref = _launch(backend, spec)
    original = _labels(gce)
    gce.fail(method, path, status=500)
    with pytest.raises(BackendError):
        backend.set_expiry(ref, spec.expires_at + timedelta(hours=2))
    assert _labels(gce) == original


def test_set_expiry_refuses_a_relabelled_instance_without_mutation(backend, gce, spec):
    # Catches an extend relabelling an instance this record no longer owns.
    ref = _launch(backend, spec)
    gce.instances[(ZONE, NAME)]['labels'][TAG_DEPLOYMENT] = 'f' * 32
    original = dict(gce.instances[(ZONE, NAME)])
    with pytest.raises(BackendError):
        backend.set_expiry(ref, spec.expires_at + timedelta(hours=2))
    assert gce.instances[(ZONE, NAME)] == original
    del gce.instances[(ZONE, NAME)]
    with pytest.raises(BackendError):
        backend.set_expiry(ref, spec.expires_at + timedelta(hours=2))


# Inventory

def test_list_managed_pages_and_filters_foreign_labels(backend, gce, spec):
    # Catches a loose provider filter conferring ownership, or pagination
    # hiding an owned VM from the sweeper.
    ref = _launch(backend, spec)
    gce.add_instance(ZONE, 'lp-other-site-5', subnetwork=(REGION, SUBNET),
                     labels={TAG_SITE: 'other-site', TAG_VMID: '5', TAG_KIND: 'instance'})
    gce.add_instance(ZONE, 'lp-test-site-router', subnetwork=(REGION, SUBNET),
                     labels={TAG_SITE: SITE, TAG_KIND: 'router'})
    gce.page_size = 1
    gce.loose_filter = True
    expected = ManagedResource(
        backend='gcp', kind='instance', provider_id=NAME, name=spec.vm_name,
        vmid=VMID, location=ZONE, deployment_id=DEPLOYMENT, network_slot=SLOT,
        expires_at_epoch=float(expires_epoch(spec.expires_at)),
        created_at_epoch=datetime.fromisoformat(CREATION_TIMESTAMP).timestamp(),
        status='running')
    assert backend.list_managed() == [expected]
    gce.loose_filter = False
    assert backend.list_managed() == [expected]
    assert ref.provider_id == expected.provider_id


def test_list_managed_keeps_malformed_owned_instances(backend, gce, spec):
    # Catches an owned instance with garbled labels vanishing from the
    # sweeper instead of being listed with unknown fields.
    gce.add_instance(ZONE, 'lp-test-site-7', subnetwork=(REGION, SUBNET), labels={
        TAG_SITE: SITE, TAG_KIND: 'instance', TAG_VMID: 'seven',
        TAG_SLOT: '-1', TAG_EXPIRES: '12x'})
    gce.instances[(ZONE, 'lp-test-site-7')]['creationTimestamp'] = 'yesterday'
    [resource] = backend.list_managed()
    assert (resource.provider_id, resource.vmid, resource.network_slot,
            resource.expires_at_epoch, resource.created_at_epoch, resource.deployment_id) == (
        'lp-test-site-7', None, None, None, None, None)


def test_list_managed_names_round_trip_to_the_portal_vm_name(backend, gce, spec):
    # Catches /create pre-flight missing an existing GCP VM because its
    # inventory name is not the portal's vm_name (labels cannot hold it).
    _launch(backend, spec)
    assert gce.instances[(ZONE, NAME)]['description'] == spec.vm_name
    [managed] = backend.list_managed()
    assert managed.name == spec.vm_name
    assert named_instances(backend, 'test-gcp-') == [
        {'vmid': VMID, 'node': ZONE, 'vm_type': 'gcp', 'name': spec.vm_name}]


@pytest.mark.parametrize('fault', [{'status': 500}, {'invalid_json': True}, {'transport': True}])
def test_list_managed_failure_raises_never_empty(backend, gce, spec, fault):
    # Catches an inventory outage read as "nothing exists" by the sweeper.
    _launch(backend, spec)
    gce.fail('GET', r'zones/[^/]+/instances', **fault)
    with pytest.raises(BackendError):
        backend.list_managed()


def _listed(backend):
    [resource] = [r for r in backend.list_managed() if r.provider_id == NAME]
    return resource


def test_delete_resource_deletes_a_listed_instance_and_rejects_other_kinds(backend, gce, spec):
    # Catches the sweeper's delete leaving a listed instance billing, or
    # dispatching a kind or backend it does not own.
    _launch(backend, spec)
    resource = _listed(backend)
    with pytest.raises(BackendError):
        backend.delete_resource(replace(resource, backend='aws'))
    with pytest.raises(BackendError):
        backend.delete_resource(replace(resource, kind='volume'))
    assert (ZONE, NAME) in gce.instances
    backend.delete_resource(resource)
    assert gce.instances == {}
    backend.delete_resource(resource)  # absent: done


@pytest.mark.parametrize('label, value', [
    (TAG_SITE, 'other-site'), (TAG_KIND, 'router'), (TAG_VMID, '999'),
    (TAG_DEPLOYMENT, 'f' * 32),
])
def test_delete_resource_refuses_an_instance_relabelled_since_listing(
        backend, gce, spec, label, value):
    # Catches a sweep deleting an instance another portal or record owns now.
    _launch(backend, spec)
    resource = _listed(backend)
    gce.instances[(ZONE, NAME)]['labels'][label] = value
    with pytest.raises(DestroyRefused):
        backend.delete_resource(resource)
    assert (ZONE, NAME) in gce.instances


def test_delete_resource_propagates_unknown_identity(backend, gce, spec):
    # Catches a read failure during a sweep being read as permission to delete.
    _launch(backend, spec)
    resource = _listed(backend)
    gce.fail('GET', INSTANCE_PATH, status=503)
    with pytest.raises(BackendError) as raised:
        backend.delete_resource(resource)
    assert not isinstance(raised.value, DestroyRefused)
    assert (ZONE, NAME) in gce.instances


# Operations

def test_operation_error_and_timeout_never_report_success(backend, gce, spec, monkeypatch):
    # Catches a failed or unfinished Compute operation read as a launched or
    # destroyed VM, and evidence of the failure being dropped.
    gce.next_operation_error = {'code': 'QUOTA_EXCEEDED', 'message': 'CPUS quota exceeded',
                                'location': 'zones/us-central1-a'}
    with pytest.raises(OperationFailed) as raised:
        backend.create(replace(spec, vmid=103), ZONE, heartbeat=lambda: None)
    assert isinstance(raised.value, BackendError)
    assert 'QUOTA_EXCEEDED' in str(raised.value) and 'CPUS quota exceeded' in str(raised.value)
    assert 'zones/us-central1-a' in str(raised.value)
    assert raised.value.operation['error']['errors'][0]['code'] == 'QUOTA_EXCEEDED'
    assert raised.value.operation['status'] == 'DONE'
    assert gce.instances == {}

    ref = _launch(backend, spec)
    gce.next_operation_error = {'code': 'RESOURCE_NOT_READY', 'message': 'busy'}
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert (ZONE, NAME) in gce.instances

    now = [0.0]

    def clock():
        now[0] += 4.0
        return now[0]

    monkeypatch.setattr(gcp_module, '_clock', clock)
    gce.stall_operations = True
    beats = []
    with pytest.raises(BackendError) as raised:
        backend.create(replace(spec, vmid=102), ZONE, heartbeat=lambda: beats.append(1))
    assert not isinstance(raised.value, OperationFailed)
    assert len(beats) >= 2
    assert (ZONE, 'lp-test-site-102') not in gce.instances
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)


def test_operation_wait_failure_raises(backend, gce, spec):
    # Catches a lost /wait response being read as a finished operation.
    gce.stall_operations = True
    gce.fail('POST', r'zones/[^/]+/operations/[^/]+/wait', status=500)
    with pytest.raises(BackendError):
        backend.create(spec, ZONE, heartbeat=lambda: None)


# Admission

def test_admit_counts_live_records_and_pending_reservations_at_the_cap(app, backend, spec):
    # Catches concurrent creates exceeding the mandatory GCP_MAX_VMS cap, or
    # stale and cross-cloud leases consuming it.
    now = coordination.lease_clock()
    db.session.add(VMRecord(
        vmid=699, vm_name='test-gcp-699', vm_type='gcp', node=ZONE,
        template_name='test/gcp', student_name='test-owner',
        memory=1024, cores=1, storage=20, is_entry_point=True,
        expires_at=spec.expires_at, destroyed=False, backend='gcp',
        provider_id='lp-test-site-699', deployment_id='c' * 32, network_slot=SLOT))
    db.session.add_all([
        NodeMemoryReservation(reservation_id='expired-gcp', node='gcp',
                              memory_bytes=1, expires_at=now - 1),
        NodeMemoryReservation(reservation_id='live-aws', node='aws',
                              memory_bytes=1, expires_at=now + 600),
    ])
    db.session.commit()
    assert backend.admit(spec, reservation_id='r1', load_balancing_config=None) == ZONE
    assert NodeMemoryReservation.query.filter_by(
        reservation_id='r1', node='gcp', memory_bytes=1).one()
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='r2', load_balancing_config=None)
    assert db.session.get(NodeMemoryReservation, 'expired-gcp') is None
    assert db.session.get(NodeMemoryReservation, 'live-aws') is not None


def test_release_and_renew_admission_use_the_shared_lease(app, backend, spec):
    # Catches a GCP lease that cannot be renewed during a slow create or is
    # never released, holding the cap after the VM is gone.
    backend.admit(spec, reservation_id='r1', load_balancing_config=None)
    backend.admit(spec, reservation_id='r2', load_balancing_config=None)
    with pytest.raises(AdmissionRefused):
        backend.admit(spec, reservation_id='r3', load_balancing_config=None)
    assert backend.renew_admission('r1') is True
    assert backend.release_admission('r1') is True
    assert backend.release_admission('r1') is False
    assert backend.admit(spec, reservation_id='r3', load_balancing_config=None) == ZONE
    assert backend.release_admission('r2') is True
    assert backend.release_admission('r3') is True


# Registry

def test_get_backend_gcp_builds_one_authorized_session(app, monkeypatch):
    # Catches the registry building a session per call, GCP staying
    # unregistered, or credentials being discovered with the wrong scope.
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from google.auth.transport.requests import AuthorizedSession

    config = importlib.import_module('app.config')
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('gcp',))
    calls = []

    def default(scopes=None, **kwargs):
        calls.append(list(scopes or []))
        return AnonymousCredentials(), PROJECT

    monkeypatch.setattr(google.auth, 'default', default)
    backend = get_backend('gcp')
    assert isinstance(backend, GCPBackend)
    assert get_backend('gcp') is backend
    assert isinstance(backend._session, AuthorizedSession)
    assert calls == [['https://www.googleapis.com/auth/compute']]


# The fake itself

def test_fake_answers_404_for_absent_resources(gce):
    # Catches the fake inventing resources, which would let absence bugs pass.
    url = f'https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{NAME}'
    response = gce.request('GET', url)
    assert response.status_code == 404
    with pytest.raises(requests.HTTPError) as raised:
        response.raise_for_status()
    assert raised.value.response.status_code == 404
    assert gce.request('DELETE', url, params={'requestId': str(uuid.uuid4())}).status_code == 404
    assert gce.instances == {} and gce.operations == {}


def test_fake_refuses_a_stale_label_fingerprint(gce):
    # Catches the fake accepting stale label writes, which would hide a lost update.
    gce.add_instance(ZONE, NAME, subnetwork=(REGION, SUBNET), labels={TAG_SITE: SITE})
    url = (f'https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}'
           f'/instances/{NAME}/setLabels')
    current = gce.instances[(ZONE, NAME)]['labelFingerprint']
    stale = gce.request('POST', url, json={'labels': {'x': 'y'}, 'labelFingerprint': 'old'})
    assert stale.status_code == 412
    assert gce.instances[(ZONE, NAME)]['labels'] == {TAG_SITE: SITE}
    fresh = gce.request('POST', url, json={'labels': {'x': 'y'}, 'labelFingerprint': current})
    assert fresh.status_code == 200 and fresh.json()['status'] == 'DONE'
    assert gce.instances[(ZONE, NAME)]['labels'] == {'x': 'y'}
    assert gce.instances[(ZONE, NAME)]['labelFingerprint'] != current


def test_fake_replays_a_repeated_request_id_without_changing_state(gce):
    # Catches the fake creating a twin on retry, which would hide a missing requestId.
    url = f'https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances'
    body = {'name': NAME, 'machineType': f'zones/{ZONE}/machineTypes/e2-small',
            'disks': [{'boot': True, 'autoDelete': True}],
            'networkInterfaces': [{'subnetwork': (
                f'projects/{PROJECT}/regions/{REGION}/subnetworks/{SUBNET}')}]}
    request_id = str(uuid.uuid4())
    first = gce.request('POST', url, params={'requestId': request_id}, json=body)
    instance = dict(gce.instances[(ZONE, NAME)])
    second = gce.request('POST', url, params={'requestId': request_id}, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()['name'] == second.json()['name']
    assert list(gce.instances) == [(ZONE, NAME)] and gce.instances[(ZONE, NAME)] == instance
    assert len(gce.operations) == 1
    other = gce.request('POST', url, params={'requestId': str(uuid.uuid4())}, json=body)
    assert other.status_code == 409
    assert list(gce.instances) == [(ZONE, NAME)]
