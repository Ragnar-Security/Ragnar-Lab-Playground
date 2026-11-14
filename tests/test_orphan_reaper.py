"""Regression tests for guests and cloud resources orphaned by an interrupted deploy."""

import importlib
import time
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from app import coordination
from app.alert_outbox import dispatch_pending_alerts
from app.backends import register_backend_factory, reset_backends_for_tests
from app.backends.aws import AWSBackend
from app.backends.base import BackendError, DestroyRefused, ManagedResource
from app.backends.cloud_common import (
    TAG_CREATED, TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT,
    TAG_VMID, deployment_network_name, slot_network_name,
)
from app.backends.gcp import GCPBackend
from app.backends.network_slots import cloud_slot_cidr
from app.models import (
    AdminAlert, KothBox, KothGame, NodeMemoryReservation, VMIDReservation,
    VMRecord, db,
)
from app.proxmox_service import (
    _clear_vmid_reservations,
    get_next_available_vmid,
    get_proxmox_connection,
    release_vmid_reservation,
)
from tests.backends.fake_backend import (
    AWS_TEST_CONFIG, AWS_TEST_ENV, register_aws_test_image,
)
from tests.backends.fake_gce import GCP_TEST_CONFIG, FakeGCESession

config = importlib.import_module('app.config')

try:
    from app import orphan_reaper
except ImportError:
    orphan_reaper = None


NOW = datetime(2026, 10, 23, 12, 0, 0)


class ProxmoxBoundary:
    """Small ProxmoxAPI-shaped boundary fake with observable guest endpoints."""

    def __init__(self, resources, descriptions=None, task_results=None,
                 operation_errors=None):
        self.api = MagicMock(name="ProxmoxAPI")
        self.api.cluster.resources.get.return_value = resources
        self.guests = {}
        descriptions = descriptions or {}
        task_results = task_results or {}
        operation_errors = operation_errors or {}

        nodes = {}
        guest_maps = {}
        for row in resources:
            vmid = int(row["vmid"])
            node_name = row.get("node", "pve")
            vm_type = row.get("type", "qemu")
            guest = MagicMock(name=f"{vm_type}-{vmid}")
            description = descriptions.get(vmid, f"Expires: {(NOW - timedelta(hours=2)).isoformat()}")
            guest.config.get.return_value = {"description": description}
            guest.status.stop.post.return_value = f"UPID:stop:{vmid}"
            guest.delete.return_value = f"UPID:delete:{vmid}"
            if (vmid, "config") in operation_errors:
                guest.config.get.side_effect = operation_errors[(vmid, "config")]
            if (vmid, "stop") in operation_errors:
                guest.status.stop.post.side_effect = operation_errors[(vmid, "stop")]
            if (vmid, "delete") in operation_errors:
                guest.delete.side_effect = operation_errors[(vmid, "delete")]
            self.guests[vmid] = guest

            node = nodes.setdefault(node_name, MagicMock(name=f"node-{node_name}"))
            guest_maps.setdefault((node_name, vm_type), {})[vmid] = guest

        for (node_name, vm_type), mapping in guest_maps.items():
            endpoint = getattr(nodes[node_name], vm_type)
            endpoint.side_effect = (
                lambda requested, guests=mapping: guests[int(requested)]
            )

        for node_name, node in nodes.items():
            def task_for(upid, results=task_results):
                result = results.get(upid, {"status": "stopped", "exitstatus": "OK"})
                task = MagicMock(name=f"task-{upid}")
                task.status.get.return_value = result
                return task
            node.tasks.side_effect = task_for

        self.api.nodes.side_effect = lambda requested: nodes[requested]

    def connection(self):
        # The fake is installed at the same constructor boundary production uses.
        return patch("app.proxmox_service.ProxmoxAPI", return_value=self.api)

    def assert_untouched(self, vmid):
        guest = self.guests[vmid]
        guest.status.stop.post.assert_not_called()
        guest.delete.assert_not_called()


def _resource(vmid=700, name="student-alice-web", vm_type="qemu",
              status="running", **extra):
    return {"vmid": vmid, "name": name, "node": "pve", "type": vm_type,
            "status": status, **extra}


def _run(boundary, **kwargs):
    assert orphan_reaper is not None, "orphan reaper module must exist"
    with boundary.connection():
        proxmox = get_proxmox_connection()
        return orphan_reaper.reap_orphaned_vms(proxmox, now=NOW, **kwargs)


def _record(vmid=700, *, node="pve", destroyed=False):
    return VMRecord(
        vmid=vmid, vm_name="student-alice-web", vm_type="qemu", node=node,
        template_name="homework/web", student_name="alice",
        expires_at=NOW - timedelta(hours=4), destroyed=destroyed,
    )


def test_non_app_name_is_never_touched(app):
    boundary = ProxmoxBoundary([_resource(name="production-database")])
    with app.app_context():
        _run(boundary)
    boundary.assert_untouched(700)
    boundary.guests[700].config.get.assert_not_called()


def test_template_is_never_touched(app):
    boundary = ProxmoxBoundary([_resource(template=1)])
    with app.app_context():
        _run(boundary)
    boundary.assert_untouched(700)
    boundary.guests[700].config.get.assert_not_called()


def test_any_existing_record_including_destroyed_protects_guest(app):
    boundary = ProxmoxBoundary([_resource()])
    with app.app_context():
        db.session.add(_record(destroyed=True))
        db.session.commit()
        _run(boundary)
    boundary.assert_untouched(700)
    boundary.guests[700].config.get.assert_not_called()


def test_record_for_same_node_and_vmid_protects_guest(app):
    boundary = ProxmoxBoundary([_resource()])
    with app.app_context():
        db.session.add(_record(node="pve"))
        db.session.commit()
        _run(boundary)
    boundary.assert_untouched(700)


def test_missing_expiration_is_never_touched(app):
    boundary = ProxmoxBoundary([_resource()], descriptions={700: "owner: alice"})
    with app.app_context():
        _run(boundary)
    boundary.assert_untouched(700)


def test_unparseable_expiration_is_never_touched(app):
    boundary = ProxmoxBoundary([_resource()], descriptions={700: "Expires: eventually"})
    with app.app_context():
        _run(boundary)
    boundary.assert_untouched(700)


def test_expiration_inside_grace_period_is_never_touched(app):
    boundary = ProxmoxBoundary(
        [_resource()],
        descriptions={700: f"Expires: {(NOW - timedelta(minutes=59)).isoformat()}"},
    )
    with app.app_context():
        _run(boundary, grace_minutes=60)
    boundary.assert_untouched(700)


def test_koth_box_reference_protects_guest(app):
    boundary = ProxmoxBoundary([_resource()])
    with app.app_context():
        game = KothGame(name="arena", deployment_group_name="lab",
                        entry_vlan_tag=300, internal_vlan_tag=400)
        db.session.add(game)
        db.session.flush()
        db.session.add(KothBox(game_id=game.id, name="student-alice-web"))
        db.session.commit()
        _run(boundary)
    boundary.assert_untouched(700)
    boundary.guests[700].config.get.assert_not_called()


@pytest.mark.parametrize("vm_type", ["qemu", "lxc"])
def test_expired_orphan_is_stopped_deleted_awaited_and_alerted(app, vm_type):
    boundary = ProxmoxBoundary([_resource(vm_type=vm_type)])
    with app.app_context():
        results = _run(boundary)
        dispatch_pending_alerts()
        alerts = AdminAlert.query.filter_by(alert_type="vm_orphan_reaped").all()

    guest = boundary.guests[700]
    guest.status.stop.post.assert_called_once_with()
    guest.delete.assert_called_once_with()
    node = boundary.api.nodes("pve")
    assert node.tasks.call_count == 2
    assert results == [{"vmid": 700, "node": "pve", "name": "student-alice-web",
                        "status": "reaped"}]
    assert len(alerts) == 1
    assert alerts[0].vmid == 700

def test_already_stopped_orphan_is_deleted_and_awaited(app):
    boundary = ProxmoxBoundary([_resource(vm_type="lxc", status="stopped")])
    with app.app_context():
        results = _run(boundary)

    guest = boundary.guests[700]
    guest.status.stop.post.assert_not_called()
    guest.delete.assert_called_once_with()
    assert boundary.api.nodes("pve").tasks.call_count == 1
    assert results[0]["status"] == "reaped"


def test_failed_delete_task_is_reported_not_reaped(app):
    boundary = ProxmoxBoundary(
        [_resource()],
        task_results={"UPID:delete:700": {
            "status": "stopped", "exitstatus": "ERROR", "errors": "locked"
        }},
    )
    with app.app_context():
        results = _run(boundary)
        assert AdminAlert.query.filter_by(alert_type="vm_orphan_reaped").count() == 0

    assert results == [{"vmid": 700, "node": "pve", "name": "student-alice-web",
                        "status": "delete_failed", "error": "locked"}]


# Review I1 (pre-existing in v1.0.0's sweep): catches the delete acting on a
# VMID another guest took while the stop waited, or the stop acting on one
# taken since the sweep's listing.
@pytest.mark.parametrize("when", ["before stop", "after stop"])
@pytest.mark.parametrize("replacement", [
    {"name": "student-bob-db"},
    {"name": "student-alice-web", "node": "pve2"},
    None,
], ids=["other-name", "other-node", "vanished"])
def test_identity_is_rechecked_before_the_stop_and_before_the_delete(
        app, when, replacement):
    boundary = ProxmoxBoundary([_resource()])
    listing = boundary.api.cluster.resources.get
    sweep_rows = [dict(row) for row in listing.return_value]
    later_rows = [] if replacement is None else [{**sweep_rows[0], **replacement}]
    guest = boundary.guests[700]
    if when == "before stop":
        reads = iter([sweep_rows])
        listing.side_effect = lambda **_: next(reads, later_rows)
    else:
        def stop():
            listing.return_value = later_rows
            return "UPID:stop:700"
        guest.status.stop.post.side_effect = stop
    with app.app_context():
        results = _run(boundary)

    guest.delete.assert_not_called()
    if when == "before stop":
        guest.status.stop.post.assert_not_called()
    expected = "not_found" if replacement is None else "identity_unverified"
    assert [r["status"] for r in results] == [expected]


# A failed inventory read during either recheck must refuse, never fall
# through to the stop or the delete.
@pytest.mark.parametrize("when", ["before stop", "after stop"])
def test_identity_recheck_inventory_error_refuses(app, when):
    boundary = ProxmoxBoundary([_resource()])
    listing = boundary.api.cluster.resources.get
    sweep_rows = [dict(row) for row in listing.return_value]
    guest = boundary.guests[700]

    def unreachable(**_):
        raise ConnectionError("cluster/resources unreachable")

    if when == "before stop":
        reads = iter([sweep_rows])

        def listing_read(**kwargs):
            rows = next(reads, None)
            if rows is None:
                return unreachable(**kwargs)
            return rows
        listing.side_effect = listing_read
    else:
        def stop():
            listing.side_effect = unreachable
            return "UPID:stop:700"
        guest.status.stop.post.side_effect = stop
    with app.app_context():
        results = _run(boundary)

    guest.delete.assert_not_called()
    if when == "before stop":
        guest.status.stop.post.assert_not_called()
    assert [r["status"] for r in results] == ["identity_unverified"]


def test_active_vmid_reservation_protects_expired_orphan_in_dry_run(app):
    boundary = ProxmoxBoundary([_resource()])
    allocator_view = MagicMock()
    allocator_view.cluster.resources.get.return_value = [{'vmid': 699}]
    _clear_vmid_reservations()
    try:
        # The create path reserves through the app database, which is what
        # the reaper (possibly in another worker) reads.
        with app.app_context():
            assert get_next_available_vmid(
                allocator_view,
                reservation_id='active-create',
            ) == 700
            results = _run(boundary, dry_run=True)
        assert results == []
        boundary.assert_untouched(700)
        boundary.guests[700].config.get.assert_not_called()
    finally:
        release_vmid_reservation('active-create')
        _clear_vmid_reservations()


def test_vmid_reservation_is_rechecked_immediately_before_stop(app):
    boundary = ProxmoxBoundary([_resource()])
    allocator_view = MagicMock()
    allocator_view.cluster.resources.get.return_value = [{'vmid': 699}]
    _clear_vmid_reservations()

    def reserve_during_config_read():
        assert get_next_available_vmid(
            allocator_view,
            reservation_id='racing-create',
        ) == 700
        return {
            'description': (
                f'Expires: {(NOW - timedelta(hours=2)).isoformat()}')
        }

    boundary.guests[700].config.get.side_effect = reserve_during_config_read
    try:
        with app.app_context():
            results = _run(boundary)
        assert results == []
        boundary.assert_untouched(700)
    finally:
        release_vmid_reservation('racing-create')
        _clear_vmid_reservations()


def test_commit_then_reservation_release_handoff_protects_guest(app):
    boundary = ProxmoxBoundary([_resource()])
    reservation_checks = 0

    def commit_during_final_reservation_check(vmid):
        nonlocal reservation_checks
        reservation_checks += 1
        if reservation_checks == 2:
            db.session.add(_record(vmid))
            db.session.commit()
        return False

    with app.app_context(), patch.object(
        orphan_reaper,
        'is_vmid_reserved',
        side_effect=commit_during_final_reservation_check,
    ):
        results = _run(boundary)

    assert results == []
    boundary.assert_untouched(700)

def test_reaper_waits_for_destroy_path_holding_the_vmid_lock(app):
    """A destroy or create path that holds vm_lifecycle_lock(vmid) may commit a
    VMRecord before releasing it. The reaper must decide only after acquiring
    the same lock, so it sees that row and leaves the guest alone."""
    import threading
    from app.proxmox_service import vm_lifecycle_lock

    boundary = ProxmoxBoundary([_resource()])
    lock_held = threading.Event()
    release = threading.Event()

    def lifecycle_owner():
        with app.app_context(), vm_lifecycle_lock(700):
            lock_held.set()
            release.wait(5)
            db.session.add(_record(700))
            db.session.commit()

    results = {}

    def reaper():
        with app.app_context():
            results['reports'] = _run(boundary)

    owner = threading.Thread(target=lifecycle_owner)
    owner.start()
    assert lock_held.wait(5)
    sweeper = threading.Thread(target=reaper)
    sweeper.start()
    # Without the lock the sweep finishes here and has already destroyed 700.
    sweeper.join(0.5)
    release.set()
    owner.join(5)
    sweeper.join(5)

    assert results['reports'] == []
    boundary.assert_untouched(700)



def test_dry_run_never_calls_mutating_endpoints(app):
    boundary = ProxmoxBoundary([_resource()])
    with app.app_context():
        results = _run(boundary, dry_run=True)
        assert AdminAlert.query.count() == 0
    boundary.assert_untouched(700)
    assert results[0]["status"] == "dry_run"


def test_inventory_is_single_call_and_configs_only_read_for_untracked_app_guests(app):
    resources = [
        _resource(700, "student-alice-web"),
        _resource(701, "group-red-router"),
        _resource(702, "production-database"),
        _resource(703, "student-carol-db"),
        _resource(704, "student-dan-template", template=1),
    ]
    future = f"Expires: {(NOW + timedelta(hours=2)).isoformat()}"
    boundary = ProxmoxBoundary(resources, descriptions={700: future, 701: future})
    with app.app_context():
        db.session.add(_record(703))
        db.session.commit()
        _run(boundary)

    boundary.api.cluster.resources.get.assert_called_once_with(type="vm")
    assert boundary.guests[700].config.get.call_count == 1
    assert boundary.guests[701].config.get.call_count == 1
    assert boundary.guests[702].config.get.call_count == 0
    assert boundary.guests[703].config.get.call_count == 0
    assert boundary.guests[704].config.get.call_count == 0


class _ProxmoxError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# Real Proxmox answers a request for a missing guest with HTTP 500 and this
# message, not 404 (verified against a live PVE 9 cluster).
_MISSING = "500 Internal Server Error: Configuration file 'nodes/pve/qemu-server/700.conf' does not exist"


def _vanishes_after_first_inventory(boundary, resources):
    # The sweep's own inventory still lists the guest; the fresh read used to
    # confirm absence does not.
    boundary.api.cluster.resources.get.side_effect = [resources, []]


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_missing_guest_in_real_proxmox_shape_is_reported_not_found(app, operation):
    resources = [_resource(status="running" if operation == "stop" else "stopped")]
    boundary = ProxmoxBoundary(
        resources,
        operation_errors={(700, operation): _ProxmoxError(_MISSING, status_code=500)},
    )
    _vanishes_after_first_inventory(boundary, resources)
    with app.app_context():
        result = _run(boundary)
    assert result[0]["status"] == "not_found"


def test_missing_message_is_not_trusted_while_inventory_still_lists_the_guest(app):
    boundary = ProxmoxBoundary(
        [_resource()],
        operation_errors={(700, "stop"): _ProxmoxError(_MISSING, status_code=500)},
    )
    with app.app_context():
        result = _run(boundary)
    assert result[0]["status"] == "stop_failed"
    boundary.guests[700].delete.assert_not_called()


def test_404_counts_as_gone_only_when_fresh_inventory_agrees(app):
    resources = [_resource()]
    boundary = ProxmoxBoundary(
        resources,
        operation_errors={(700, "stop"): _ProxmoxError("gone", status_code=404)},
    )
    _vanishes_after_first_inventory(boundary, resources)
    with app.app_context():
        result = _run(boundary)
    assert result[0]["status"] == "not_found"
    boundary.guests[700].delete.assert_not_called()

    transport = ProxmoxBoundary(
        [_resource(701, "student-bob-web")],
        operation_errors={(701, "stop"): _ProxmoxError("request /qemu/404 failed")},
    )
    with app.app_context():
        result = _run(transport)
    assert result[0]["status"] == "stop_failed"
    transport.guests[701].delete.assert_not_called()


def test_periodic_hook_honors_kill_switch_and_ten_minute_throttle(app):
    boundary = ProxmoxBoundary([_resource()])
    assert orphan_reaper is not None, "orphan reaper module must exist"
    with app.app_context(), boundary.connection(), \
            patch.object(orphan_reaper, "ORPHAN_REAPER_ENABLED", False):
        proxmox = get_proxmox_connection()
        assert orphan_reaper.maybe_reap_orphaned_vms(proxmox, monotonic_now=1.0) == []
    boundary.api.cluster.resources.get.assert_not_called()

    with app.app_context(), boundary.connection(), \
            patch.object(orphan_reaper, "ORPHAN_REAPER_ENABLED", True), \
            patch.object(orphan_reaper, "reap_orphaned_vms", return_value=[]) as sweep:
        proxmox = get_proxmox_connection()
        orphan_reaper.reset_reaper_schedule()
        orphan_reaper.maybe_reap_orphaned_vms(proxmox, monotonic_now=1.0)
        orphan_reaper.maybe_reap_orphaned_vms(proxmox, monotonic_now=600.0)
        orphan_reaper.maybe_reap_orphaned_vms(proxmox, monotonic_now=601.0)
    assert sweep.call_count == 2


class _SlowTaskClock:
    """Fake monotonic clock that Proxmox task waits advance by their maximum.

    Each orphan's stop and delete waits use their full 60 s / 120 s timeouts,
    the worst case a hung Proxmox task can cost one sweep.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait_for_task(self, _proxmox, _node, _upid, *, timeout, poll_interval):
        self.now += timeout
        return {"success": True, "status": "stopped"}


def _many_orphans(count):
    return ProxmoxBoundary([
        _resource(vmid=700 + index, name=f"student-s{index}-web")
        for index in range(count)
    ])


def test_slow_orphans_cannot_hold_a_sweep_past_its_time_budget(app):
    boundary = _many_orphans(20)
    clock = _SlowTaskClock()
    with app.app_context(), patch.object(
            orphan_reaper, "wait_for_proxmox_task",
            side_effect=clock.wait_for_task):
        results = _run(boundary, max_seconds=300, clock=clock)

    # A guest is only started inside the budget; the one in flight when the
    # budget runs out may finish its own stop (60 s) + delete (120 s) waits.
    assert clock.now <= 300 + 180
    assert [r["status"] for r in results] == ["reaped", "reaped"]
    for vmid in range(702, 720):
        boundary.assert_untouched(vmid)
        boundary.guests[vmid].config.get.assert_not_called()


def test_sweep_stops_after_its_guest_budget(app):
    boundary = _many_orphans(20)
    with app.app_context():
        results = _run(boundary, max_candidates=3)

    assert [r["vmid"] for r in results] == [700, 701, 702]
    for vmid in range(703, 720):
        boundary.assert_untouched(vmid)


def test_default_budget_leaves_a_normal_sweep_unchanged(app):
    boundary = _many_orphans(3)
    with app.app_context():
        results = _run(boundary)

    assert [r["status"] for r in results] == ["reaped"] * 3


def test_periodic_sweep_honours_configured_guest_budget(app):
    boundary = _many_orphans(5)
    with app.app_context(), boundary.connection(), \
            patch.object(orphan_reaper, "ORPHAN_REAPER_ENABLED", True), \
            patch.object(orphan_reaper, "ORPHAN_REAPER_MAX_PASS_GUESTS", 2):
        orphan_reaper.reset_reaper_schedule()
        results = orphan_reaper.maybe_reap_orphaned_vms(
            get_proxmox_connection(), monotonic_now=1.0)

    assert [r["vmid"] for r in results] == [700, 701]
    for vmid in range(702, 705):
        boundary.assert_untouched(vmid)


# ---------------------------------------------------------------------------
# Cloud resources: the provider-generic sweep over list_managed()
# ---------------------------------------------------------------------------

NOW_EPOCH = 1_900_000_000


def reap_orphaned_cloud_resources(**kwargs):
    return orphan_reaper.reap_orphaned_cloud_resources(**kwargs)


class _FakeMonotonic:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _CloudSweepBackend:
    """A stateful cloud provider whose inventory is exactly `resources`."""

    def __init__(self, name):
        self.name = name
        self.resources = []
        self.deleted = []
        self.refuse = set()
        self.errors = {}  # provider_id -> the exception its delete raises
        self.on_list = None
        self.clock = _FakeMonotonic()
        self.advance_clock_per_delete = 0
        self.fail_list = None

    def list_managed(self):
        if self.fail_list is not None:
            raise self.fail_list
        snapshot = list(self.resources)
        if self.on_list is not None:
            self.on_list()
        return snapshot

    def delete_resource(self, resource):
        self.clock.now += self.advance_clock_per_delete
        if resource.provider_id in self.errors:
            raise self.errors[resource.provider_id]
        if resource.provider_id in self.refuse:
            raise BackendError(f"{resource.provider_id} no longer carries this site's tag")
        assert resource.backend == self.name
        self.resources = [r for r in self.resources if r is not resource]
        self.deleted.append(resource.provider_id)

    def reset_with_old_orphans(self, n, advance_clock_per_delete=0):
        self.resources = [managed_slot(f"slot-old-{index}", 100 + index, NOW_EPOCH - 7200)
                          for index in range(n)]
        self.deleted = []
        self.advance_clock_per_delete = advance_clock_per_delete


def managed_instance(backend, provider_id, vmid, expires, deployment_id="9" * 32, slot=None):
    return ManagedResource(backend=backend, kind="instance", provider_id=provider_id,
                           name=provider_id, vmid=vmid, deployment_id=deployment_id,
                           network_slot=slot, expires_at_epoch=expires)


def managed_network(provider_id, deployment_key, created):
    return ManagedResource(backend="aws", kind="deployment-network",
                           provider_id=provider_id, name=provider_id,
                           deployment_id=deployment_key, created_at_epoch=created)


def managed_slot(provider_id, slot, created):
    return ManagedResource(backend="aws", kind="slot-network", provider_id=provider_id,
                           name=provider_id, network_slot=slot, created_at_epoch=created)


def add_live_record(backend, vmid=900, deployment_id=None, network_slot=None):
    db.session.add(VMRecord(
        vmid=vmid, vm_name=f"student-alice-{vmid}", vm_type=backend, node="zone-a",
        template_name="homework/web", student_name="alice",
        expires_at=datetime.now() + timedelta(hours=1), backend=backend,
        deployment_id=deployment_id, network_slot=network_slot))
    db.session.commit()


def add_live_vmid_reservation(vmid, expires_at, reservation_id=None):
    db.session.add(VMIDReservation(
        vmid=vmid, reservation_id=reservation_id or f"r-{vmid}", expires_at=expires_at))
    db.session.commit()


def add_node_reservation(node, reservation_id="r-sweep"):
    db.session.add(NodeMemoryReservation(
        reservation_id=reservation_id, node=node, memory_bytes=1,
        expires_at=coordination.lease_clock() + 600))
    db.session.commit()


def deleted_ids(backend):
    return set(backend.deleted)


def report(backend, kind, provider_id, status):
    return {"backend": backend, "kind": kind, "provider_id": provider_id,
            "name": provider_id, "status": status}


@pytest.fixture
def backend(app, monkeypatch):
    # The cloud sweep's only provider: registered, enabled, reset after the test.
    sweep = _CloudSweepBackend('aws')
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    register_backend_factory('aws', lambda: sweep)
    with app.app_context():
        yield sweep
    reset_backends_for_tests()


def test_cloud_instance_needs_no_record_no_vmid_lease_and_expiry_past_grace(app, backend):
    # Catches a stale provider expiry deleting a still-tracked or in-flight VM.
    backend.resources = [
        managed_instance("aws", "i-delete", 801, expires=NOW_EPOCH - 3601),
        managed_instance("aws", "i-record", 802, expires=NOW_EPOCH - 3601),
        managed_instance("aws", "i-reserved", 803, expires=NOW_EPOCH - 3601),
        managed_instance("aws", "i-young", 804, expires=NOW_EPOCH - 3599),
        managed_instance("aws", "i-untimed", 805, expires=None),
    ]
    add_live_record(backend="aws", vmid=802)
    add_live_vmid_reservation(vmid=803, expires_at=NOW_EPOCH + 60)
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
    assert [(r["provider_id"], r["status"]) for r in reports] == [("i-delete", "reaped")]
    assert {r.provider_id for r in backend.resources} == {"i-record", "i-reserved", "i-young", "i-untimed"}


def test_cloud_instance_from_another_backend_template_or_without_vmid_is_kept(app, backend):
    # Catches a listing row that is not provably this backend's own machine being deleted.
    import dataclasses
    backend.resources = [
        managed_instance("gcp", "i-foreign-backend", 830, expires=NOW_EPOCH - 7200),
        dataclasses.replace(managed_instance("aws", "i-template", 831,
                                             expires=NOW_EPOCH - 7200), is_template=True),
        managed_instance("aws", "i-no-vmid", None, expires=NOW_EPOCH - 7200),
    ]
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.deleted == []


def test_deployment_network_requires_no_record_reservation_or_instance_and_old_age(app, backend):
    # Catches removal of a firewall/SG while its deployment is live or creating,
    # including the 16-character key vs 32-character id mismatch (C1).
    old, young = NOW_EPOCH - 3601, NOW_EPOCH - 3599
    backend.resources = [
        managed_network("net-orphan", "a" * 16, old),
        managed_network("net-record", "b" * 16, old),
        managed_network("net-reservation", "c" * 16, old),
        managed_network("net-instance", "e" * 16, old),
        managed_network("net-young", "f" * 16, young),
        managed_network("net-untimed", "0" * 16, None),
        managed_instance("aws", "i-for-net", 811, expires=NOW_EPOCH + 3600,
                         deployment_id="e" * 32),
    ]
    add_live_record(backend="aws", vmid=810, deployment_id="b" * 32)
    add_live_vmid_reservation(vmid=812, expires_at=NOW_EPOCH + 60,
                              reservation_id="c" * 32)
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
    assert deleted_ids(backend) == {"net-orphan"}
    assert reports == [report("aws", "deployment-network", "net-orphan", "reaped")]


def test_slot_network_requires_no_same_backend_record_or_instance_and_old_age(app, backend):
    # Catches deleting a shared slot subnet still used by a VM.
    old, young = NOW_EPOCH - 3601, NOW_EPOCH - 3599
    backend.resources = [
        managed_slot("slot-orphan", 11, old),
        managed_slot("slot-other-backend-record", 12, old),
        managed_slot("slot-record", 13, old),
        managed_slot("slot-instance", 14, old),
        managed_slot("slot-young", 15, young),
        managed_slot("slot-untimed", 16, None),
        managed_instance("aws", "i-in-slot", 820, expires=NOW_EPOCH + 3600, slot=14),
    ]
    add_live_record(backend="gcp", vmid=821, network_slot=12)
    add_live_record(backend="aws", vmid=822, network_slot=13)
    reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
    assert deleted_ids(backend) == {"slot-orphan", "slot-other-backend-record"}


def test_listed_instance_with_unknown_network_identity_keeps_that_network_kind(app, backend):
    # Catches a malformed instance (no slot or deployment tag) being read as
    # "uses no network", which would delete the network under a running machine.
    old = NOW_EPOCH - 7200
    backend.resources = [
        managed_network("net-maybe-used", "a" * 16, old),
        managed_slot("slot-maybe-used", 21, old),
        managed_instance("aws", "i-untagged", 840, expires=NOW_EPOCH + 3600,
                         deployment_id=None, slot=None),
    ]
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.deleted == []


def test_cloud_instance_delete_rechecks_under_vmid_lock(app, backend):
    # Catches deleting an instance whose VMRecord committed after the sweep's first query (I14).
    orphan = managed_instance("aws", "i-late", 806, expires=NOW_EPOCH - 3601)
    backend.resources = [orphan]
    backend.on_list = lambda: add_live_record(backend="aws", vmid=806)
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.resources == [orphan]


def test_cloud_instance_delete_rechecks_vmid_reservation_under_vmid_lock(app, backend):
    # Catches deleting an instance whose VMID a create reserved after the sweep's first query.
    orphan = managed_instance("aws", "i-late-lease", 807, expires=NOW_EPOCH - 3601)
    backend.resources = [orphan]
    backend.on_list = lambda: add_live_vmid_reservation(
        vmid=807, expires_at=coordination.lease_clock() + 600)
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.resources == [orphan]


def test_cloud_instance_recheck_waits_for_the_vmid_lifecycle_lock(app, backend):
    # Catches the final recheck running outside the lock a destroy/create path holds.
    import threading
    from app.proxmox_service import vm_lifecycle_lock
    orphan = managed_instance("aws", "i-locked", 808, expires=NOW_EPOCH - 3601)
    backend.resources = [orphan]
    holding, release = threading.Event(), threading.Event()

    def create_path():
        with app.app_context(), vm_lifecycle_lock(808):
            holding.set()
            release.wait(timeout=5)
            add_live_record(backend="aws", vmid=808)

    creator = threading.Thread(target=create_path)
    creator.start()
    assert holding.wait(timeout=5)
    backend.on_list = release.set
    try:
        assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    finally:
        release.set()
        creator.join(timeout=10)
    assert backend.resources == [orphan]


def test_cloud_slot_delete_rechecks_reservation_under_slot_lock(app, backend):
    # Catches the reaper deleting a slot subnet an admission reserved after the sweep listed it (New 6).
    slot = managed_slot("slot-late", 18, NOW_EPOCH - 7200)
    backend.resources = [slot]
    backend.on_list = lambda: add_node_reservation(node="aws")
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.resources == [slot]


def test_cloud_slot_delete_rechecks_record_under_slot_lock(app, backend):
    # Catches the reaper deleting a slot subnet a VM committed into after the sweep listed it.
    slot = managed_slot("slot-late-record", 19, NOW_EPOCH - 7200)
    backend.resources = [slot]
    backend.on_list = lambda: add_live_record(backend="aws", vmid=823, network_slot=19)
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.resources == [slot]


def test_cloud_slot_delete_holds_the_slot_lock(app, backend, monkeypatch):
    # Catches deleting a slot subnet outside the lock ensure_slot and release_slot_if_unused take.
    from contextlib import contextmanager
    from app.backends.cloud_common import slot_lock_key
    held = []
    real_lock = orphan_reaper.vm_lifecycle_lock

    @contextmanager
    def observed_lock(key):
        with real_lock(key):
            held.append(key)
            try:
                yield
            finally:
                held.remove(key)

    monkeypatch.setattr(orphan_reaper, "vm_lifecycle_lock", observed_lock)
    slot = managed_slot("slot-locked", 20, NOW_EPOCH - 7200)
    backend.resources = [slot]
    seen = []
    real_delete = backend.delete_resource
    backend.delete_resource = lambda resource: (seen.append(list(held)),
                                                real_delete(resource))
    reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60)
    assert seen == [[slot_lock_key("aws", 20)]]
    assert backend.deleted == ["slot-locked"]


def test_cloud_slot_is_not_eligible_while_backend_has_unexpired_reservation(app, backend):
    # Catches the initial eligibility pass ignoring an in-flight admission,
    # or comparing the lease with the sweep's now_epoch instead of lease_clock().
    add_node_reservation(node="aws")
    backend.reset_with_old_orphans(1)
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, grace_minutes=60) == []
    assert backend.deleted == []


def test_cloud_slot_ignores_another_backends_or_expired_reservation(app, backend):
    # Catches a GCP admission, or a lease that already lapsed, pinning AWS slots forever.
    add_node_reservation(node="gcp")
    db.session.add(NodeMemoryReservation(
        reservation_id="r-lapsed", node="aws", memory_bytes=1,
        expires_at=coordination.lease_clock() - 1))
    db.session.commit()
    backend.reset_with_old_orphans(1)
    assert [r["status"] for r in reap_orphaned_cloud_resources(
        now_epoch=NOW_EPOCH, grace_minutes=60)] == ["reaped"]


def test_cloud_dry_run_reports_but_mutates_nothing(app, backend):
    # Catches dry-run mode deleting provider resources.
    backend.reset_with_old_orphans(3)
    before = list(backend.resources)
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, dry_run=True)
    assert [r["status"] for r in reports] == ["dry_run"] * 3
    assert backend.resources == before


def test_cloud_item_and_time_budgets_stop_starting_new_deletes(app, backend):
    # Catches a large backlog of leaks starving the next expiry pass.
    backend.reset_with_old_orphans(5)
    assert len(reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH, max_candidates=2)) == 2
    assert len(backend.deleted) == 2
    backend.reset_with_old_orphans(5, advance_clock_per_delete=61)
    assert len(reap_orphaned_cloud_resources(
        now_epoch=NOW_EPOCH, max_seconds=120, clock=backend.clock)) == 2
    assert len(backend.deleted) == 2


def test_cloud_delete_refusal_is_reported_and_resource_remains(app, backend):
    # Catches an ownership refusal being reported as reaped.
    refused = managed_slot("slot-refused", 17, NOW_EPOCH - 7200)
    backend.resources = [refused]
    backend.refuse = {"slot-refused"}
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH)
    assert reports == [report("aws", "slot-network", "slot-refused", "delete_failed")]
    assert backend.resources == [refused]


def test_cloud_inventory_failure_aborts_that_backend_without_deletes(app, backend):
    # Catches a failed listing being read as "nothing left", or a later backend never swept.
    backend.reset_with_old_orphans(2)
    backend.fail_list = BackendError("EC2 inventory unavailable")
    assert reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH) == []
    assert backend.deleted == []


@pytest.mark.parametrize("error", [
    DestroyRefused("slot-refused no longer carries this site's tag"),
    BackendError("DescribeSubnets throttled"),
], ids=["refused", "provider-error"])
def test_cloud_delete_failure_does_not_stop_the_rest_of_the_sweep(app, backend, error):
    # Catches one refused or failing delete ending the pass, leaving every
    # later eligible leftover billing until it, too, sorts first.
    refused = managed_slot("slot-refused", 17, NOW_EPOCH - 7200)
    eligible = managed_slot("slot-next", 18, NOW_EPOCH - 7200)
    backend.resources = [refused, eligible]
    backend.errors = {"slot-refused": error}
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH)
    assert reports == [report("aws", "slot-network", "slot-refused", "delete_failed"),
                       report("aws", "slot-network", "slot-next", "reaped")]
    assert backend.deleted == ["slot-next"]
    assert backend.resources == [refused]


@pytest.mark.parametrize("failure", ["inventory", "construction"])
def test_a_failing_first_backend_does_not_stop_the_second(app, backend, monkeypatch, failure):
    # Catches an AWS outage (or an unbuildable client) ending the cloud pass
    # before GCP's leftovers are swept.
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws', 'gcp'))
    healthy = _CloudSweepBackend('gcp')
    healthy.resources = [replace(managed_slot("gcp-slot-old", 21, NOW_EPOCH - 7200),
                                 backend='gcp')]
    register_backend_factory('gcp', lambda: healthy)
    backend.reset_with_old_orphans(1)
    if failure == "inventory":
        backend.fail_list = BackendError("EC2 inventory unavailable")
    else:
        def unbuildable():
            raise BackendError("no AWS credentials")
        register_backend_factory('aws', unbuildable)
    reports = reap_orphaned_cloud_resources(now_epoch=NOW_EPOCH)
    assert reports == [report("gcp", "slot-network", "gcp-slot-old", "reaped")]
    assert healthy.deleted == ["gcp-slot-old"]
    assert backend.deleted == []


def test_combined_sweep_without_proxmox_runs_only_the_cloud_pass(app, backend):
    # Catches a cloud-only portal (no Proxmox client) never reaping cloud leaks (C6).
    backend.reset_with_old_orphans(1)
    reports = orphan_reaper.reap_orphaned_vms(
        None, now=datetime.fromtimestamp(NOW_EPOCH))
    assert reports == [report("aws", "slot-network", "slot-old-0", "reaped")]


def test_combined_sweep_shares_the_item_budget_with_proxmox(app, backend):
    # Catches each pass getting its own budget, doubling the work an expiry pass waits on.
    boundary = _many_orphans(2)
    backend.reset_with_old_orphans(5)
    with boundary.connection():
        results = orphan_reaper.reap_orphaned_vms(
            get_proxmox_connection(), now=datetime.fromtimestamp(NOW_EPOCH),
            max_candidates=3)
    assert [r.get("vmid") for r in results[:2]] == [700, 701]
    assert results[2:] == [report("aws", "slot-network", "slot-old-0", "reaped")]
    assert backend.deleted == ["slot-old-0"]


def test_combined_sweep_still_runs_cloud_pass_when_proxmox_inventory_fails(app, backend):
    # Catches a Proxmox outage mid-sweep starving the cloud pass; Proxmox guests stay untouched.
    boundary = _many_orphans(1)
    boundary.api.cluster.resources.get.side_effect = RuntimeError("cluster unreachable")
    backend.reset_with_old_orphans(1)
    with boundary.connection():
        results = orphan_reaper.reap_orphaned_vms(
            get_proxmox_connection(), now=datetime.fromtimestamp(NOW_EPOCH))
    assert results == [report("aws", "slot-network", "slot-old-0", "reaped")]
    boundary.assert_untouched(700)


# -- the same sweep against the real AWS backend (moto) ---------------------

def _aws_tags(pairs):
    return [{'Key': key, 'Value': value} for key, value in pairs.items()]


@pytest.fixture
def aws_sweep(app, monkeypatch):
    for name, value in AWS_TEST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    for name, value in AWS_TEST_CONFIG.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    with mock_aws(), app.app_context():
        ec2 = boto3.client('ec2', region_name='us-east-1')
        vpc_id = ec2.create_vpc(CidrBlock='172.20.0.0/16')['Vpc']['VpcId']
        monkeypatch.setattr(config, 'AWS_LAB_VPC_ID', vpc_id)
        monkeypatch.setattr(config, 'AWS_LAB_CIDR', '172.20.0.0/16')
        aws = AWSBackend(ec2_client=ec2)
        register_backend_factory('aws', lambda: aws)
        yield ec2, vpc_id, register_aws_test_image(ec2)
    reset_backends_for_tests()


def _aws_slot(ec2, vpc_id, slot, site='test-site', created=None):
    return ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=cloud_slot_cidr('172.20.0.0/16', slot),
        AvailabilityZone='us-east-1a',
        TagSpecifications=[{'ResourceType': 'subnet', 'Tags': _aws_tags({
            'Name': slot_network_name(site, slot), TAG_SITE: site,
            TAG_SLOT: str(slot), TAG_KIND: 'slot-network',
            TAG_CREATED: str(int(created))})}],
    )['Subnet']['SubnetId']


def _aws_group(ec2, vpc_id, deployment_id, site='test-site', created=None):
    return ec2.create_security_group(
        GroupName=deployment_network_name(site, deployment_id),
        Description='test', VpcId=vpc_id,
        TagSpecifications=[{'ResourceType': 'security-group', 'Tags': _aws_tags({
            TAG_SITE: site, TAG_DEPLOYMENT: deployment_id,
            TAG_KIND: 'deployment-network', TAG_CREATED: str(int(created))})}],
    )['GroupId']


def _aws_instance(ec2, image_id, vmid, site='test-site', expires=None, subnet_id=None):
    # The orphan sits in its tagged slot's real subnet: the delete-time
    # recheck proves placement, not just tags.
    placement = {'SubnetId': subnet_id} if subnet_id else {}
    return ec2.run_instances(
        ImageId=image_id, InstanceType='t3.micro', MinCount=1, MaxCount=1,
        TagSpecifications=[{'ResourceType': 'instance', 'Tags': _aws_tags({
            'Name': f'student-alice-{vmid}', TAG_SITE: site, TAG_KIND: 'instance',
            TAG_VMID: str(vmid), TAG_DEPLOYMENT: '9' * 32, TAG_SLOT: '7',
            TAG_EXPIRES: str(int(expires))})}],
        **placement,
    )['Instances'][0]['InstanceId']


def _instance_state(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])[
        'Reservations'][0]['Instances'][0]['State']['Name']


def test_aws_sweep_reaps_only_this_sites_unowned_leftovers(aws_sweep):
    # Catches the sweep deleting another portal's resources, or a recorded slot,
    # through the real AWS inventory and its delete-time ownership re-check.
    ec2, vpc_id, image_id = aws_sweep
    old = time.time() - 7200
    orphan_slot = _aws_slot(ec2, vpc_id, 3, created=old)
    foreign_slot = _aws_slot(ec2, vpc_id, 4, site='other-site', created=old)
    recorded_slot = _aws_slot(ec2, vpc_id, 5, created=old)
    orphan_group = _aws_group(ec2, vpc_id, 'a' * 32, created=old)
    foreign_group = _aws_group(ec2, vpc_id, 'a' * 32, site='other-site', created=old)
    # Slot 7 is in use by the orphan instance, so this sweep keeps it.
    instance_slot = _aws_slot(ec2, vpc_id, 7, created=old)
    orphan_instance = _aws_instance(ec2, image_id, 901, expires=old,
                                    subnet_id=instance_slot)
    foreign_instance = _aws_instance(ec2, image_id, 902, site='other-site', expires=old)
    add_live_record(backend='aws', vmid=903, network_slot=5)

    reports = reap_orphaned_cloud_resources(grace_minutes=60)

    assert sorted((r['kind'], r['provider_id'], r['status']) for r in reports) == sorted([
        ('instance', orphan_instance, 'reaped'),
        ('deployment-network', orphan_group, 'reaped'),
        ('slot-network', orphan_slot, 'reaped'),
    ])
    assert _instance_state(ec2, orphan_instance) in ('shutting-down', 'terminated')
    assert _instance_state(ec2, foreign_instance) == 'running'
    remaining_subnets = {s['SubnetId'] for s in ec2.describe_subnets()['Subnets']}
    assert {foreign_slot, recorded_slot, instance_slot} <= remaining_subnets
    assert orphan_slot not in remaining_subnets
    remaining_groups = {g['GroupId'] for g in ec2.describe_security_groups()['SecurityGroups']}
    assert foreign_group in remaining_groups and orphan_group not in remaining_groups


# -- the same sweep against the real GCP backend (stateful fake) ------------

@pytest.fixture
def gcp_sweep(app, monkeypatch):
    for name, value in GCP_TEST_CONFIG.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('gcp',))
    gce = FakeGCESession(GCP_TEST_CONFIG['GCP_PROJECT'])
    gcp = GCPBackend(session=gce)
    register_backend_factory('gcp', lambda: gcp)
    with app.app_context():
        yield gce
    reset_backends_for_tests()


def _gcp_slot(gce, slot, site='test-site'):
    name = slot_network_name(site, slot)
    gce.add_subnetwork(GCP_TEST_CONFIG['GCP_REGION'], name,
                       cloud_slot_cidr(GCP_TEST_CONFIG['GCP_LAB_CIDR'], slot))
    return name


def _gcp_firewall(gce, deployment_id, site='test-site'):
    name = deployment_network_name(site, deployment_id)
    gce.add_firewall({
        'name': name, 'network': 'projects/example-project/global/networks/lab-vpc',
        'direction': 'INGRESS', 'priority': 1000, 'sourceTags': [name],
        'targetTags': [name], 'allowed': [{'IPProtocol': 'all'}]})
    return name


def _gcp_instance(gce, vmid, slot_name, site='test-site', expires=None):
    name = f'lp-{site}-{vmid}'
    gce.add_instance(
        GCP_TEST_CONFIG['GCP_ZONE'], name, description=f'student-alice-{vmid}',
        subnetwork=(GCP_TEST_CONFIG['GCP_REGION'], slot_name),
        labels={TAG_SITE: site, TAG_KIND: 'instance', TAG_VMID: str(vmid),
                TAG_DEPLOYMENT: '9' * 32, TAG_SLOT: '7',
                TAG_EXPIRES: str(int(expires))})
    return name


def test_gcp_sweep_reaps_only_this_sites_unowned_leftovers(gcp_sweep):
    # Catches the GCP inventory fields and the sweep disagreeing (a network
    # never reaped, or a recorded/foreign one deleted), end to end through
    # GCPBackend's listing and its delete-time identity re-check.
    gce = gcp_sweep
    old = time.time() - 7200  # the fake stamps every creation in early 2026
    orphan_slot = _gcp_slot(gce, 3)
    foreign_slot = _gcp_slot(gce, 4, site='other-site')
    recorded_slot = _gcp_slot(gce, 5)
    # Slot 7 is in use by the orphan instance, so this sweep keeps it.
    instance_slot = _gcp_slot(gce, 7)
    orphan_firewall = _gcp_firewall(gce, 'a' * 32)
    foreign_firewall = _gcp_firewall(gce, 'a' * 32, site='other-site')
    orphan_instance = _gcp_instance(gce, 901, instance_slot, expires=old)
    foreign_instance = _gcp_instance(gce, 902, instance_slot, site='other-site',
                                     expires=old)
    add_live_record(backend='gcp', vmid=903, network_slot=5)

    reports = reap_orphaned_cloud_resources(grace_minutes=60)

    assert sorted((r['kind'], r['name'], r['status']) for r in reports) == sorted([
        ('instance', 'student-alice-901', 'reaped'),
        ('deployment-network', orphan_firewall, 'reaped'),
        ('slot-network', orphan_slot, 'reaped'),
    ])
    zone = GCP_TEST_CONFIG['GCP_ZONE']
    assert (zone, orphan_instance) not in gce.instances
    assert (zone, foreign_instance) in gce.instances
    remaining_slots = {name for _region, name in gce.subnetworks}
    assert {foreign_slot, recorded_slot, instance_slot} <= remaining_slots
    assert orphan_slot not in remaining_slots
    assert foreign_firewall in gce.firewalls and orphan_firewall not in gce.firewalls
