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
    ManagedResource, SlotNetworkMissing, VMRef,
)
from app.backends.cloud_common import (
    TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT, TAG_VMID,
    deployment_key, expires_epoch, router_lock_key, slot_lock_key,
)
from app.backends.gcp import GCPBackend, OperationFailed, _request_id
from app.models import NodeMemoryReservation, VMRecord, db
from app.proxmox_service import vm_lifecycle_lock
from app.routes.vm_creation import named_instances
from tests.backends.fake_gce import (
    CREATION_TIMESTAMP, GCP_TEST_CONFIG, FakeGCESession, gcp_test_spec,
)

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
SLOT_CIDR = '172.21.25.0/26'
FIREWALL = 'lp-test-site-d-0123456789abcdef'
NETWORK = 'projects/example-project/global/networks/lab-vpc'
ROUTER = 'lab-router'
INSTANCE_PATH = r'zones/[^/]+/instances/[^/]+'
AGGREGATED_PATH = r'aggregated/instances'
INSERT_PATH = f'zones/{ZONE}/instances'
OTHER_ZONE = 'us-east1-b'
CREATED = datetime.fromisoformat(CREATION_TIMESTAMP).timestamp()


def _deployment_rule(name=FIREWALL, **overrides):
    """The rule create makes for a deployment, as another writer would."""
    return dict({'name': name, 'network': NETWORK, 'direction': 'INGRESS', 'priority': 1000,
                 'sourceTags': [name], 'targetTags': [name],
                 'allowed': [{'IPProtocol': 'all'}]}, **overrides)


@pytest.fixture
def gce():
    """A project whose slot 100 is ensured and whose deployment already has
    its firewall, so instance tests start where a real create would."""
    session = FakeGCESession(PROJECT)
    session.add_subnetwork(REGION, SUBNET, SLOT_CIDR)
    session.add_firewall(_deployment_rule())
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
    return gcp_test_spec()


def _ref(spec, launched):
    return VMRef(vmid=spec.vmid, vm_name=spec.vm_name, vm_type=BACKEND_GCP,
                 location=launched.location, backend=BACKEND_GCP,
                 provider_id=launched.provider_id, deployment_id=spec.deployment_id,
                 network_slot=launched.network_slot)


def _launch(backend, spec):
    return _ref(spec, backend.create(spec, ZONE, heartbeat=lambda: None))


def _instances(backend):
    return [r for r in backend.list_managed() if r.kind == 'instance']


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
    third = backend.create(spec, ZONE, heartbeat=lambda: None)
    assert list(gce.instances) == [(ZONE, 'lp-test-site-101')]
    assert first.provider_id == second.provider_id == third.provider_id == NAME
    assert _insert_request_ids(gce) == [_request_id('instance-insert', DEPLOYMENT, VMID, 0)]


def test_request_ids_are_stable_nonzero_rfc4122_uuids():
    # Catches requestId changing across processes (Python's randomized
    # hash()) so a retry is not deduplicated, or a value Compute rejects.
    value = _request_id('instance-insert', DEPLOYMENT, VMID, 0)
    parsed = uuid.UUID(value)
    assert value == _request_id('instance-insert', DEPLOYMENT, VMID, 0)
    assert parsed.version == 5 and parsed.variant == uuid.RFC_4122 and parsed.int != 0
    assert value != _request_id('instance-insert', DEPLOYMENT, VMID + 1, 0)
    assert value != _request_id('instance-insert', DEPLOYMENT, VMID, 1)
    assert value != _request_id('instance-delete', DEPLOYMENT, VMID, 0)


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


def test_create_without_slot_subnetwork_raises_slot_network_missing(backend, gce, spec):
    # Catches a launch into a slot a concurrent retirement just released (I3).
    gce.subnetworks.clear()
    gce.firewalls.clear()
    with pytest.raises(SlotNetworkMissing):
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert gce.instances == {} and gce.firewalls == {}


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
    assert backend.ip_addresses(ref) == ['172.21.25.2']
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


@pytest.mark.parametrize('path', [INSTANCE_PATH, AGGREGATED_PATH])
@pytest.mark.parametrize('fault', [
    {'status': 403}, {'status': 404}, {'status': 429}, {'status': 503},
    {'invalid_json': True}, {'transport': True},
])
def test_every_ambiguous_read_is_unknown_never_absent(backend, gce, spec, path, fault):
    # Catches a 403/404/429/5xx, garbage body or dropped connection, from
    # discovery or the zonal read after it, being read as absence.
    ref = _launch(backend, spec)
    gce.fail('GET', path, times=2, **fault)
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
    assert _instances(backend) == [expected]
    gce.loose_filter = False
    assert _instances(backend) == [expected]
    assert ref.provider_id == expected.provider_id


def test_list_managed_keeps_malformed_owned_instances(backend, gce, spec):
    # Catches an owned instance with garbled labels vanishing from the
    # sweeper instead of being listed with unknown fields.
    gce.add_instance(ZONE, 'lp-test-site-7', subnetwork=(REGION, SUBNET), labels={
        TAG_SITE: SITE, TAG_KIND: 'instance', TAG_VMID: 'seven',
        TAG_SLOT: '-1', TAG_EXPIRES: '12x'})
    gce.instances[(ZONE, 'lp-test-site-7')]['creationTimestamp'] = 'yesterday'
    [resource] = _instances(backend)
    assert (resource.provider_id, resource.vmid, resource.network_slot,
            resource.expires_at_epoch, resource.created_at_epoch, resource.deployment_id) == (
        'lp-test-site-7', None, None, None, None, None)


def test_list_managed_names_round_trip_to_the_portal_vm_name(backend, gce, spec):
    # Catches /create pre-flight missing an existing GCP VM because its
    # inventory name is not the portal's vm_name (labels cannot hold it).
    _launch(backend, spec)
    assert gce.instances[(ZONE, NAME)]['description'] == spec.vm_name
    [managed] = _instances(backend)
    assert managed.name == spec.vm_name
    assert named_instances(backend, 'test-gcp-') == [
        {'vmid': VMID, 'node': ZONE, 'vm_type': 'gcp', 'name': spec.vm_name}]


@pytest.mark.parametrize('fault', [{'status': 500}, {'invalid_json': True}, {'transport': True}])
def test_list_managed_failure_raises_never_empty(backend, gce, spec, fault):
    # Catches an inventory outage read as "nothing exists" by the sweeper.
    _launch(backend, spec)
    gce.fail('GET', AGGREGATED_PATH, **fault)
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


# Discovery: the instance's zone and its absence come from the project-wide
# aggregated list, never from a location string.

@pytest.mark.parametrize('location', [OTHER_ZONE, 'zz-fake9-q'])
def test_a_wrong_zone_in_the_record_never_reads_as_absence(backend, gce, spec, location):
    # Catches a record whose node names another real zone, or a fabricated
    # zone-shaped one, 404ing into "absent" and being retired while billing.
    ref = replace(_launch(backend, spec), location=location)
    assert backend.inspect(ref) == GUEST_MATCH
    assert backend.confirm_absent(ref) is False
    assert backend.status(ref) == 'running'
    new_expiry = spec.expires_at + timedelta(hours=2)
    backend.set_expiry(ref, new_expiry)
    assert _labels(gce)[TAG_EXPIRES] == str(expires_epoch(new_expiry))
    backend.destroy(ref, DestroyPolicy.STRICT)
    assert gce.instances == {}
    assert backend.confirm_absent(ref) is True


def test_changing_gcp_zone_after_launch_keeps_the_vm_found(backend, gce, spec, monkeypatch):
    # Catches an operator's GCP_ZONE change hiding every VM launched before
    # it, so reconcile retires their records and the sweeper never sees them.
    ref = _launch(backend, spec)
    monkeypatch.setattr(importlib.import_module('app.config'), 'GCP_ZONE', OTHER_ZONE)
    survivor = replace(ref, provider_id=None, location='gcp')
    assert backend.inspect(ref) == backend.inspect(survivor) == GUEST_MATCH
    assert backend.confirm_absent(ref) is False and backend.confirm_absent(survivor) is False
    [listed] = _instances(backend)
    assert listed.location == ZONE
    backend.delete_resource(replace(listed, location=OTHER_ZONE))
    assert gce.instances == {}


@pytest.mark.parametrize('reports', [('warning',), ('unreachables',)])
def test_an_unreachable_zone_makes_absence_unknown(backend, gce, spec, reports):
    # Catches an aggregated list that could not reach every zone being read
    # as "no such instance anywhere".
    ref = _launch(backend, spec)
    del gce.instances[(ZONE, NAME)]
    gce.unreachable_zones = {OTHER_ZONE}
    gce.unreachable_reports = reports
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False
    with pytest.raises(BackendError) as raised:
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert not isinstance(raised.value, DestroyRefused)
    with pytest.raises(BackendError):
        backend.list_managed()
    gce.unreachable_zones = set()
    assert backend.confirm_absent(ref) is True


def test_an_instance_in_an_unreachable_zone_is_never_absent(backend, gce, spec):
    # Catches a VM in a zone the list could not reach being retired.
    ref = _launch(backend, spec)
    gce.unreachable_zones = {ZONE}
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False


def test_the_same_name_in_two_zones_is_unknown(backend, gce, spec):
    # Catches discovery picking one of two same-named instances and acting on it.
    ref = _launch(backend, spec)
    gce.add_subnetwork('us-east1', 'other-subnet', '172.22.0.0/24')
    gce.add_instance(OTHER_ZONE, NAME, labels=_labels(gce), subnetwork=('us-east1', 'other-subnet'))
    assert backend.inspect(ref) == GUEST_UNKNOWN
    assert backend.confirm_absent(ref) is False
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert {(ZONE, NAME), (OTHER_ZONE, NAME)} <= set(gce.instances)


def test_a_delete_404_for_an_instance_that_still_exists_raises(backend, gce, spec):
    # Catches a DELETE answered 404 being reported as a destroyed VM while
    # discovery still finds it.
    ref = _launch(backend, spec)
    gce.fail('DELETE', INSTANCE_PATH, status=404)
    with pytest.raises(BackendError):
        backend.destroy(ref, DestroyPolicy.STRICT)
    assert (ZONE, NAME) in gce.instances


# Insert attempts

def _insert_request_ids(gce):
    return [request_id for method, path, request_id in gce.request_results
            if (method, path) == ('POST', INSERT_PATH)]


def _release_slot_during_insert(gce, monkeypatch):
    """Remove the slot's subnetwork when the next instance insert arrives: a
    retirement releasing it between create's slot check and its insert."""
    request = gce.request
    fired = []

    def racing(method, url, **kwargs):
        if not fired and method == 'POST' and url.endswith('/' + INSERT_PATH):
            fired.append(True)
            gce.subnetworks.pop((REGION, SUBNET), None)
        return request(method, url, **kwargs)

    monkeypatch.setattr(gce, 'request', racing)


@pytest.mark.parametrize('cause', ['quota', 'released-subnetwork'])
def test_terminal_insert_failure_then_corrected_cause_launches_the_same_spec(
        backend, gce, spec, monkeypatch, cause):
    # Catches a failed insert's requestId replaying the failure forever, so
    # the VM can never be created once the cause is fixed.
    if cause == 'quota':
        gce.next_operation_error = {'code': 'QUOTA_EXCEEDED', 'message': 'CPUS quota exceeded'}
        with pytest.raises(OperationFailed):
            backend.create(spec, ZONE, heartbeat=lambda: None)
    else:
        gce.subnetwork_failure = 'async'
        _release_slot_during_insert(gce, monkeypatch)
        with pytest.raises(SlotNetworkMissing) as raised:
            backend.create(spec, ZONE, heartbeat=lambda: None)
        assert isinstance(raised.value.__cause__, OperationFailed)
        gce.add_subnetwork(REGION, SUBNET, SLOT_CIDR)  # create_vm re-ensures (I3)
    assert gce.instances == {}
    launched = backend.create(spec, ZONE, heartbeat=lambda: None)
    again = backend.create(spec, ZONE, heartbeat=lambda: None)
    assert launched == again and list(gce.instances) == [(ZONE, NAME)]
    assert _insert_request_ids(gce) == [
        _request_id('instance-insert', DEPLOYMENT, VMID, 0),
        _request_id('instance-insert', DEPLOYMENT, VMID, 1)]


def test_a_failed_insert_whose_absence_is_unproven_keeps_its_request_id(backend, gce, spec):
    # Catches a fresh requestId being issued while an instance from the
    # failed attempt might still exist somewhere in the project.
    gce.next_operation_error = {'code': 'QUOTA_EXCEEDED', 'message': 'CPUS quota exceeded'}
    gce.fail('GET', AGGREGATED_PATH, status=503)
    with pytest.raises(OperationFailed) as first:
        backend.create(spec, ZONE, heartbeat=lambda: None)
    with pytest.raises(OperationFailed) as second:
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert second.value.operation['name'] == first.value.operation['name']
    assert _insert_request_ids(gce) == [_request_id('instance-insert', DEPLOYMENT, VMID, 0)]
    # The replay proved absence, so the next try is a new attempt.
    assert backend.create(spec, ZONE, heartbeat=lambda: None).provider_id == NAME
    assert list(gce.instances) == [(ZONE, NAME)]


@pytest.mark.parametrize('ambiguity', ['timeout', 'transport'])
def test_an_ambiguous_insert_retry_reuses_its_request_id(
        backend, gce, spec, monkeypatch, ambiguity):
    # Catches a retry after a timed-out wait or lost response issuing a new
    # requestId, which could start a second instance.
    if ambiguity == 'timeout':
        now = [0.0]

        def clock():
            now[0] += 4.0
            return now[0]

        monkeypatch.setattr(gcp_module, '_clock', clock)
        gce.stall_operations = True
    else:
        gce.fail('POST', INSERT_PATH, transport=True)
    with pytest.raises(BackendError) as raised:
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert not isinstance(raised.value, OperationFailed)
    gce.stall_operations = False
    assert backend.create(spec, ZONE, heartbeat=lambda: None).provider_id == NAME
    assert list(gce.instances) == [(ZONE, NAME)]
    assert _insert_request_ids(gce) == [_request_id('instance-insert', DEPLOYMENT, VMID, 0)]


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


def _instances_url():
    return f'https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances'


def _insert_body(subnet=SUBNET):
    return {'name': NAME, 'machineType': f'zones/{ZONE}/machineTypes/e2-small',
            'disks': [{'boot': True, 'autoDelete': True}],
            'networkInterfaces': [{'subnetwork': (
                f'projects/{PROJECT}/regions/{REGION}/subnetworks/{subnet}')}]}


def test_fake_replays_a_failed_operation_for_a_repeated_request_id(gce):
    # Catches the fake retrying a failed insert under its old requestId,
    # which would hide the replay that makes a new attempt id necessary.
    gce.next_operation_error = {'code': 'QUOTA_EXCEEDED', 'message': 'quota'}
    request_id = str(uuid.uuid4())
    first = gce.request('POST', _instances_url(), params={'requestId': request_id},
                        json=_insert_body())
    second = gce.request('POST', _instances_url(), params={'requestId': request_id},
                         json=_insert_body())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.json()['status'] == 'DONE'
    assert second.json()['error']['errors'][0]['code'] == 'QUOTA_EXCEEDED'
    assert gce.instances == {} and len(gce.operations) == 1


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_fake_reports_a_missing_subnetwork_synchronously_or_in_the_operation(gce, mode):
    # Catches the fake modelling only one of the two ways Compute may refuse
    # an insert into a subnetwork that is gone.
    gce.subnetwork_failure = mode
    response = gce.request('POST', _instances_url(), params={'requestId': str(uuid.uuid4())},
                           json=_insert_body('lp-test-site-s-999'))
    if mode == 'sync':
        assert response.status_code == 404 and gce.operations == {}
    else:
        assert response.status_code == 200
        operation = response.json()
        assert operation['status'] == 'DONE'
        assert operation['error']['errors'][0]['code'] == 'RESOURCE_NOT_FOUND'
        assert len(gce.operations) == 1
    assert gce.instances == {}


# Slot networks

def _ensure_slot(backend, slot=SLOT):
    with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, slot)):
        backend.ensure_slot(slot)


def _release_slot(backend, slot=SLOT):
    with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, slot)):
        backend.release_slot(slot)


def test_ensure_and_release_slot_are_idempotent_and_use_the_derived_cidr(backend, gce):
    # Catches two slot owners being placed on an overlapping/default network.
    gce.subnetworks.clear()
    with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, 100)):
        backend.ensure_slot(100)
        backend.ensure_slot(100)
    subnet = gce.subnetworks[(REGION, 'lp-test-site-s-100')]
    assert subnet['ipCidrRange'] == '172.21.25.0/26'
    assert subnet['network'].endswith('/global/networks/lab-vpc')
    assert subnet['privateIpGoogleAccess'] is False
    assert len(gce.subnetworks) == 1
    with vm_lifecycle_lock(slot_lock_key(BACKEND_GCP, 100)):
        backend.release_slot(100)
        backend.release_slot(100)
    assert gce.subnetworks == {}


@pytest.mark.parametrize('winner, adopted', [
    ({}, True),
    ({'ip_cidr_range': '172.21.26.0/26'}, False),
    ({'network': 'other-vpc'}, False),
])
def test_a_slot_insert_race_adopts_only_an_exactly_placed_winner(
        backend, gce, monkeypatch, winner, adopted):
    # Catches a lost create race launching a slot into another network or
    # CIDR that happens to hold the slot's name.
    gce.subnetworks.clear()
    request = gce.request

    def racing(method, url, **kwargs):
        if method == 'POST' and url.endswith(f'/regions/{REGION}/subnetworks'):
            gce.add_subnetwork(REGION, SUBNET, winner.get('ip_cidr_range', SLOT_CIDR),
                               network=winner.get('network', 'lab-vpc'))
        return request(method, url, **kwargs)

    monkeypatch.setattr(gce, 'request', racing)
    if adopted:
        _ensure_slot(backend)
    else:
        with pytest.raises(BackendError):
            _ensure_slot(backend)
    assert list(gce.subnetworks) == [(REGION, SUBNET)]


@pytest.mark.parametrize('field, value', [
    ('ipCidrRange', '172.21.26.0/26'),
    ('network', 'projects/example-project/global/networks/other-vpc'),
    ('creationTimestamp', None),
])
def test_ensure_slot_and_create_refuse_a_misplaced_slot_subnetwork(backend, gce, spec, field, value):
    # Catches a VM launched into a same-named subnetwork at the wrong CIDR
    # or in another network.
    gce.subnetworks[(REGION, SUBNET)][field] = value
    with pytest.raises(BackendError):
        _ensure_slot(backend)
    with pytest.raises(BackendError) as raised:
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert not isinstance(raised.value, SlotNetworkMissing)
    assert gce.instances == {}


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_a_slot_released_during_the_insert_raises_slot_network_missing(
        backend, gce, spec, monkeypatch, mode):
    # Catches a launch that lost its slot mid-insert failing for good
    # instead of letting create_vm re-ensure the slot and retry once (I3).
    gce.subnetwork_failure = mode
    _release_slot_during_insert(gce, monkeypatch)
    with pytest.raises(SlotNetworkMissing):
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert gce.instances == {}
    _ensure_slot(backend)
    assert backend.create(spec, ZONE, heartbeat=lambda: None).provider_id == NAME
    assert list(gce.instances) == [(ZONE, NAME)]


def test_release_slot_refuses_a_subnetwork_it_cannot_prove_is_its_own(backend, gce):
    # Catches a release deleting a same-named subnetwork in another network.
    gce.subnetworks[(REGION, SUBNET)]['network'] = 'projects/example-project/global/networks/x'
    with pytest.raises(DestroyRefused):
        _release_slot(backend)
    assert (REGION, SUBNET) in gce.subnetworks


# Deployment firewalls

def test_create_uses_only_the_per_deployment_firewall(backend, gce, spec):
    # Catches broad lateral ingress or accidental ownership of the static tailnet rule.
    gce.firewalls.clear()
    backend.ensure_slot(100)
    backend.create(spec, ZONE, heartbeat=lambda: None)
    name = 'lp-test-site-d-0123456789abcdef'
    assert list(gce.firewalls) == [name]
    assert gce.firewalls[name] == {
        **gce.firewall_server_fields(name),
        'name': name,
        'network': 'projects/example-project/global/networks/lab-vpc',
        'direction': 'INGRESS',
        'priority': 1000,
        'sourceTags': [name],
        'targetTags': [name],
        'allowed': [{'IPProtocol': 'all'}],
    }
    assert 'sourceRanges' not in gce.firewalls[name]
    assert 'destinationRanges' not in gce.firewalls[name]
    assert gce.instances[(ZONE, 'lp-test-site-101')]['tags']['items'] == [
        name, 'lp-test-site-lab']
    assert 'lp-test-site-lab' not in gce.firewalls


@pytest.mark.parametrize('field, value', [
    ('network', 'projects/example-project/global/networks/other-vpc'),
    ('direction', 'EGRESS'),
    ('priority', 900),
    ('sourceTags', ['lp-test-site-lab']),
    ('targetTags', ['lp-test-site-lab']),
    ('allowed', [{'IPProtocol': 'tcp', 'ports': ['22']}]),
    ('sourceRanges', ['0.0.0.0/0']),
    ('destinationRanges', ['0.0.0.0/0']),
    ('disabled', True),
])
def test_existing_firewall_must_match_every_security_field(backend, gce, spec, field, value):
    # Catches a lab joining a same-named rule that grants more (or other)
    # access than its own deployment's.
    gce.firewalls[FIREWALL][field] = value
    before = dict(gce.firewalls[FIREWALL])
    with pytest.raises(BackendError):
        backend.create(spec, ZONE, heartbeat=lambda: None)
    assert gce.instances == {}
    assert gce.firewalls[FIREWALL] == before


@pytest.mark.parametrize('winner, adopted', [
    ({}, True), ({'allowed': [{'IPProtocol': 'tcp'}]}, False),
])
def test_a_firewall_insert_race_adopts_only_an_exact_winner(
        backend, gce, spec, monkeypatch, winner, adopted):
    # Catches a lost create race attaching the lab to a foreign rule.
    gce.firewalls.clear()
    request = gce.request

    def racing(method, url, **kwargs):
        if method == 'POST' and url.endswith('/global/firewalls'):
            gce.add_firewall(_deployment_rule(**winner))
        return request(method, url, **kwargs)

    monkeypatch.setattr(gce, 'request', racing)
    if adopted:
        backend.create(spec, ZONE, heartbeat=lambda: None)
        assert list(gce.instances) == [(ZONE, NAME)]
    else:
        with pytest.raises(BackendError):
            backend.create(spec, ZONE, heartbeat=lambda: None)
        assert gce.instances == {}
    assert list(gce.firewalls) == [FIREWALL]


def test_cleanup_deployment_deletes_only_its_exact_firewall_and_is_idempotent(backend, gce, spec):
    # Catches cleanup deleting the operator's tailnet rule, another
    # deployment's or another site's firewall, or failing when already clean.
    ref = _launch(backend, spec)
    others = ['lp-test-site-lab', 'lp-test-site-d-ffffffffffffffff',
              'lp-other-site-d-0123456789abcdef']
    for name in others:
        gce.add_firewall(_deployment_rule(name))
    backend.destroy(ref, DestroyPolicy.STRICT)
    backend.cleanup_deployment(DEPLOYMENT)
    backend.cleanup_deployment(DEPLOYMENT)
    assert sorted(gce.firewalls) == sorted(others)


@pytest.mark.parametrize('field, value', [
    ('creationTimestamp', None), ('creationTimestamp', 'yesterday'),
    ('sourceTags', ['lp-test-site-lab']), ('targetTags', [FIREWALL, 'other']),
    ('network', 'projects/example-project/global/networks/other-vpc'),
])
def test_cleanup_deployment_refuses_a_forged_exact_name_firewall(backend, gce, field, value):
    # Catches cleanup deleting a rule that only shares the deployment's name.
    gce.firewalls[FIREWALL][field] = value
    with pytest.raises(DestroyRefused):
        backend.cleanup_deployment(DEPLOYMENT)
    assert FIREWALL in gce.firewalls


def test_network_operation_failure_keeps_the_resource_visible_for_retry(backend, gce, spec):
    # Catches a refused network delete being reported as cleaned up, so the
    # caller and the sweeper would never retry it.
    ref = _launch(backend, spec)
    with pytest.raises(OperationFailed):
        _release_slot(backend)  # the instance still uses it
    gce.next_operation_error = {'code': 'RESOURCE_NOT_READY', 'message': 'busy'}
    with pytest.raises(OperationFailed):
        backend.cleanup_deployment(DEPLOYMENT)
    kinds = {r.kind for r in backend.list_managed()}
    assert kinds == {'instance', 'slot-network', 'deployment-network'}
    backend.destroy(ref, DestroyPolicy.STRICT)
    backend.cleanup_deployment(DEPLOYMENT)
    _release_slot(backend)
    assert backend.list_managed() == []


def test_a_failed_instance_delete_can_be_retried(backend, gce, spec):
    # Catches a delete's requestId replaying its failure on every later
    # destroy, so the VM bills forever.
    ref = _launch(backend, spec)
    gce.next_operation_error = {'code': 'RESOURCE_NOT_READY', 'message': 'busy'}
    with pytest.raises(OperationFailed):
        backend.destroy(ref, DestroyPolicy.STRICT)
    backend.destroy(ref, DestroyPolicy.STRICT)
    assert gce.instances == {}


# Inventory of every kind

def _networks(backend):
    return sorted((r for r in backend.list_managed() if r.kind != 'instance'),
                  key=lambda r: (r.kind, r.name))


def test_list_managed_parses_only_owned_subnets_and_firewalls_with_timestamps(backend, gce):
    # Catches the sweeper adopting the operator's tailnet rule, another
    # site's network, or a resource it cannot age.
    for name in ('lp-test-site-s-007', 'lp-test-site-s-abc', 'lp-test-site-s-501',
                 'lp-other-site-s-5', 'lp-test-site-lab'):
        gce.add_subnetwork(REGION, name, f'10.{len(gce.subnetworks)}.0.0/26')
    gce.add_subnetwork(REGION, 'lp-test-site-s-7', '10.50.0.0/26', network='other-vpc')
    gce.add_subnetwork(REGION, 'lp-test-site-s-8', '10.51.0.0/26')
    gce.subnetworks[(REGION, 'lp-test-site-s-8')]['creationTimestamp'] = 'yesterday'
    gce.add_subnetwork('us-east1', 'lp-test-site-s-9', '10.52.0.0/26')
    for name in ('lp-test-site-lab', 'lp-test-site-d-0123', 'lp-test-site-d-0123456789ABCDEF',
                 'lp-other-site-d-0123456789abcdef', 'lp-test-site-d-0123456789abcdef0'):
        gce.add_firewall(_deployment_rule(name))
    gce.add_firewall(_deployment_rule('lp-test-site-d-aaaaaaaaaaaaaaaa',
                                      sourceTags=['lp-test-site-lab']))
    gce.add_firewall(_deployment_rule('lp-test-site-d-bbbbbbbbbbbbbbbb'))
    gce.firewalls['lp-test-site-d-bbbbbbbbbbbbbbbb']['creationTimestamp'] = None
    gce.page_size = 1
    link = 'https://www.googleapis.com/compute/v1/projects/example-project'
    assert _networks(backend) == [
        ManagedResource(backend='gcp', kind='deployment-network',
                        provider_id=f'{link}/global/firewalls/{FIREWALL}', name=FIREWALL,
                        deployment_id='0123456789abcdef', created_at_epoch=CREATED),
        ManagedResource(backend='gcp', kind='slot-network',
                        provider_id=f'{link}/regions/{REGION}/subnetworks/{SUBNET}',
                        name=SUBNET, location=REGION, network_slot=SLOT,
                        created_at_epoch=CREATED),
        ManagedResource(backend='gcp', kind='slot-network',
                        provider_id=f'{link}/regions/us-east1/subnetworks/lp-test-site-s-9',
                        name='lp-test-site-s-9', location='us-east1', network_slot=9,
                        created_at_epoch=CREATED),
    ]


def test_firewall_inventory_reports_the_deployment_key(backend, gce, spec):
    # Catches the firewall's deployment identity disagreeing with AWS and T15 (C1).
    backend.create(spec, ZONE, heartbeat=lambda: None)
    [firewall] = [r for r in backend.list_managed() if r.kind == 'deployment-network']
    assert firewall.deployment_id == deployment_key(spec.deployment_id) == '0123456789abcdef'


def test_inventory_attributes_every_kind_for_the_sweeper(backend, gce, spec):
    # Catches T15's cloud sweep never reaping GCP networks because the
    # instances that hold them report no slot or deployment.
    _launch(backend, spec)
    resources = {r.kind: r for r in backend.list_managed()}
    assert set(resources) == {'instance', 'slot-network', 'deployment-network'}
    instance = resources['instance']
    assert (instance.network_slot, instance.deployment_id) == (SLOT, DEPLOYMENT)
    assert resources['slot-network'].network_slot == instance.network_slot
    assert resources['deployment-network'].deployment_id == deployment_key(instance.deployment_id)
    assert all(r.created_at_epoch == CREATED for r in resources.values())


@pytest.mark.parametrize('region', ['us-east1'])
def test_an_unreachable_region_fails_the_inventory(backend, gce, region):
    # Catches a slot subnetwork in an unreachable region being read as gone.
    gce.unreachable_regions = {region}
    with pytest.raises(BackendError):
        backend.list_managed()


def test_delete_resource_removes_each_known_kind_and_rejects_unknown_kind(backend, gce, spec):
    # Catches the sweeper being unable to dispatch GCP network leftovers.
    ref = _launch(backend, spec)
    resources = {r.kind: r for r in backend.list_managed()}
    with pytest.raises(BackendError):
        backend.delete_resource(replace(resources['slot-network'], kind='router'))
    backend.delete_resource(resources['instance'])
    backend.delete_resource(resources['deployment-network'])
    backend.delete_resource(resources['slot-network'])
    assert gce.instances == gce.subnetworks == gce.firewalls == {}
    for resource in resources.values():
        backend.delete_resource(resource)  # absent: done
    assert backend.confirm_absent(ref) is True


@pytest.mark.parametrize('kind, mutate', [
    ('slot-network', lambda gce: gce.subnetworks[(REGION, SUBNET)].update(
        network='projects/example-project/global/networks/x')),
    ('slot-network', lambda gce: gce.subnetworks[(REGION, SUBNET)].update(
        creationTimestamp=None)),
    ('deployment-network', lambda gce: gce.firewalls[FIREWALL].update(
        targetTags=['lp-test-site-lab'])),
    ('deployment-network', lambda gce: gce.firewalls[FIREWALL].update(
        creationTimestamp='garbled')),
])
def test_delete_resource_rereads_and_refuses_forged_or_relabelled_ownership(
        backend, gce, kind, mutate):
    # Catches a stale listing deleting a network that no longer proves it is ours.
    [resource] = [r for r in backend.list_managed() if r.kind == kind]
    mutate(gce)
    with pytest.raises(DestroyRefused):
        backend.delete_resource(resource)
    assert (REGION, SUBNET) in gce.subnetworks and FIREWALL in gce.firewalls


@pytest.mark.parametrize('kind, provider_id', [
    ('slot-network', 'lp-other-site-s-100'),
    ('slot-network', 'lp-test-site-s-101'),
    ('slot-network', 'https://www.googleapis.com/compute/v1/projects/other-project/regions/'
                     'us-central1/subnetworks/lp-test-site-s-100'),
    ('deployment-network', 'lp-test-site-lab'),
    ('deployment-network', 'lp-test-site-d-ffffffffffffffff'),
])
def test_delete_resource_refuses_a_forged_listing_without_touching_anything(
        backend, gce, kind, provider_id):
    # Catches a forged or mismatched listing reaching a DELETE.
    [resource] = [r for r in backend.list_managed() if r.kind == kind]
    gce.add_firewall(_deployment_rule('lp-test-site-lab'))
    before = (dict(gce.subnetworks), dict(gce.firewalls))
    with pytest.raises(DestroyRefused):
        backend.delete_resource(replace(resource, provider_id=provider_id))
    assert (dict(gce.subnetworks), dict(gce.firewalls)) == before


@pytest.mark.parametrize('kind, path', [
    ('slot-network', r'regions/[^/]+/subnetworks/[^/]+'),
    ('deployment-network', r'global/firewalls/[^/]+'),
])
def test_delete_resource_network_unverifiable_raises(backend, gce, kind, path):
    # Catches a read failure during a sweep being read as permission to delete.
    [resource] = [r for r in backend.list_managed() if r.kind == kind]
    gce.fail('GET', path, status=503)
    with pytest.raises(BackendError) as raised:
        backend.delete_resource(resource)
    assert not isinstance(raised.value, DestroyRefused)
    assert (REGION, SUBNET) in gce.subnetworks and FIREWALL in gce.firewalls


# Display reads use the discovered resource

def test_status_and_ips_come_from_discovery_without_a_zonal_read(backend, gce, spec):
    # Catches a /list render paying a second zonal GET per VM, or showing a
    # VM whose listed labels do not match.
    ref = _launch(backend, spec)
    gce.fail('GET', INSTANCE_PATH, status=500, times=10)
    assert backend.status(ref) == 'running'
    assert backend.ip_addresses(ref) == ['172.21.25.2']
    gce.instances[(ZONE, NAME)]['labels'][TAG_DEPLOYMENT] = 'f' * 32
    assert backend.status(ref) == 'unknown'
    assert backend.ip_addresses(ref) == []
    gce.fail('GET', AGGREGATED_PATH, status=503, times=2)
    assert backend.status(ref) == 'unknown'
    assert backend.ip_addresses(ref) == []


# Router

@pytest.fixture
def router_clock(monkeypatch):
    """A clock that jumps 4s per read and a sleep that returns at once, so
    settle deadlines are reachable without waiting."""
    now = [0.0]

    def clock():
        now[0] += 4.0
        return now[0]

    monkeypatch.setattr(gcp_module, '_clock', clock)
    monkeypatch.setattr(gcp_module, '_sleep', lambda seconds: None)


def _router_status(gce):
    return gce.instances[(ZONE, ROUTER)]['status']


def _start(backend):
    with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
        backend.ensure_router_running()


def _stop(backend):
    with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
        backend.stop_router()


def test_router_start_and_stop_use_the_reserved_lock_boundary(backend, gce):
    # Catches a launch racing an idle-stop and losing its only VPN route.
    gce.add_router(ZONE, 'lab-router', status='TERMINATED')
    with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
        backend.ensure_router_running()
    assert gce.instances[(ZONE, 'lab-router')]['status'] == 'RUNNING'
    with vm_lifecycle_lock(router_lock_key(BACKEND_GCP)):
        backend.stop_router()
    assert gce.instances[(ZONE, 'lab-router')]['status'] == 'TERMINATED'
    assert all(r.name != 'lab-router' for r in backend.list_managed())


def test_router_start_and_stop_are_idempotent(backend, gce):
    # Catches a redundant start/stop operation (or a failure) on a router
    # already in the wanted state.
    gce.add_router(ZONE, ROUTER, status='RUNNING')
    _start(backend)
    assert _router_status(gce) == 'RUNNING' and gce.operations == {}
    gce.instances[(ZONE, ROUTER)]['status'] = 'TERMINATED'
    _stop(backend)
    assert _router_status(gce) == 'TERMINATED' and gce.operations == {}


def test_router_in_another_zone_is_found_by_discovery(backend, gce):
    # Catches the router being looked up only in GCP_ZONE.
    gce.add_router(OTHER_ZONE, ROUTER, status='TERMINATED')
    _start(backend)
    assert gce.instances[(OTHER_ZONE, ROUTER)]['status'] == 'RUNNING'


@pytest.mark.parametrize('setup', [
    lambda gce, config: None,  # absent
    lambda gce, config: gce.add_router(ZONE, ROUTER, site='other-site'),
    lambda gce, config: gce.add_router(ZONE, ROUTER, kind='instance'),
    lambda gce, config: setattr(config, 'GCP_ROUTER_INSTANCE', ''),
    lambda gce, config: (gce.add_router(ZONE, ROUTER), setattr(gce, 'unreachable_zones',
                                                               {OTHER_ZONE})),
])
def test_router_absent_unconfigured_or_not_this_sites_raises(backend, gce, monkeypatch, setup):
    # Catches a launch proceeding with no router, or a misconfigured name
    # starting or stopping a machine that is not this site's router.
    config = importlib.import_module('app.config')
    monkeypatch.setattr(config, 'GCP_ROUTER_INSTANCE', ROUTER)
    setup(gce, config)
    before = {key: dict(value) for key, value in gce.instances.items()}
    with pytest.raises(BackendError):
        _start(backend)
    with pytest.raises(BackendError):
        _stop(backend)
    assert gce.instances == before and gce.operations == {}


@pytest.mark.parametrize('action, knob, stuck', [
    ('start', 'start_status', 'STAGING'),
    ('start', 'start_status', 'TERMINATED'),
    ('stop', 'stop_status', 'STOPPING'),
    ('stop', 'stop_status', 'RUNNING'),
])
def test_router_operation_that_never_reaches_its_state_raises(
        backend, gce, router_clock, action, knob, stuck):
    # Catches a start or stop reported as done while the router is not
    # (yet, or ever) in the required state.
    gce.add_router(ZONE, ROUTER, status='TERMINATED' if action == 'start' else 'RUNNING')
    setattr(gce, knob, stuck)
    with pytest.raises(BackendError):
        _start(backend) if action == 'start' else _stop(backend)
    assert _router_status(gce) == stuck


def _settles_on_second_read(gce, monkeypatch, status):
    """The router becomes `status` from its second fresh read on: it was
    mid-transition when first read."""
    reads = []
    request = gce.request

    def settling(method, url, **kwargs):
        if method == 'GET' and url.endswith(f'/zones/{ZONE}/instances/{ROUTER}'):
            reads.append(1)
            if len(reads) == 2:
                gce.instances[(ZONE, ROUTER)]['status'] = status
        return request(method, url, **kwargs)

    monkeypatch.setattr(gce, 'request', settling)


@pytest.mark.parametrize('first, settled, action, final', [
    ('STOPPING', 'TERMINATED', 'start', 'RUNNING'),
    ('STAGING', 'RUNNING', 'stop', 'TERMINATED'),
])
def test_router_transitional_states_are_waited_out(
        backend, gce, router_clock, monkeypatch, first, settled, action, final):
    # Catches a start refused while the router is still stopping, or a stop
    # issued to a router that is still starting.
    gce.add_router(ZONE, ROUTER, status=first)
    _settles_on_second_read(gce, monkeypatch, settled)
    _start(backend) if action == 'start' else _stop(backend)
    assert _router_status(gce) == final
