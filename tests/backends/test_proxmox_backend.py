"""ProxmoxBackend read/destroy half against the stateful FakeProxmox.

The shared contract suite only smoke-tests destroy policies; the stop-failure
semantics each policy stands for are pinned here, against the provider state
the fake holds, because only Proxmox has a separate stop step.
"""

import time
from dataclasses import replace
from datetime import timedelta

import pytest

from app.backends.base import (
    GUEST_ABSENT, GUEST_MATCH, GUEST_MISMATCH, BackendError, DestroyPolicy,
    DestroyRefused,
)
from app.proxmox_service import (
    RESERVATION_TTL_SECONDS, reset_cluster_vm_index,
)
from app import proxmox_service
from tests.backends.proxmox_case import (
    GUEST_IP, TEMPLATE_VMID, build_proxmox_case, forget_proxmox_caches,
)
from tests.test_live_contract_guards import _missing


@pytest.fixture
def proxmox_case():
    yield build_proxmox_case()
    forget_proxmox_caches()


@pytest.fixture
def proxmox_case_factory():
    yield build_proxmox_case
    forget_proxmox_caches()


@pytest.fixture
def task_waits(monkeypatch):
    """Every (node, upid, timeout, poll_interval) the adapter waited on.

    The arguments are the contract here: they are what each source path
    passed. A UPID the fake never issued (a falsy one) answers non-OK at
    once, where real polling would spin until the timeout.
    """
    waits = []
    real = proxmox_service.wait_for_proxmox_task

    def recording(proxmox, node, upid, timeout=300, poll_interval=3):
        waits.append((node, upid, timeout, poll_interval))
        if upid not in proxmox.tasks:
            return {'success': False, 'status': 'stopped', 'error': 'no such task'}
        return real(proxmox, node, upid, timeout=timeout,
                    poll_interval=poll_interval)
    monkeypatch.setattr('app.proxmox_service.wait_for_proxmox_task', recording)
    return waits


def _answer_none(api, monkeypatch, method, rest):
    """Perform the operation, then answer None instead of its UPID."""
    answer = api.handle

    def handle(m, parts, kwargs):
        result = answer(m, parts, kwargs)
        if m == method and parts[0] == 'nodes' and parts[4:] == rest:
            return None
        return result
    monkeypatch.setattr(api, 'handle', handle)


# --- destroy policies --------------------------------------------------------

# Catches expiry deleting a VM after its stop failed.
def test_strict_refuses_delete_after_stop_failure(proxmox_case):
    case = proxmox_case
    case.api.fail_stop.add(case.ref.vmid)
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, DestroyPolicy.STRICT)
    assert case.api.guests[case.ref.vmid]['name'] == case.ref.vm_name
    assert case.ref.vmid not in case.api.delete_kwargs


# Catches expiry no longer deleting a guest whose stop succeeded.
def test_strict_stops_then_deletes_a_healthy_guest(proxmox_case):
    case = proxmox_case
    case.backend.destroy(case.ref, DestroyPolicy.STRICT)
    assert case.ref.vmid not in case.api.guests
    assert case.api.delete_kwargs[case.ref.vmid] == {}


# Catches failed-deploy/admin cleanup regressing to strict semantics.
def test_best_effort_deletes_after_stop_failure(proxmox_case):
    case = proxmox_case
    case.api.fail_stop.add(case.ref.vmid)
    case.backend.destroy(case.ref, DestroyPolicy.BEST_EFFORT)
    assert case.ref.vmid not in case.api.guests


# Catches corrupt QEMU/LXC configs becoming undeletable or using the wrong force flags.
@pytest.mark.parametrize('vm_type,expected', [
    ('qemu', {'purge': 1, 'skiplock': 1}),
    ('lxc', {'purge': 1, 'force': 1}),
])
def test_force_corrupt_uses_existing_provider_flags(
        proxmox_case_factory, vm_type, expected):
    case = proxmox_case_factory(vm_type=vm_type, corrupt_config=True)
    case.backend.destroy(case.ref, DestroyPolicy.FORCE_CORRUPT)
    assert case.api.delete_kwargs[case.ref.vmid] == expected
    assert case.ref.vmid not in case.api.guests


# Catches the student destroy force-deleting (skiplock) a guest whose config
# is healthy, or refusing it after an ordinary stop failure.
def test_force_corrupt_on_a_readable_guest_is_a_plain_best_effort_delete(
        proxmox_case):
    case = proxmox_case
    case.api.fail_stop.add(case.ref.vmid)
    case.backend.destroy(case.ref, DestroyPolicy.FORCE_CORRUPT)
    assert case.api.delete_kwargs[case.ref.vmid] == {}
    assert case.ref.vmid not in case.api.guests


def _replace_guest(case, name):
    """Another guest now holds the VMID: inventory and config both name it."""
    guest = case.api.guests[case.ref.vmid]
    guest['name'] = name
    guest['config']['name'] = name


# Catches a stale request deleting a replacement at the same VMID.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_every_policy_refuses_identity_mismatch(proxmox_case, policy):
    case = proxmox_case
    _replace_guest(case, 'replacement')
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, policy)
    assert case.api.guests[case.ref.vmid]['name'] == 'replacement'
    assert case.api.mutations(case.ref.vmid) == []


# Catches an inventory outage being read as permission to mutate.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_every_policy_fails_closed_when_identity_is_unknown(proxmox_case, policy):
    case = proxmox_case
    case.api.cluster_error = RuntimeError('inventory unavailable')
    with pytest.raises(BackendError):
        case.backend.destroy(case.ref, policy)
    assert case.api.mutations(case.ref.vmid) == []
    assert case.ref.vmid in case.api.guests


# Catches a destroy of an already-gone guest raising, which would keep the
# caller retrying a VM that no longer exists.
def test_destroy_of_an_absent_guest_succeeds_without_mutating(proxmox_case):
    case = proxmox_case
    del case.api.guests[case.ref.vmid]
    case.backend.destroy(case.ref, DestroyPolicy.STRICT)
    assert case.api.mutations(case.ref.vmid) == []


# Catches a failed delete reported as success, which would retire the
# record of a guest that is still running.
def test_failed_delete_raises_and_keeps_the_guest(proxmox_case):
    case = proxmox_case
    case.api.fail_delete.add(case.ref.vmid)
    with pytest.raises(BackendError):
        case.backend.destroy(case.ref, DestroyPolicy.BEST_EFFORT)
    assert case.ref.vmid in case.api.guests


# Catches destroy trusting an inventory snapshot cached before a replacement
# appeared at the VMID.
def test_destroy_reads_fresh_inventory_even_when_one_is_cached(proxmox_case):
    case = proxmox_case
    reset_cluster_vm_index()
    assert case.backend.status(case.ref) == 'running'  # caches the index
    _replace_guest(case, 'replacement')
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, DestroyPolicy.BEST_EFFORT)
    assert case.api.guests[case.ref.vmid]['name'] == 'replacement'


# Catches the stop or delete waits drifting from the 60 s / 120 s, 1 s poll
# every source path passes.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_each_policy_waits_with_the_source_timeouts(
        proxmox_case, task_waits, policy):
    case = proxmox_case
    case.backend.destroy(case.ref, policy)
    assert [(node, timeout, poll) for node, _upid, timeout, poll in task_waits] == [
        ('node-a', 60, 1), ('node-a', 120, 1)]
    assert all(upid in case.api.tasks for _n, upid, _t, _p in task_waits)


# Catches the student destroy polling a delete that returned no UPID: it has
# only ever waited on one it got back (vm_lifecycle.py delete_upid is not None).
def test_force_corrupt_does_not_poll_a_missing_delete_upid(
        proxmox_case, task_waits, monkeypatch):
    case = proxmox_case
    _answer_none(case.api, monkeypatch, 'DELETE', ())
    case.backend.destroy(case.ref, DestroyPolicy.FORCE_CORRUPT)
    assert case.ref.vmid not in case.api.guests
    assert [(timeout, poll) for _n, _u, timeout, poll in task_waits] == [(60, 1)]


# Catches the stop-wait condition being normalized: expiry skips the wait on a
# falsy stop UPID; admin, failed-deploy and student destroy always waited.
@pytest.mark.parametrize('policy,waits_on_none', [
    (DestroyPolicy.STRICT, False),
    (DestroyPolicy.BEST_EFFORT, True),
    (DestroyPolicy.FORCE_CORRUPT, True),
])
def test_stop_wait_on_a_falsy_upid_follows_the_source_path(
        proxmox_case, task_waits, monkeypatch, policy, waits_on_none):
    case = proxmox_case
    _answer_none(case.api, monkeypatch, 'POST', ('status', 'stop'))
    case.backend.destroy(case.ref, policy)
    assert case.ref.vmid not in case.api.guests
    assert any(upid is None for _n, upid, _t, _p in task_waits) is waits_on_none


# Catches BEST_EFFORT issuing a DELETE to a guest fresh inventory already
# proves gone after its stop raised (failed-deploy cleanup's confirmation).
def test_best_effort_confirms_absence_after_a_stop_error_without_deleting(
        proxmox_case, monkeypatch):
    case = proxmox_case
    vmid, node = case.ref.vmid, case.ref.location
    case.api.fail_delete.add(vmid)
    answer = case.api.handle

    def guest_vanishes_on_stop(method, parts, kwargs):
        if method == 'POST' and parts[4:] == ('status', 'stop'):
            case.api.guests.pop(vmid, None)
            raise _missing(node, vmid)
        return answer(method, parts, kwargs)
    monkeypatch.setattr(case.api, 'handle', guest_vanishes_on_stop)
    case.backend.destroy(case.ref, DestroyPolicy.BEST_EFFORT)
    assert not [call for call in case.api.calls if call[0] == 'DELETE']
    assert vmid not in case.api.delete_kwargs


# --- identity and absence ----------------------------------------------------

# Catches inspect comparing anything other than name, node and type.
def test_inspect_reports_match_mismatch_and_absence(proxmox_case):
    case = proxmox_case
    assert case.backend.inspect(case.ref) == GUEST_MATCH
    assert case.backend.inspect(replace(case.ref, location='node-b')) == GUEST_MISMATCH
    assert case.backend.inspect(replace(case.ref, vm_type='lxc')) == GUEST_MISMATCH
    assert case.backend.inspect(replace(case.ref, vmid=4242)) == GUEST_ABSENT


# The ~10 s after a clone: /cluster/resources reports the name empty, or as
# the previous guest's at a reused VMID, while the guest's config already
# names it (measured live, LIVE-REPORT I1/I2). Catches the portal refusing
# to destroy (or clean up) its own fresh guest during that window.
@pytest.mark.parametrize('listed_name', [None, '', 'student-previous-vm'],
                         ids=['none', 'empty', 'stale-previous-name'])
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_inspect_trusts_the_guest_config_while_the_listed_name_lags(
        proxmox_case, listed_name, policy):
    case = proxmox_case
    case.api.guests[case.ref.vmid]['name'] = listed_name
    assert case.backend.inspect(case.ref) == GUEST_MATCH
    case.backend.destroy(case.ref, policy)
    assert case.ref.vmid not in case.api.guests


# Catches the config fallback admitting a guest that is genuinely someone
# else's, whatever the lagging listing says about it, including a name that
# only shares the expected name as a prefix (review M2).
@pytest.mark.parametrize('foreign', ['other', 'prefix', 'truncated'])
@pytest.mark.parametrize('listed_name', [None, 'student-previous-vm',
                                         'student-other-vm'])
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_inspect_refuses_a_foreign_guest_even_while_the_listing_lags(
        proxmox_case, foreign, listed_name, policy):
    case = proxmox_case
    guest = case.api.guests[case.ref.vmid]
    guest['name'] = listed_name
    guest['config']['name'] = {'other': 'student-other-vm',
                               'prefix': case.ref.vm_name + '-foreign',
                               'truncated': case.ref.vm_name[:-1]}[foreign]
    assert case.backend.inspect(case.ref) == GUEST_MISMATCH
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, policy)
    assert case.api.mutations(case.ref.vmid) == []


def _after_the_stop(case, monkeypatch, change):
    """Run `change` once the stop has been handled, before anything else."""
    answer = case.api.handle

    def handle(method, parts, kwargs):
        result = answer(method, parts, kwargs)
        if method == 'POST' and parts[4:] == ('status', 'stop'):
            change()
        return result
    monkeypatch.setattr(case.api, 'handle', handle)


def _deletes(case):
    return [call for call in case.api.calls if call[0] == 'DELETE']


# Review I1: catches the delete trusting the identity read before the stop,
# which may wait up to a minute, while another guest took the VMID.
@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_destroy_rechecks_identity_between_the_stop_and_the_delete(
        proxmox_case, monkeypatch, policy):
    case = proxmox_case

    def replaced():
        del case.api.guests[case.ref.vmid]
        case.api.add_guest(case.ref.vmid, 'student-intruder-vm', node=case.ref.location,
                           status='running', vm_type=case.ref.vm_type)
    _after_the_stop(case, monkeypatch, replaced)
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, policy)
    assert _deletes(case) == []
    assert case.api.guests[case.ref.vmid]['name'] == 'student-intruder-vm'


@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_destroy_refuses_the_delete_when_identity_turns_unreadable_after_the_stop(
        proxmox_case, monkeypatch, policy):
    case = proxmox_case
    _after_the_stop(case, monkeypatch, lambda: setattr(
        case.api, 'cluster_error', RuntimeError('inventory unavailable')))
    with pytest.raises(BackendError):
        case.backend.destroy(case.ref, policy)
    assert _deletes(case) == []
    assert case.ref.vmid in case.api.guests


@pytest.mark.parametrize('policy', list(DestroyPolicy))
def test_destroy_is_done_when_the_guest_vanishes_after_the_stop(
        proxmox_case, monkeypatch, policy):
    case = proxmox_case
    _after_the_stop(case, monkeypatch, lambda: case.api.guests.pop(case.ref.vmid))
    case.backend.destroy(case.ref, policy)
    assert _deletes(case) == []


# Catches an unreadable config being taken as proof while the listed name
# disagrees.
def test_inspect_refuses_when_the_config_cannot_confirm_a_lagging_name(
        proxmox_case):
    case = proxmox_case
    case.api.guests[case.ref.vmid]['name'] = None
    case.api.corrupt_config.add(case.ref.vmid)
    assert case.backend.inspect(case.ref) == GUEST_MISMATCH
    with pytest.raises(DestroyRefused):
        case.backend.destroy(case.ref, DestroyPolicy.FORCE_CORRUPT)
    assert case.api.mutations(case.ref.vmid) == []


# Catches the config fallback being read on the wrong node or for the wrong
# type: only a lagging *name* is ever settled by the config.
def test_inspect_does_not_consult_config_for_a_node_or_type_mismatch(
        proxmox_case):
    case = proxmox_case
    before = len(case.api.calls)
    assert case.backend.inspect(replace(case.ref, location='node-b')) == GUEST_MISMATCH
    assert case.backend.inspect(replace(case.ref, vm_type='lxc')) == GUEST_MISMATCH
    assert not [call for call in case.api.calls[before:]
                if call[1][-1:] == ('config',)]


# Catches treating a transport error as positive absence.
def test_confirm_absent_requires_fresh_inventory(proxmox_case):
    case = proxmox_case
    case.api.cluster_error = RuntimeError('inventory unavailable')
    assert case.backend.confirm_absent(
        case.ref, RuntimeError('request failed')) is False


# Catches absence confirmed from a cached snapshot or for a listed guest.
def test_confirm_absent_answers_from_a_fresh_read(proxmox_case):
    case = proxmox_case
    assert case.backend.confirm_absent(case.ref) is False
    del case.api.guests[case.ref.vmid]
    assert case.backend.confirm_absent(case.ref) is True


# --- inventory ---------------------------------------------------------------

# Catches reconcile/contract losing templates, status, or presence evidence, and
# list_managed growing a per-guest config read (C5).
def test_list_managed_is_the_cluster_index_projection(proxmox_case):
    case = proxmox_case
    reset_cluster_vm_index()
    resources = case.backend.list_managed()
    instance = next(r for r in resources if r.vmid == case.ref.vmid)
    assert instance.kind == 'instance'
    assert instance.provider_id == str(case.ref.vmid)
    assert instance.location == case.ref.location
    assert instance.status == 'running'
    assert instance.expires_at_epoch is None
    assert any(r.is_template for r in resources)
    assert sum(1 for call in case.api.calls if call[1] == ('cluster', 'resources')) == 1
    assert not [call for call in case.api.calls if call[1][-1:] == ('config',)]


# Catches a non-app guest or a template being dropped from the snapshot.
def test_list_managed_includes_every_cluster_row(proxmox_case):
    case = proxmox_case
    case.api.add_guest(300, 'someone-elses-vm', node='node-b', status='stopped')
    resources = {r.vmid: r for r in case.backend.list_managed()}
    assert set(resources) == {case.ref.vmid, TEMPLATE_VMID, 300}
    assert resources[TEMPLATE_VMID].is_template is True
    assert resources[case.ref.vmid].is_template is False
    assert resources[300].name == 'someone-elses-vm'
    assert resources[300].location == 'node-b'
    assert all(r.backend == 'proxmox' for r in resources.values())


# Catches list_managed serving a cached snapshot to a reconcile decision.
def test_list_managed_never_serves_a_cached_index(proxmox_case):
    case = proxmox_case
    reset_cluster_vm_index()
    case.backend.status(case.ref)  # caches the index
    del case.api.guests[case.ref.vmid]
    assert case.ref.vmid not in {r.vmid for r in case.backend.list_managed()}


# Catches an inventory outage being read as "every guest is gone".
def test_list_managed_raises_when_the_cluster_read_fails(proxmox_case):
    case = proxmox_case
    reset_cluster_vm_index()
    case.api.cluster_error = RuntimeError('inventory unavailable')
    with pytest.raises(RuntimeError, match='inventory unavailable'):
        case.backend.list_managed()


# Catches reaping through an inventory row whose VMID now names another guest.
def test_delete_resource_reverifies_identity(proxmox_case):
    case = proxmox_case
    resource = next(r for r in case.backend.list_managed()
                    if r.vmid == case.ref.vmid)
    _replace_guest(case, 'replacement')
    with pytest.raises(DestroyRefused):
        case.backend.delete_resource(resource)
    assert case.api.guests[case.ref.vmid]['name'] == 'replacement'
    assert case.api.mutations(case.ref.vmid) == []


# Catches delete_resource using strict semantics or a stale vm_type.
def test_delete_resource_deletes_best_effort_with_the_fresh_type(
        proxmox_case_factory):
    case = proxmox_case_factory(vm_type='lxc')
    resource = next(r for r in case.backend.list_managed()
                    if r.vmid == case.ref.vmid)
    case.api.fail_stop.add(case.ref.vmid)
    case.backend.delete_resource(resource)
    assert case.ref.vmid not in case.api.guests
    assert ('DELETE', ('nodes', 'node-a', 'lxc', str(case.ref.vmid)), {}) \
        in case.api.calls


# Catches an orphan sweep deleting a template it found in inventory.
def test_delete_resource_refuses_a_template(proxmox_case):
    case = proxmox_case
    template = next(r for r in case.backend.list_managed() if r.is_template)
    with pytest.raises(DestroyRefused):
        case.backend.delete_resource(template)
    assert TEMPLATE_VMID in case.api.guests
    assert case.api.mutations(TEMPLATE_VMID) == []


# --- display reads -----------------------------------------------------------

# Catches /list regressing from one cluster read per render to one per VM (C5).
def test_status_and_ips_share_one_cached_cluster_read(proxmox_case):
    case = proxmox_case
    reset_cluster_vm_index()
    assert case.backend.status(case.ref) == 'running'
    assert case.backend.ip_addresses(case.ref) == ['10.1.0.5']
    assert case.backend.status(case.ref) == 'running'
    assert sum(1 for call in case.api.calls if call[1] == ('cluster', 'resources')) == 1


# Catches the ['N/A'] display sentinel leaking through the protocol, or a
# non-lab address being shown.
def test_ip_addresses_keep_lab_addresses_and_map_no_answer_to_empty(
        proxmox_case):
    case = proxmox_case
    case.api.guests[case.ref.vmid]['ips'] = [GUEST_IP, '192.0.2.7']
    assert case.backend.ip_addresses(case.ref) == [GUEST_IP]
    forget_proxmox_caches()
    case.api.guests[case.ref.vmid]['ips'] = []  # guest agent not running
    assert case.backend.ip_addresses(case.ref) == []


# Catches a student mid-SSH losing their address over one unanswered render.
def test_ip_addresses_fall_back_to_the_cache_when_state_is_unknown(
        proxmox_case, monkeypatch):
    case = proxmox_case
    assert case.backend.ip_addresses(case.ref) == [GUEST_IP]  # fills the cache
    # Proxmox stops answering: no inventory, and the per-VM config read fails
    # with a transport error rather than an unreadable-config answer.
    reset_cluster_vm_index()
    case.api.cluster_error = RuntimeError('inventory unavailable')
    answer = case.api.handle

    def transport_failure(method, parts, kwargs):
        if parts[-1:] == ('config',):
            raise ConnectionError('connection reset')
        return answer(method, parts, kwargs)
    monkeypatch.setattr(case.api, 'handle', transport_failure)
    assert case.backend.status(case.ref) == 'unknown'
    assert case.backend.ip_addresses(case.ref) == [GUEST_IP]


# Catches an unreadable-but-listed config shown as running instead of CONFIG ERROR.
def test_status_maps_an_unreadable_listed_config_to_config_error(
        proxmox_case_factory):
    case = proxmox_case_factory(corrupt_config=True)
    # A healthy index status answers present without a config read, so model
    # the row as Proxmox serves it while it cannot read the config.
    case.api.guests[case.ref.vmid]['status'] = 'unknown'
    assert case.backend.status(case.ref) == 'config_error'
    assert case.backend.ip_addresses(case.ref) == []


# --- expiry ------------------------------------------------------------------

# Catches the orphan reaper failing to parse what extend wrote.
@pytest.mark.parametrize('vm_type', ['qemu', 'lxc'])
def test_set_expiry_writes_the_local_isoformat_description(
        proxmox_case_factory, vm_type):
    case = proxmox_case_factory(vm_type=vm_type)
    new_expiry = case.expires_at + timedelta(hours=2)
    case.backend.set_expiry(case.ref, new_expiry)
    assert case.api.guests[case.ref.vmid]['config']['description'] == (
        f'Expires: {new_expiry.isoformat()}')
    assert ('PUT', ('nodes', 'node-a', vm_type, str(case.ref.vmid), 'config'),
            {'description': f'Expires: {new_expiry.isoformat()}'}) in case.api.calls


# Catches a failed Proxmox description write being counted as an extension (I2).
def test_set_expiry_raises_backend_error_on_config_failure(proxmox_case):
    case = proxmox_case
    case.api.fail_config_put = True
    with pytest.raises(BackendError):
        case.backend.set_expiry(case.ref, case.expires_at + timedelta(hours=1))
    assert case.api.guests[case.ref.vmid]['config']['description'] == (
        f'Expires: {case.expires_at.isoformat()}')


# --- hooks that do not apply to Proxmox --------------------------------------

# Catches a cloud-only hook reaching the Proxmox API.
def test_cloud_network_and_router_hooks_do_not_touch_proxmox(proxmox_case):
    case = proxmox_case
    before = {vmid: dict(guest) for vmid, guest in case.api.guests.items()}
    case.backend.cleanup_deployment('a' * 32)
    case.backend.ensure_slot(101)
    case.backend.release_slot(101)
    case.backend.ensure_router_running()
    case.backend.stop_router()
    assert case.api.calls == []
    assert case.api.guests == before


# --- admission ---------------------------------------------------------------

# Catches Proxmox admission renew/release drifting from the reservation table
# the node-admission check sums.
def test_admission_renew_and_release_round_trip_a_reservation_row(
        app, proxmox_case):
    from app.models import NodeMemoryReservation, db
    case = proxmox_case
    with app.app_context():
        expires_at = time.time() + 60
        db.session.add(NodeMemoryReservation(
            reservation_id='reservation-a', node='node-a',
            memory_bytes=1024 ** 3, expires_at=expires_at))
        db.session.commit()

        assert case.backend.renew_admission('reservation-a') is True
        db.session.expire_all()
        row = db.session.get(NodeMemoryReservation, 'reservation-a')
        assert row.expires_at >= time.time() + RESERVATION_TTL_SECONDS - 5

        assert case.backend.release_admission('reservation-a') is True
        db.session.expire_all()
        assert db.session.get(NodeMemoryReservation, 'reservation-a') is None
        assert case.backend.renew_admission('reservation-a') is False
        assert case.backend.release_admission('reservation-a') is False
        assert case.backend.release_admission(None) is False
