"""Regression tests for Proxmox guests orphaned by an interrupted deploy."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models import AdminAlert, KothBox, KothGame, VMRecord, db
from app.proxmox_service import (
    _clear_vmid_reservations,
    get_next_available_vmid,
    get_proxmox_connection,
    release_vmid_reservation,
)

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


def test_active_vmid_reservation_protects_expired_orphan_in_dry_run(app):
    boundary = ProxmoxBoundary([_resource()])
    allocator_view = MagicMock()
    allocator_view.cluster.resources.get.return_value = [{'vmid': 699}]
    _clear_vmid_reservations()
    try:
        assert get_next_available_vmid(
            allocator_view,
            reservation_id='active-create',
        ) == 700
        with app.app_context():
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


def test_only_status_code_404_is_classified_as_not_found(app):
    class ProxmoxError(Exception):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    boundary = ProxmoxBoundary(
        [_resource()],
        operation_errors={(700, "stop"): ProxmoxError("gone", status_code=404)},
    )
    with app.app_context():
        result = _run(boundary)
    assert result[0]["status"] == "not_found"
    boundary.guests[700].delete.assert_not_called()

    transport = ProxmoxBoundary(
        [_resource(701, "student-bob-web")],
        operation_errors={(701, "stop"): ProxmoxError("request /qemu/404 failed")},
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
