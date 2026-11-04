"""Shared lifecycle contract every VMBackend must pass.

Each later backend joins by appending its case builder to the fixture's
params; the test bodies never change per backend.
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.backends.base import (
    GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, BackendError, DestroyPolicy,
    DestroyRefused, SlotNetworkMissing,
)
from app.backends.cloud_common import deployment_key
from app.proxmox_service import vm_lifecycle_lock
from tests.backends.fake_backend import (
    FakeBackend, make_aws_backend_case, make_memory_backend_case,
)
from tests.backends.proxmox_case import make_proxmox_backend_case


@pytest.fixture(params=[make_memory_backend_case,
                        make_proxmox_backend_case,
                        make_aws_backend_case],
                ids=lambda factory: factory.__name__)
def backend_case(request, app):
    case = request.param(app)
    yield case
    case.cleanup()


# Catches a provider adapter that cannot complete the common VM lifecycle.
# Like create_vm (T7), each test starts the router and ensures the slot before
# admit/create: AWS and GCP create() need the slot network (ruling C3).
def test_backend_lifecycle_contract(backend_case):
    case = backend_case
    case.backend.ensure_router_running()
    case.backend.ensure_slot(case.spec.network_slot)
    location = case.backend.admit(case.spec, reservation_id='reservation-a',
                                  load_balancing_config=None)
    heartbeats = []
    launched = case.backend.create(case.spec, location,
                                   heartbeat=lambda: heartbeats.append(True))
    ref = case.ref(launched, location)
    assert case.backend.renew_admission('reservation-a') is True
    assert case.backend.release_admission('reservation-a') is True
    assert heartbeats
    assert case.backend.inspect(ref) == GUEST_MATCH
    assert case.backend.ip_addresses(ref) == case.expected_ips
    assert case.backend.status(ref) == case.expected_status
    extended = datetime.now() + timedelta(hours=2)
    case.backend.set_expiry(ref, extended)
    resource = next(r for r in case.backend.list_managed()
                    if r.kind == 'instance' and r.vmid == ref.vmid)
    expected_epoch = (pytest.approx(extended.timestamp(), abs=1)
                      if case.inventory_reports_expiry else None)
    assert resource.expires_at_epoch == expected_epoch
    case.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    assert case.backend.inspect(ref) == GUEST_ABSENT
    assert case.backend.confirm_absent(ref)


# Acceptance smoke: catches an adapter that rejects a policy value or leaves
# a healthy matching guest behind under it. It does not prove a policy is
# enforced — clouds terminate in one call, so every policy behaves the same
# there; enforcement on stop failure is pinned per adapter (Proxmox in T6).
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_every_policy_destroys_a_healthy_matching_guest(backend_case, policy):
    case = backend_case
    case.backend.ensure_router_running()
    case.backend.ensure_slot(case.spec.network_slot)
    location = case.backend.admit(
        case.spec, reservation_id=f'reservation-{policy.value}',
        load_balancing_config=None)
    launched = case.backend.create(case.spec, location, heartbeat=lambda: None)
    ref = case.ref(launched, location)
    assert case.backend.release_admission(f'reservation-{policy.value}') is True
    case.backend.destroy(ref, policy)
    assert case.backend.inspect(ref) == GUEST_ABSENT
    assert case.backend.confirm_absent(ref)


# Catches a provider that deletes the instance but leaves billable deployment resources.
def test_backend_cleanup_invariant(backend_case):
    case = backend_case
    case.backend.ensure_router_running()
    case.backend.ensure_slot(case.spec.network_slot)
    location = case.backend.admit(case.spec, reservation_id='reservation-cleanup',
                                  load_balancing_config=None)
    launched = case.backend.create(case.spec, location, heartbeat=lambda: None)
    ref = case.ref(launched, location)
    assert case.backend.release_admission('reservation-cleanup') is True
    case.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    case.backend.cleanup_deployment(case.spec.deployment_id)
    case.backend.release_slot(case.spec.network_slot)
    case.backend.stop_router()
    # Not vacuous for Proxmox, whose resources carry no deployment id (M7).
    assert not [r for r in case.backend.list_managed()
                if r.kind == 'instance' and r.vmid == ref.vmid]
    key = deployment_key(case.spec.deployment_id)
    assert not [r for r in case.backend.list_managed()
                if r.deployment_id is not None and deployment_key(r.deployment_id) == key]


# The fake stands in for clouds in route tests (T7, T8, T16, T19), so the
# failure modes those tests rely on are pinned here, against the fake alone.

def _launched_fake():
    case = make_memory_backend_case(None)
    case.backend.ensure_router_running()
    case.backend.ensure_slot(case.spec.network_slot)
    location = case.backend.admit(case.spec, reservation_id='reservation-f',
                                  load_balancing_config=None)
    launched = case.backend.create(case.spec, location, heartbeat=lambda: None)
    return case, case.ref(launched, location)


# Catches the fake deleting a guest whose identity does not match, which
# would let route tests pass a destroy the real backends must refuse.
def test_fake_destroy_fails_closed_on_identity_mismatch():
    case, ref = _launched_fake()
    stranger = replace(ref, provider_id='provider-other')
    assert case.backend.inspect(stranger) == GUEST_MISMATCH
    with pytest.raises(DestroyRefused):
        case.backend.destroy(stranger, DestroyPolicy.FORCE_CORRUPT)
    assert ref.vmid in case.backend.instances
    assert ref.vmid not in case.backend.destroyed


# Catches a survivor row without a provider id (create raised after launch)
# becoming impossible to inspect or destroy (ruling C7).
def test_fake_resolves_ref_without_provider_id_by_vmid():
    case, ref = _launched_fake()
    anonymous = replace(ref, provider_id=None)
    assert case.backend.inspect(anonymous) == GUEST_MATCH
    case.backend.destroy(anonymous, DestroyPolicy.STRICT)
    assert case.backend.confirm_absent(anonymous)
    assert case.backend.destroyed[ref.vmid] == DestroyPolicy.STRICT


# Catches an injected identity answer being ignored by destroy, so the
# refuse-on-unknown path would go untested.
def test_fake_inspect_result_drives_destroy():
    case, ref = _launched_fake()
    case.backend.inspect_result = 'unknown'
    with pytest.raises(BackendError):
        case.backend.destroy(ref, DestroyPolicy.BEST_EFFORT)
    assert ref.vmid in case.backend.instances


# Catches a create that dies after launch leaving no trace for cleanup to find.
def test_fake_create_failure_after_launch_leaves_instance():
    case = make_memory_backend_case(None)
    case.backend.ensure_slot(case.spec.network_slot)
    case.backend.fail_create_after_launch = True
    with pytest.raises(RuntimeError, match='injected create failure'):
        case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    assert case.spec.vmid in case.backend.instances


# Catches the one-shot pre-launch failure sticking, or launching anyway.
def test_fake_fail_next_create_is_one_shot():
    case = make_memory_backend_case(None)
    case.backend.ensure_slot(case.spec.network_slot)
    case.backend.fail_next_create = True
    with pytest.raises(RuntimeError):
        case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    assert case.spec.vmid not in case.backend.instances
    case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    assert case.spec.vmid in case.backend.instances


# Catches a cloud create launching into a slot network that no longer exists (I3).
def test_fake_create_requires_slot_and_lose_slot_once_drops_it():
    case = make_memory_backend_case(None)
    with pytest.raises(SlotNetworkMissing):
        case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    case.backend.ensure_slot(case.spec.network_slot)
    case.backend.lose_slot_once = True
    with pytest.raises(SlotNetworkMissing):
        case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    assert case.spec.network_slot not in case.backend.slot_networks
    assert case.backend.lose_slot_once is False
    case.backend.ensure_slot(case.spec.network_slot)
    case.backend.create(case.spec, 'memory-zone', heartbeat=lambda: None)
    assert case.spec.vmid in case.backend.instances


# Catches a failed expiry write looking successful (ruling I2).
def test_fake_set_expiry_failure_raises_backend_error():
    case, ref = _launched_fake()
    before = dict(case.backend.expiry)
    case.backend.fail_set_expiry = True
    with pytest.raises(BackendError):
        case.backend.set_expiry(ref, datetime.now() + timedelta(hours=5))
    assert case.backend.expiry == before


# Catches lock-scope assertions (T7) reading call lists instead of the locks
# actually held by this thread when the operation ran.
def test_fake_records_lifecycle_locks_held_on_entry():
    backend = FakeBackend('aws')
    with vm_lifecycle_lock(-1):
        backend.ensure_router_running()
    with vm_lifecycle_lock(-10101):
        backend.ensure_slot(101)
    backend.create(make_memory_backend_case(None).spec, 'memory-zone',
                   heartbeat=lambda: None)
    assert backend.observed_locks == {
        'ensure_router_running': {-1},
        'ensure_slot': {-10101},
        'create': set(),
    }
