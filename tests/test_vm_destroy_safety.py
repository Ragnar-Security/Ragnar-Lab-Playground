"""Regression tests for VM destruction safety.

Proxmox itself is mocked only at the ``ProxmoxAPI`` boundary.  These tests
assert on the caller-visible terminal state: a live VM remains tracked, while
cleanup continues past a timed-out sibling.
"""

from contextlib import contextmanager
import threading
from unittest.mock import MagicMock, patch
import app.tasks as vm_tasks
from datetime import datetime, timedelta


from proxmoxer.core import ResourceException

from app.routes.vm_creation import cleanup_vms
from app.models import KothBox, KothGame, User, VMRecord, db
from app import proxmox_service
from app.proxmox_service import reset_cluster_vm_index



def _proxmox_with_qemu_endpoints(mock_api, vmids, *, names=None):
    proxmox = MagicMock()
    mock_api.return_value = proxmox
    endpoints = {vmid: MagicMock() for vmid in vmids}
    names = names or {}
    for vmid, endpoint in endpoints.items():
        endpoint.status.stop.post.return_value = f"stop-{vmid}"
    proxmox.nodes.return_value.qemu.side_effect = lambda vmid: endpoints[vmid]
    proxmox.cluster.resources.get.return_value = [
        {"vmid": vmid, "name": names.get(vmid, f"vm-{vmid}"),
         "node": "pve", "type": "qemu", "status": "running"}
        for vmid in vmids
    ]
    reset_cluster_vm_index()
    return proxmox, endpoints


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_creation.wait_for_proxmox_task")
def test_cleanup_does_not_treat_vmid_containing_404_as_missing(mock_wait, mock_api):
    """A live VM 10404 that rejects destroy must not be reported destroyed."""
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [10404])
    endpoints[10404].delete.side_effect = ResourceException(
        500, "Internal Server Error", "VM 10404 is running - destroy failed"
    )
    mock_wait.return_value = {"success": True, "status": "stopped"}

    destroyed = cleanup_vms([
        {"vmid": 10404, "node": "pve", "vm_type": "qemu",
         "vm_name": "vm-10404"},
    ])

    assert destroyed == set(), "live VM 10404 must remain tracked after destroy refusal"


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_creation.wait_for_proxmox_task")
def test_cleanup_continues_after_delete_task_timeout(mock_wait, mock_api):
    """One timed-out delete task must not prevent later VMs being destroyed."""
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100, 101])
    endpoints[100].delete.return_value = "delete-100"
    endpoints[101].delete.return_value = "delete-101"

    def wait_result(_proxmox, _node, upid, **_kwargs):
        if upid == "delete-100":
            raise TimeoutError("delete-100 timed out")
        return {"success": True, "status": "stopped"}

    mock_wait.side_effect = wait_result

    try:
        destroyed = cleanup_vms([
            {"vmid": 100, "node": "pve", "vm_type": "qemu",
             "vm_name": "vm-100"},
            {"vmid": 101, "node": "pve", "vm_type": "qemu",
             "vm_name": "vm-101"},
        ])
    except TimeoutError as exc:
        raise AssertionError("cleanup must continue after a per-VM timeout") from exc

    assert destroyed == {101}


def _student_with_vm(app, *, vmid=100):
    with app.app_context():
        student = User(login_id="student", full_name="Student")
        student.set_password("studentpass")
        db.session.add(student)
        db.session.flush()
        vm = VMRecord(
            vmid=vmid,
            vm_name=f"student-student-vm-{vmid}",
            vm_type="qemu",
            node="pve",
            template_name="homework/vm",
            student_name="student",
            created_by_user_id=student.id,
            expires_at=datetime.now() + timedelta(hours=1),
        )
        db.session.add(vm)
        db.session.commit()
        return vm.id


def _login_student(client):
    response = client.post(
        "/auth/login",
        json={"login_id": "student", "password": "studentpass"},
    )
    assert response.status_code == 200


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_lifecycle.wait_for_proxmox_task")
def test_destroy_keeps_record_when_async_delete_task_fails(
        mock_wait, mock_api, app, client):
    """An accepted delete UPID is not success until exitstatus is OK."""
    vm_id = _student_with_vm(app)
    _login_student(client)
    _, endpoints = _proxmox_with_qemu_endpoints(
        mock_api, [100], names={100: "student-student-vm-100"})
    endpoints[100].config.get.return_value = {}
    endpoints[100].delete.return_value = "delete-100"

    def wait_result(_proxmox, _node, upid, **_kwargs):
        if upid == "delete-100":
            return {"success": False, "status": "stopped", "error": "locked"}
        return {"success": True, "status": "stopped"}

    mock_wait.side_effect = wait_result

    response = client.post("/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is False
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None, (
            "failed asynchronous destroy must leave the VM tracked"
        )


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_lifecycle.wait_for_proxmox_task")
def test_destroy_keeps_live_vmid_10404_after_submit_failure(
        mock_wait, mock_api, app, client):
    """Digits in a live VMID must not masquerade as an HTTP 404."""
    vm_id = _student_with_vm(app, vmid=10404)
    _login_student(client)
    _, endpoints = _proxmox_with_qemu_endpoints(
        mock_api, [10404], names={10404: "student-student-vm-10404"})
    endpoints[10404].config.get.return_value = {}
    endpoints[10404].delete.side_effect = ResourceException(
        500, "Internal Server Error", "VM 10404 is running - destroy failed"
    )
    mock_wait.return_value = {"success": True, "status": "stopped"}

    response = client.post("/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is False
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None, (
            "live VM 10404 must remain tracked after destroy refusal"
        )


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_keeps_config_error_record_when_force_delete_fails(
        mock_api, app, client):
    """A failed force-delete of a listed VM must preserve its record."""
    vm_id = _student_with_vm(app)
    _login_student(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].config.get.side_effect = ResourceException(
        500, "Internal Server Error", "configuration file does not exist"
    )
    endpoints[100].delete.side_effect = ResourceException(
        500, "Internal Server Error", "permission denied"
    )
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "student-student-vm-100",
         "node": "pve", "type": "qemu", "status": "unknown"},
    ]
    reset_cluster_vm_index()

    response = client.post("/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is False
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None, (
            "failed force-delete must leave the VM tracked"
        )


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_keeps_listed_config_error_on_not_found_force_response(
        mock_api, app, client):
    """A listed VM stays tracked even if force-delete says it does not exist."""
    vm_id = _student_with_vm(app)
    _login_student(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].config.get.side_effect = ResourceException(
        500, "Internal Server Error", "configuration file does not exist"
    )
    endpoints[100].delete.side_effect = ResourceException(
        500, "Internal Server Error", "VM 100 does not exist"
    )
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "student-student-vm-100",
         "node": "pve", "type": "qemu", "status": "unknown"},
    ]
    reset_cluster_vm_index()

    response = client.post("/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is False
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None, (
            "cluster-listed VM must survive ambiguous force-delete response"
        )


def _admin_with_vm(app, *, vmid=100, koth=False):
    with app.app_context():
        admin = User(login_id="admin", full_name="Admin", is_admin=True)
        admin.set_password("adminpass")
        db.session.add(admin)
        db.session.flush()
        game = None
        if koth:
            game = KothGame(
                name="Game",
                deployment_group_name="koth-lab",
                entry_vlan_tag=300,
                internal_vlan_tag=400,
            )
            db.session.add(game)
            db.session.flush()
        vm = VMRecord(
            vmid=vmid,
            vm_name=f"vm-{vmid}",
            vm_type="qemu",
            node="pve",
            template_name="homework/vm",
            student_name="student",
            created_by_user_id=admin.id,
            koth_game_id=game.id if game else None,
            expires_at=datetime.now() + timedelta(hours=1),
        )
        db.session.add(vm)
        db.session.flush()
        box = None
        if game:
            box = KothBox(game_id=game.id, vm_record_id=vm.id, name="box")
            db.session.add(box)
        db.session.commit()
        return vm.id, box.id if box else None


def _login_admin(client):
    response = client.post(
        "/auth/login",
        json={"login_id": "admin", "password": "adminpass"},
    )
    assert response.status_code == 200


def _set_task_results(proxmox, results):
    def task_for(upid):
        task = MagicMock()
        task.status.get.return_value = results.get(
            upid, {"status": "stopped", "exitstatus": "OK"})
        return task

    proxmox.nodes.return_value.tasks.side_effect = task_for


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_marks_koth_record_destroyed_instead_of_deleting(
        mock_api, app, client):
    """The regular destroy route must preserve KothBox VM history."""
    vm_id, box_id = _admin_with_vm(app, koth=True)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].config.get.return_value = {}
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
        "delete-100": {"status": "stopped", "exitstatus": "OK"},
    })

    response = client.post(
        "/destroy",
        data={"deployment_group": "homework", "student_name": "student"},
    )

    assert response.get_json()["success"] is True
    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        box = db.session.get(KothBox, box_id)
        assert vm is not None and vm.destroyed is True
        assert box.vm_record_id == vm_id


@patch("app.routes.admin.time.sleep", return_value=None)
@patch("app.proxmox_service.ProxmoxAPI")
def test_bulk_destroy_keeps_record_when_delete_task_fails(
        mock_api, _mock_sleep, app, client):
    """Bulk destroy must await the delete task before dropping a record."""
    vm_id, _ = _admin_with_vm(app)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
        "delete-100": {
            "status": "stopped", "exitstatus": "ERROR", "errors": "locked",
        },
    })

    response = client.post("/admin/bulk-destroy", json={"vm_ids": [vm_id]})

    assert response.get_json()["destroyed"] == 0
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None, (
            "failed asynchronous bulk destroy must leave the VM tracked"
        )


@patch("app.routes.admin.time.sleep", return_value=None)
@patch("app.proxmox_service.ProxmoxAPI")
def test_bulk_destroy_keeps_live_vmid_10404(mock_api, _mock_sleep, app, client):
    """A VMID containing 404 is not evidence that the live VM is gone."""
    vm_id, _ = _admin_with_vm(app, vmid=10404)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [10404])
    endpoints[10404].delete.side_effect = ResourceException(
        500, "Internal Server Error", "VM 10404 is running - destroy failed"
    )
    _set_task_results(proxmox, {
        "stop-10404": {"status": "stopped", "exitstatus": "OK"},
    })

    response = client.post("/admin/bulk-destroy", json={"vm_ids": [vm_id]})

    assert response.get_json()["destroyed"] == 0
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None


@patch("app.routes.admin.time.sleep", return_value=None)
@patch("app.proxmox_service.ProxmoxAPI")
def test_bulk_destroy_marks_koth_record_destroyed_instead_of_deleting(
        mock_api, _mock_sleep, app, client):
    """Successful KOTH cleanup must preserve the KothBox foreign key."""
    vm_id, box_id = _admin_with_vm(app, koth=True)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
        "delete-100": {"status": "stopped", "exitstatus": "OK"},
    })

    response = client.post("/admin/bulk-destroy", json={"vm_ids": [vm_id]})

    assert response.get_json()["destroyed"] == 1
    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        box = db.session.get(KothBox, box_id)
        assert vm is not None and vm.destroyed is True
        assert vm.destroyed_at is not None
        assert box.vm_record_id == vm_id


@patch("app.proxmox_service.ProxmoxAPI")
def test_bulk_destroy_skips_already_destroyed_record(mock_api, app, client):
    """A stale admin request must not delete a new VM that reused the VMID."""
    vm_id, _ = _admin_with_vm(app, koth=True)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.side_effect = AssertionError(
        "already-destroyed record reached Proxmox delete")
    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        vm.destroyed = True
        vm.destroyed_at = datetime.now()
        db.session.commit()

    response = client.post("/admin/bulk-destroy", json={"vm_ids": [vm_id]})

    assert response.get_json()["destroyed"] == 0
    endpoints[100].status.stop.post.assert_not_called()
    endpoints[100].delete.assert_not_called()


class _StopExpiryLoop(BaseException):
    pass


def _run_one_expiry_cycle(app):
    minute_sleeps = 0

    def fake_sleep(seconds):
        nonlocal minute_sleeps
        if seconds == 60:
            minute_sleeps += 1
            if minute_sleeps == 2:
                raise _StopExpiryLoop

    with patch("app.tasks.time.sleep", side_effect=fake_sleep):
        try:
            vm_tasks.check_and_destroy_expired_vms(app)
        except _StopExpiryLoop:
            pass


def _expired_vm(app, *, retry_count=0, koth=False):
    vm_id, box_id = _admin_with_vm(app, koth=koth)
    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        vm.expires_at = datetime.now() - timedelta(minutes=10)
        vm.deletion_retry_count = retry_count
        db.session.commit()
    return vm_id, box_id


@patch("app.proxmox_service.ProxmoxAPI")
def test_expiry_delete_waits_for_task_success(mock_api, app):
    """A failed qmdestroy task must not count as a deleted VM."""
    with app.app_context():
        vm = VMRecord(
            vmid=100,
            vm_name="expired",
            vm_type="qemu",
            node="pve",
            template_name="homework/vm",
            student_name="student",
            expires_at=datetime.now() - timedelta(minutes=1),
        )
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "delete-100": {
            "status": "stopped", "exitstatus": "ERROR", "errors": "locked",
        },
    })

    assert vm_tasks._delete_vm(proxmox, vm) is False, (
        "failed asynchronous expiry destroy must remain retryable"
    )


@patch("app.tasks._send_deletion_failure_alert")
@patch("app.proxmox_service.ProxmoxAPI")
def test_expiry_keeps_retrying_and_alerts_only_once(
        mock_api, mock_alert, app):
    """Five failures alert once without dropping the only tracking row."""
    vm_id, _ = _expired_vm(app, retry_count=4)
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].status.stop.post.side_effect = Exception("cluster locked")

    _run_one_expiry_cycle(app)

    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        assert vm is not None, "max retries must never delete a live VMRecord"
        assert vm.deletion_retry_count == 5
        assert vm.deletion_alert_sent is True
        vm.deletion_last_attempt_at = datetime.now() - timedelta(hours=2)
        db.session.commit()

    _run_one_expiry_cycle(app)

    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        assert vm is not None
        assert vm.deletion_retry_count == 6
    assert mock_alert.call_count == 1


@patch("app.proxmox_service.ProxmoxAPI")
def test_expiry_backoff_skips_recent_failed_vm(mock_api, app):
    """A recently failed VM is not hammered again on the next minute tick."""
    vm_id, _ = _expired_vm(app, retry_count=2)
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        vm.deletion_last_attempt_at = datetime.now()
        db.session.commit()

    _run_one_expiry_cycle(app)

    endpoints[100].status.stop.post.assert_not_called()
    with app.app_context():
        assert db.session.get(VMRecord, vm_id).deletion_retry_count == 2


@patch("app.proxmox_service.ProxmoxAPI")
def test_expiry_marks_koth_record_destroyed_instead_of_deleting(mock_api, app):
    """Expiry preserves KothBox history after a confirmed successful delete."""
    vm_id, box_id = _expired_vm(app, koth=True)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "delete-100": {"status": "stopped", "exitstatus": "OK"},
    })

    _run_one_expiry_cycle(app)

    with app.app_context():
        vm = db.session.get(VMRecord, vm_id)
        box = db.session.get(KothBox, box_id)
        assert vm is not None and vm.destroyed is True
        assert vm.destroyed_at is not None
        assert box.vm_record_id == vm_id


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_creation.wait_for_proxmox_task")
def test_cleanup_keeps_cluster_listed_vm_after_ambiguous_missing_errors(
        mock_wait, mock_api):
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    ambiguous = ResourceException(
        500, "Internal Server Error",
        "configuration file nodes/pve/qemu-server/100.conf does not exist")
    endpoints[100].status.stop.post.side_effect = ambiguous
    endpoints[100].delete.side_effect = ambiguous
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "student-vm", "node": "pve", "type": "qemu"},
    ]
    reset_cluster_vm_index()

    destroyed = cleanup_vms([
        {"vmid": 100, "node": "pve", "vm_type": "qemu",
         "vm_name": "student-vm"},
    ])

    assert destroyed == set()
    endpoints[100].delete.assert_called_once()


@patch("app.proxmox_service.ProxmoxAPI")
def test_cleanup_retires_only_when_fresh_inventory_confirms_absence(mock_api):
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].status.stop.post.side_effect = ResourceException(
        404, "Not Found", "VM 100 does not exist")
    proxmox.cluster.resources.get.return_value = []
    reset_cluster_vm_index()

    destroyed = cleanup_vms([
        {"vmid": 100, "node": "pve", "vm_type": "qemu",
         "vm_name": "student-vm"},
    ])

    assert destroyed == {100}
    endpoints[100].delete.assert_not_called()


@patch("app.proxmox_service.ProxmoxAPI")
def test_cleanup_keeps_vm_when_absence_inventory_read_fails(mock_api):
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    ambiguous = ResourceException(404, "Not Found", "VM 100 does not exist")
    endpoints[100].status.stop.post.side_effect = ambiguous
    endpoints[100].delete.side_effect = ambiguous
    proxmox.cluster.resources.get.side_effect = RuntimeError("inventory down")
    reset_cluster_vm_index()

    destroyed = cleanup_vms([
        {"vmid": 100, "node": "pve", "vm_type": "qemu",
         "vm_name": "student-vm"},
    ])

    assert destroyed == set()


@patch("app.proxmox_service.ProxmoxAPI")
def test_expiry_keeps_cluster_listed_vm_after_ambiguous_missing_error(
        mock_api, app):
    vm_id, _ = _expired_vm(app)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.side_effect = ResourceException(
        500, "Internal Server Error",
        "configuration file nodes/pve/qemu-server/100.conf does not exist")
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "vm-100", "node": "pve", "type": "qemu"},
    ]
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
    })
    reset_cluster_vm_index()

    _run_one_expiry_cycle(app)

    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None


@patch("app.routes.admin.time.sleep", return_value=None)
@patch("app.proxmox_service.ProxmoxAPI")
def test_bulk_destroy_keeps_cluster_listed_vm_after_ambiguous_missing_error(
        mock_api, _mock_sleep, app, client):
    vm_id, _ = _admin_with_vm(app)
    _login_admin(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].delete.side_effect = ResourceException(
        500, "Internal Server Error",
        "configuration file nodes/pve/qemu-server/100.conf does not exist")
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "vm-100", "node": "pve", "type": "qemu"},
    ]
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
    })
    reset_cluster_vm_index()

    response = client.post("/admin/bulk-destroy", json={"vm_ids": [vm_id]})

    assert response.get_json()["destroyed"] == 0
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_keeps_record_when_absence_inventory_read_fails(
        mock_api, app, client):
    vm_id = _student_with_vm(app)
    _login_student(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    endpoints[100].config.get.side_effect = ResourceException(
        500, "Internal Server Error", "configuration file does not exist")
    endpoints[100].delete.side_effect = ResourceException(
        404, "Not Found", "VM 100 does not exist")
    proxmox.cluster.resources.get.side_effect = RuntimeError("inventory down")
    reset_cluster_vm_index()

    response = client.post("/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is False
    with app.app_context():
        assert db.session.get(VMRecord, vm_id) is not None


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_waiting_on_lifecycle_lock_never_touches_replacement(
        mock_api, app):
    old_id = _student_with_vm(app)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])
    proxmox.cluster.resources.get.return_value = [
        {"vmid": 100, "name": "replacement", "node": "pve", "type": "qemu"},
    ]
    reset_cluster_vm_index()
    waiting = threading.Event()
    finished = threading.Event()
    response_data = {}
    real_lock = proxmox_service.vm_lifecycle_lock

    @contextmanager
    def observed_lock(vmid):
        waiting.set()
        with real_lock(vmid):
            yield

    def request_destroy():
        with app.test_client() as thread_client:
            _login_student(thread_client)
            with patch("app.routes.vm_lifecycle.vm_lifecycle_lock",
                       side_effect=observed_lock):
                response = thread_client.post(
                    "/destroy", data={"deployment_group": "homework"})
                response_data.update(response.get_json())
        finished.set()

    with real_lock(100):
        request_thread = threading.Thread(target=request_destroy)
        request_thread.start()
        assert waiting.wait(timeout=5), "destroy request never reached lifecycle lock"
        with app.app_context():
            old = db.session.get(VMRecord, old_id)
            db.session.delete(old)
            db.session.commit()
            replacement = VMRecord(
                vmid=100,
                vm_name="replacement",
                vm_type="qemu",
                node="pve",
                template_name="replacement/vm",
                student_name="other",
                expires_at=datetime.now() + timedelta(hours=1),
            )
            db.session.add(replacement)
            db.session.commit()

    assert finished.wait(timeout=5), "destroy request did not finish"
    request_thread.join()
    endpoints[100].status.stop.post.assert_not_called()
    endpoints[100].delete.assert_not_called()
    with app.app_context():
        replacement = VMRecord.query.filter_by(vmid=100).one()
        assert replacement.vm_name == "replacement"


@patch("app.proxmox_service.ProxmoxAPI")
@patch("app.routes.vm_creation.wait_for_proxmox_task",
       return_value={"success": True, "status": "stopped"})
def test_cleanup_refuses_untracked_guest_without_expected_name(
        _mock_wait, mock_api):
    _, endpoints = _proxmox_with_qemu_endpoints(mock_api, [100])

    destroyed = cleanup_vms([
        {"vmid": 100, "node": "pve", "vm_type": "qemu"},
    ])

    assert destroyed == set()
    endpoints[100].status.stop.post.assert_not_called()
    endpoints[100].delete.assert_not_called()


@patch("app.proxmox_service.ProxmoxAPI")
def test_destroy_refreshes_transaction_and_commits_while_lock_is_held(
        mock_api, app, client):
    _student_with_vm(app)
    _login_student(client)
    proxmox, endpoints = _proxmox_with_qemu_endpoints(
        mock_api, [100], names={100: "student-student-vm-100"})
    endpoints[100].config.get.return_value = {}
    endpoints[100].delete.return_value = "delete-100"
    _set_task_results(proxmox, {
        "stop-100": {"status": "stopped", "exitstatus": "OK"},
        "delete-100": {"status": "stopped", "exitstatus": "OK"},
    })
    lock_held = False
    commit_states = []
    real_commit = db.session.commit
    real_lock = proxmox_service.vm_lifecycle_lock

    @contextmanager
    def observed_lock(vmid):
        nonlocal lock_held
        with real_lock(vmid):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    def observed_commit():
        commit_states.append(lock_held)
        return real_commit()

    with patch("app.routes.vm_lifecycle.vm_lifecycle_lock",
               side_effect=observed_lock), patch.object(
                   db.session, "commit", side_effect=observed_commit
               ), patch.object(
                   db.session, "rollback", wraps=db.session.rollback
               ) as rollback:
        response = client.post(
            "/destroy", data={"deployment_group": "homework"})

    assert response.get_json()["success"] is True
    assert rollback.call_count >= 1
    assert True in commit_states
