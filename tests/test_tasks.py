"""
Test cases for background tasks module
Tests VM expiration checking, deletion retry logic, and admin alerts
"""

import pytest
import os
import json
import requests
from datetime import datetime, timedelta
from unittest.mock import Mock, call, patch, MagicMock, mock_open
from app.backends.base import DestroyPolicy, VMRef
from app.backends.proxmox import ProxmoxBackend
from app.tasks import (
    _log_alert_to_file, _create_database_alert,
    VM_DELETION_MAX_RETRIES
)
from app.models import VMRecord, AdminAlert, db


def _listed(vm_record):
    return [{'vmid': vm_record.vmid, 'name': vm_record.vm_name,
             'node': vm_record.node, 'type': vm_record.vm_type,
             'status': 'running'}]


def _strict_destroy(proxmox, vm_record, inventory=None):
    """Expiry's stop-then-delete: ProxmoxBackend.destroy under STRICT.

    `inventory` is the sequence of cluster reads; by default the guest is
    listed, as the identity check before any mutation requires. Returns
    whether the guest is gone, which is what expiry retires on.
    """
    from app.proxmox_service import reset_cluster_vm_index
    if inventory is None:
        proxmox.cluster.resources.get.return_value = _listed(vm_record)
    else:
        proxmox.cluster.resources.get.side_effect = inventory
    reset_cluster_vm_index()
    try:
        ProxmoxBackend(client=proxmox).destroy(
            VMRef.from_record(vm_record), DestroyPolicy.STRICT)
    except Exception:
        return False
    return True


_TASK_OK = {'success': True, 'status': 'stopped'}


class TestVMStopAndDelete:
    """Expiry's stop and delete, through ProxmoxBackend's STRICT policy"""

    @patch('app.proxmox_service.wait_for_proxmox_task', return_value=_TASK_OK)
    def test_stop_vm_success(self, _mock_wait, app):
        """Test successful VM stop"""
        with app.app_context():
            # Create test VM record
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            # Mock Proxmox connection
            mock_proxmox = Mock()
            mock_proxmox.nodes().qemu().status.stop.post.return_value = None
            
            result = _strict_destroy(mock_proxmox, vm_record)
            
            assert result is True
            mock_proxmox.nodes().qemu().status.stop.post.assert_called_once()

    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    def test_stop_vm_waits_for_async_task(self, mock_wait, app):
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                expires_at=datetime.now() + timedelta(hours=1),
            )
            mock_proxmox = Mock()
            mock_proxmox.nodes().qemu().status.stop.post.return_value = (
                'UPID:stop')
            mock_proxmox.nodes().qemu().delete.return_value = 'UPID:delete'

            assert _strict_destroy(mock_proxmox, vm_record) is True

            # Exactly one stop wait, then exactly one delete wait.
            assert mock_wait.call_args_list == [
                call(mock_proxmox, 'pve', 'UPID:stop',
                     timeout=60, poll_interval=1),
                call(mock_proxmox, 'pve', 'UPID:delete',
                     timeout=120, poll_interval=1),
            ]
    
    @patch('app.proxmox_service.wait_for_proxmox_task', return_value=_TASK_OK)
    def test_stop_vm_already_stopped(self, _mock_wait, app):
        """Test stopping a VM that's already stopped"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='lxc',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            mock_proxmox = Mock()
            # Simulate "already stopped" error
            mock_proxmox.nodes().lxc().status.stop.post.side_effect = Exception("already stopped")
            
            result = _strict_destroy(mock_proxmox, vm_record)
            
            # Should still return True (graceful handling)
            assert result is True
    
    def test_stop_vm_failure(self, app):
        """Test VM stop failure"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            mock_proxmox = Mock()
            mock_proxmox.nodes().qemu().status.stop.post.side_effect = Exception("Network error")
            
            result = _strict_destroy(mock_proxmox, vm_record)
            
            assert result is False
            mock_proxmox.nodes().qemu().delete.assert_not_called()
    
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    def test_delete_vm_success(self, mock_wait, app):
        """Test successful VM deletion"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            mock_proxmox = Mock()
            mock_proxmox.nodes().qemu().delete.return_value = None
            
            result = _strict_destroy(mock_proxmox, vm_record)
            
            assert result is True
            mock_proxmox.nodes().qemu().delete.assert_called_once()
    
    @patch('app.proxmox_service.wait_for_proxmox_task', return_value=_TASK_OK)
    def test_delete_vm_not_found(self, _mock_wait, app):
        """Test deleting a VM that doesn't exist"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='lxc',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            mock_proxmox = Mock()
            mock_proxmox.nodes().lxc().delete.side_effect = Exception("does not exist")
            
            # Listed for the identity check, then gone when the delete's
            # "does not exist" is confirmed against fresh inventory.
            result = _strict_destroy(
                mock_proxmox, vm_record, inventory=[_listed(vm_record), []])
            
            # Should return True (VM already gone)
            assert result is True
    
    @patch('app.proxmox_service.wait_for_proxmox_task', return_value=_TASK_OK)
    def test_delete_vm_failure(self, _mock_wait, app):
        """Test VM deletion failure"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=0
            )
            
            mock_proxmox = Mock()
            mock_proxmox.nodes().qemu().delete.side_effect = Exception("Storage locked")
            
            result = _strict_destroy(mock_proxmox, vm_record)
            
            assert result is False


class TestRetryLogic:
    """Test VM deletion retry logic"""
    
    def test_retry_count_increments(self, app):
        """Test that retry count increments on failure"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() - timedelta(hours=1),  # Expired
                deletion_retry_count=0
            )
            db.session.add(vm_record)
            db.session.commit()
            
            # Simulate incrementing retry count
            vm_record.deletion_retry_count += 1
            db.session.commit()
            
            assert vm_record.deletion_retry_count == 1
    
    def test_max_retries_constant(self):
        """Test that max retries is set to expected value"""
        assert VM_DELETION_MAX_RETRIES == 5


class TestAdminAlerts:
    """Test admin alert notification system"""
    
    def test_log_alert_to_file(self, app, tmp_path):
        """Test file logging alert"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=5
            )
            
            log_file = tmp_path / "test_alerts.log"
            message = "Test alert message"
            
            # Mock the alert file path
            with patch('app.tasks.ALERT_LOG_FILE', str(log_file)):
                _log_alert_to_file(vm_record, message)
            
            # Verify file was created and contains message
            assert log_file.exists()
            content = log_file.read_text()
            assert "CRITICAL" in content
            assert message in content
            assert str(vm_record.vmid) in content
    
    def test_create_database_alert(self, app):
        """Test database alert creation"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=5
            )
            db.session.add(vm_record)
            db.session.commit()
            
            message = "VM deletion failed after max retries"
            _create_database_alert(vm_record, message)
            
            # Verify alert was created
            alert = AdminAlert.query.filter_by(vmid=100).first()
            assert alert is not None
            assert alert.alert_type == 'vm_deletion_failed'
            assert alert.severity == 'critical'
            assert alert.message == message
            assert alert.vm_name == 'test-vm'
            assert alert.acknowledged is False
            
            # Verify details contain expected information
            details = json.loads(alert.details)
            assert details['vm_type'] == 'qemu'
            assert details['node'] == 'pve'
            assert details['student_name'] == 'testuser'
            assert details['retry_attempts'] == 5
    
    @patch('app.tasks.smtplib.SMTP')
    def test_send_email_alert_configured(self, mock_smtp, app):
        """Test email alert when SMTP is configured"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=5
            )
            
            # Mock environment variables
            with patch.dict(os.environ, {
                'SMTP_SERVER': 'smtp.gmail.com',
                'SMTP_PORT': '587',
                'SMTP_USERNAME': 'test@example.com',
                'SMTP_PASSWORD': 'password',
                'SMTP_FROM_EMAIL': 'alerts@example.com',
                'ADMIN_ALERT_EMAILS': 'admin@example.com,admin2@example.com'
            }):
                from app.tasks import _send_email_alert
                
                message = "VM deletion failed"
                _send_email_alert(vm_record, message)
                
                # Verify SMTP was called
                mock_smtp.assert_called_once_with('smtp.gmail.com', 587, timeout=10)
    
    @patch('app.tasks.requests.post')
    def test_send_webhook_alert_discord(self, mock_post, app):
        """Test Discord webhook alert"""
        with app.app_context():
            vm_record = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=5
            )
            
            # Mock environment variables
            with patch.dict(os.environ, {
                'ALERT_WEBHOOK_URL': 'https://discord.com/api/webhooks/test',
                'ALERT_WEBHOOK_TYPE': 'discord'
            }):
                from app.tasks import _send_webhook_alert
                
                message = "VM deletion failed"
                mock_post.return_value.status_code = 200
                
                _send_webhook_alert(vm_record, message)
                
                # Verify webhook was called
                assert mock_post.called
                call_args = mock_post.call_args
                
                # Verify Discord embed format
                payload = call_args[1]['json']
                assert 'embeds' in payload
                assert payload['embeds'][0]['title'] == '🚨 VM Deletion Failed'
                assert payload['embeds'][0]['color'] == 15158332  # Red


class TestAdminAlertModel:
    """Test AdminAlert database model"""
    
    def test_create_admin_alert(self, app):
        """Test creating an admin alert"""
        with app.app_context():
            alert = AdminAlert(
                alert_type='vm_deletion_failed',
                severity='critical',
                message='Test alert',
                vmid=100,
                vm_name='test-vm',
                details='{"error": "test"}'
            )
            db.session.add(alert)
            db.session.commit()
            
            # Retrieve and verify
            saved_alert = AdminAlert.query.filter_by(vmid=100).first()
            assert saved_alert is not None
            assert saved_alert.alert_type == 'vm_deletion_failed'
            assert saved_alert.severity == 'critical'
            assert saved_alert.acknowledged is False
    
    def test_alert_to_dict(self, app):
        """Test alert serialization to dictionary"""
        with app.app_context():
            alert = AdminAlert(
                alert_type='vm_deletion_failed',
                severity='warning',
                message='Test alert',
                vmid=100,
                vm_name='test-vm',
                details='{"key": "value"}'
            )
            db.session.add(alert)
            db.session.commit()
            
            alert_dict = alert.to_dict()
            assert alert_dict['alert_type'] == 'vm_deletion_failed'
            assert alert_dict['severity'] == 'warning'
            assert alert_dict['vmid'] == 100
            assert alert_dict['acknowledged'] is False


class TestVMRecordRetryField:
    """Test VMRecord model with deletion_retry_count field"""
    
    def test_vm_record_has_retry_count(self, app):
        """Test that VMRecord has deletion_retry_count field"""
        with app.app_context():
            vm = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1)
            )
            db.session.add(vm)
            db.session.commit()
            
            # Verify default value
            assert vm.deletion_retry_count == 0
    
    def test_vm_record_to_dict_includes_retry_count(self, app):
        """Test that to_dict includes deletion_retry_count"""
        with app.app_context():
            vm = VMRecord(
                vmid=100,
                vm_name='test-vm',
                vm_type='qemu',
                node='pve',
                template_name='ubuntu',
                student_name='testuser',
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=1),
                deletion_retry_count=3
            )
            db.session.add(vm)
            db.session.commit()
            
            vm_dict = vm.to_dict()
            assert 'deletion_retry_count' in vm_dict
            assert vm_dict['deletion_retry_count'] == 3


# ---------------------------------------------------------------------------
# Expiry worker versus concurrent extension / manual deletion
# ---------------------------------------------------------------------------

import threading
from contextlib import contextmanager

import app.tasks as vm_tasks
from app import proxmox_service
from app.models import User
from app.proxmox_service import reset_cluster_vm_index


class _StopExpiryLoop(BaseException):
    pass


def _run_one_expiry_pass(app):
    """Run exactly one iteration of the forever-loop expiry worker."""
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


def _cluster_with_guest(mock_api, *, vmid=100, name="student-student-vm"):
    """ProxmoxAPI boundary fake: one running qemu guest whose tasks succeed."""
    proxmox = MagicMock()
    mock_api.return_value = proxmox
    endpoint = MagicMock(name=f"qemu-{vmid}")
    endpoint.status.stop.post.return_value = f"stop-{vmid}"
    endpoint.delete.return_value = f"delete-{vmid}"
    proxmox.nodes.return_value.qemu.side_effect = lambda requested: endpoint
    proxmox.cluster.resources.get.return_value = [
        {"vmid": vmid, "name": name, "node": "pve", "type": "qemu",
         "status": "running"},
    ]
    task = MagicMock()
    task.status.get.return_value = {"status": "stopped", "exitstatus": "OK"}
    proxmox.nodes.return_value.tasks.return_value = task
    reset_cluster_vm_index()
    return proxmox, endpoint


def _student_with_vm(app, *, expires_in):
    with app.app_context():
        student = User(login_id="student", full_name="Student")
        student.set_password("studentpass")
        db.session.add(student)
        db.session.flush()
        vm = VMRecord(
            vmid=100, vm_name="student-student-vm", vm_type="qemu",
            node="pve", template_name="homework/vm", student_name="student",
            created_by_user_id=student.id,
            expires_at=datetime.now() + expires_in,
        )
        db.session.add(vm)
        db.session.commit()
        return vm.id


def _login_student(client):
    response = client.post(
        "/auth/login", json={"login_id": "student", "password": "studentpass"})
    assert response.status_code == 200


class TestExpiryExtensionRace:
    """ops#38: an extension that lands mid-pass must win over expiry."""

    @patch("app.proxmox_service.ProxmoxAPI")
    def test_extension_after_selection_stops_the_destroy(
            self, mock_api, app, client):
        vm_id = _student_with_vm(app, expires_in=-timedelta(minutes=5))
        _, endpoint = _cluster_with_guest(mock_api)
        selected = threading.Event()
        extended = threading.Event()
        real_lock = proxmox_service.vm_lifecycle_lock

        @contextmanager
        def lock_after_extension(vmid):
            # The worker has loaded its expired batch; hold it here until
            # the student's extension has committed.
            selected.set()
            extended.wait(timeout=5)
            with real_lock(vmid):
                yield

        def worker():
            with patch("app.tasks.vm_lifecycle_lock",
                       side_effect=lock_after_extension):
                _run_one_expiry_pass(app)

        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        try:
            assert selected.wait(timeout=5), "worker never selected the VM"
            _login_student(client)
            response = client.post(
                "/extend", data={"deployment_group": "homework"})
        finally:
            extended.set()
            worker_thread.join(timeout=10)

        assert response.get_json()["success"] is True
        endpoint.status.stop.post.assert_not_called()
        endpoint.delete.assert_not_called()
        with app.app_context():
            vm = db.session.get(VMRecord, vm_id)
            assert vm is not None, "extended VM's record was retired"
            assert vm.expires_at > datetime.now()

    @patch("app.proxmox_service.ProxmoxAPI")
    def test_extend_waits_for_inflight_expiry_destroy(self, mock_api, app):
        vm_id = _student_with_vm(app, expires_in=-timedelta(minutes=5))
        _, endpoint = _cluster_with_guest(mock_api)
        stopping = threading.Event()
        release_stop = threading.Event()
        extend_waiting = threading.Event()
        real_lock = proxmox_service.vm_lifecycle_lock
        response_data = {}

        def blocking_stop():
            stopping.set()
            release_stop.wait(timeout=5)
            return "stop-100"

        endpoint.status.stop.post.side_effect = blocking_stop

        @contextmanager
        def observed_lock(vmid):
            extend_waiting.set()
            with real_lock(vmid):
                yield

        def request_extend():
            with app.test_client() as thread_client:
                _login_student(thread_client)
                with patch("app.routes.vm_lifecycle.vm_lifecycle_lock",
                           side_effect=observed_lock):
                    response = thread_client.post(
                        "/extend", data={"deployment_group": "homework"})
                    response_data.update(response.get_json())

        worker_thread = threading.Thread(
            target=_run_one_expiry_pass, args=(app,))
        worker_thread.start()
        extend_thread = None
        try:
            assert stopping.wait(timeout=5), "worker never began the stop"
            extend_thread = threading.Thread(target=request_extend)
            extend_thread.start()
            reached_lock = extend_waiting.wait(timeout=5)
        finally:
            release_stop.set()
            worker_thread.join(timeout=10)
            if extend_thread is not None:
                extend_thread.join(timeout=10)

        assert reached_lock, "extend never took the VMID lifecycle lock"
        assert response_data["success"] is False, (
            "student was told a VM being destroyed had been extended")
        endpoint.config.put.assert_not_called()
        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is None


class TestExpiryNotStarvedByReaper:
    """ops#46: orphan cleanup must not hold up ordinary expiry."""

    @patch("app.proxmox_service.ProxmoxAPI")
    def test_blocked_reaper_does_not_delay_the_expiry_pass(self, mock_api, app):
        vm_id = _student_with_vm(app, expires_in=-timedelta(minutes=5))
        _, endpoint = _cluster_with_guest(mock_api)
        deleted = threading.Event()
        reaper_entered = threading.Event()
        release_reaper = threading.Event()

        def record_delete():
            deleted.set()
            return "delete-100"

        def blocked_reaper(_proxmox):
            # Stands in for a backlog of orphans each waiting on a hung task.
            reaper_entered.set()
            release_reaper.wait(timeout=5)
            return []

        endpoint.delete.side_effect = record_delete

        def worker():
            with patch("app.tasks.maybe_reap_orphaned_vms",
                       side_effect=blocked_reaper):
                _run_one_expiry_pass(app)

        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        try:
            expired_while_reaper_blocked = deleted.wait(timeout=5)
        finally:
            release_reaper.set()
            worker_thread.join(timeout=10)

        assert expired_while_reaper_blocked, (
            "expired VM waited behind the orphan reaper")
        assert reaper_entered.is_set(), "the reaper still runs every pass"
        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is None


def _queue_webhook_alerts(app, count):
    from tests.test_alert_outbox import _queue_alerts
    with app.app_context():
        _queue_alerts(count)


class TestExpiryNotStarvedByAlerts:
    """Alert delivery must never stand between an expired VM and its destroy."""

    @pytest.fixture(autouse=True)
    def _webhook_configured(self, monkeypatch):
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.edu/a")

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_hung_alert_sends_do_not_delay_the_expiry_pass(
            self, mock_api, _reaper, app):
        """A storage fault queued many alerts and the webhook hangs on each."""
        vm_id = _student_with_vm(app, expires_in=-timedelta(minutes=5))
        _, endpoint = _cluster_with_guest(mock_api)
        _queue_webhook_alerts(app, 5)
        deleted = threading.Event()
        sending = threading.Event()
        release_sends = threading.Event()

        def record_delete():
            deleted.set()
            return "delete-100"

        def hung_post(url, json=None, timeout=None):
            sending.set()
            release_sends.wait(timeout=5)
            raise requests.Timeout("no answer")

        endpoint.delete.side_effect = record_delete

        def worker():
            with patch("app.tasks.requests.post", side_effect=hung_post):
                _run_one_expiry_pass(app)

        worker_thread = threading.Thread(target=worker)
        worker_thread.start()
        try:
            expired_while_sends_hung = deleted.wait(timeout=5)
        finally:
            release_sends.set()
            worker_thread.join(timeout=60)

        assert expired_while_sends_hung, (
            "expired VM waited behind hung alert sends")
        assert sending.is_set(), "alerts are still dispatched every pass"
        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is None

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    # ProxmoxBackend opens the pass's connection at the definition site.
    @patch("app.proxmox_service.get_proxmox_connection",
           side_effect=RuntimeError("Proxmox unreachable"))
    def test_proxmox_outage_still_delivers_queued_alerts(
            self, _connect, _reaper, app):
        """The alerts that matter most during an outage still go out."""
        from app.models import AlertOutbox
        _queue_webhook_alerts(app, 1)
        response = MagicMock()
        with patch("app.tasks.requests.post",
                   return_value=response) as post:
            _run_one_expiry_pass(app)
        assert post.call_count == 1
        with app.app_context():
            assert AlertOutbox.query.one().status == "sent"


class TestManualDeletionReconciliation:
    """ops#43: a guest deleted by hand in Proxmox stops holding capacity."""

    @pytest.fixture(autouse=True)
    def _immediate_confirmation(self):
        # Two sightings normally have to be ORPHAN_CONFIRM_SECONDS apart;
        # the passes below run back to back, so only the count matters here.
        with patch("app.routes.utils.ORPHAN_CONFIRM_SECONDS", 0.0), \
                patch.dict("app.routes.utils._missing_since", clear=True):
            yield

    @staticmethod
    def _live_vm(app, vmid, name, *, koth=False):
        with app.app_context():
            vm = VMRecord(
                vmid=vmid, vm_name=name, vm_type="qemu", node="pve",
                template_name="homework/vm", student_name="student",
                expires_at=datetime.now() + timedelta(hours=2),
            )
            db.session.add(vm)
            db.session.commit()
            return vm.id

    @staticmethod
    def _inventory(mock_api, listings):
        """Each cluster inventory read returns the next listing in turn."""
        proxmox = MagicMock()
        mock_api.return_value = proxmox
        reads = iter(listings)

        def read_inventory(**_kwargs):
            listing = next(reads)
            if isinstance(listing, Exception):
                raise listing
            return [
                {"vmid": vmid, "name": name, "node": "pve", "type": "qemu",
                 "status": "running"}
                for vmid, name in listing
            ]

        proxmox.cluster.resources.get.side_effect = read_inventory
        reset_cluster_vm_index()
        return proxmox

    @staticmethod
    def _reconciled_alerts(app):
        with app.app_context():
            return [(a.vmid, a.message) for a in
                    AdminAlert.query.filter_by(
                        alert_type="vm_record_reconciled").all()]

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_confirmed_absence_retires_record_and_releases_capacity(
            self, mock_api, _reaper, app):
        from app.proxmox_service import _load_live_vm_records
        gone_id = self._live_vm(app, 100, "student-student-gone")
        kept_id = self._live_vm(app, 101, "student-student-kept")
        neighbour = [(101, "student-student-kept")]
        # Pass 1: snapshot + confirmation; pass 2: snapshot + confirmation.
        proxmox = self._inventory(mock_api, [neighbour] * 4)

        _run_one_expiry_pass(app)
        with app.app_context():
            assert db.session.get(VMRecord, gone_id) is not None, (
                "one sighting must never retire a record")
        _run_one_expiry_pass(app)

        with app.app_context():
            assert db.session.get(VMRecord, gone_id) is None
            assert db.session.get(VMRecord, kept_id) is not None
            # The retired record no longer counts toward admission.
            with db.engine.connect() as conn:
                assert [r.vmid for r in _load_live_vm_records(conn)] == [101]
        alerts = self._reconciled_alerts(app)
        assert [vmid for vmid, _ in alerts] == [100]
        assert "outside the portal" in alerts[0][1]
        proxmox.nodes.return_value.qemu.return_value.delete.assert_not_called()

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_koth_record_is_marked_destroyed_not_deleted(
            self, mock_api, _reaper, app):
        from app.models import KothBox, KothGame
        with app.app_context():
            game = KothGame(name="Game", deployment_group_name="koth-lab",
                            entry_vlan_tag=300, internal_vlan_tag=400)
            db.session.add(game)
            db.session.flush()
            vm = VMRecord(
                vmid=100, vm_name="koth-box", vm_type="qemu", node="pve",
                template_name="koth-lab/box", student_name="koth",
                koth_game_id=game.id,
                expires_at=datetime.now() + timedelta(hours=2),
            )
            db.session.add(vm)
            db.session.flush()
            db.session.add(KothBox(game_id=game.id, vm_record_id=vm.id,
                                   name="box"))
            db.session.commit()
            vm_id = vm.id
        self._inventory(mock_api, [[]] * 4)

        _run_one_expiry_pass(app)
        _run_one_expiry_pass(app)

        with app.app_context():
            vm = db.session.get(VMRecord, vm_id)
            assert vm is not None and vm.destroyed is True

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_transient_inventory_miss_keeps_record(
            self, mock_api, _reaper, app):
        vm_id = self._live_vm(app, 100, "student-student-vm")
        listed = [(100, "student-student-vm")]
        # Missing, listed again, then missing once more: never two in a row.
        self._inventory(mock_api, [[], [], listed, [], []])

        for _ in range(3):
            _run_one_expiry_pass(app)

        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is not None
        assert self._reconciled_alerts(app) == []

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_failed_inventory_read_keeps_record(self, mock_api, _reaper, app):
        vm_id = self._live_vm(app, 100, "student-student-vm")
        outage = RuntimeError("cluster/resources unavailable")
        # Snapshot shows it missing, then every confirmation read fails.
        self._inventory(mock_api, [[], outage, [], outage, [], outage])

        for _ in range(3):
            _run_one_expiry_pass(app)

        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is not None
        assert self._reconciled_alerts(app) == []

    @patch("app.tasks.maybe_reap_orphaned_vms", return_value=[])
    @patch("app.proxmox_service.ProxmoxAPI")
    def test_vmid_reused_by_another_guest_keeps_record(
            self, mock_api, _reaper, app):
        """Absence is decided by VMID: a reused VMID is not proof of deletion."""
        vm_id = self._live_vm(app, 100, "student-student-vm")
        self._inventory(mock_api, [[(100, "group-team1-other")]] * 4)

        _run_one_expiry_pass(app)
        _run_one_expiry_pass(app)

        with app.app_context():
            assert db.session.get(VMRecord, vm_id) is not None


def register_cloud_sweep_backend(name):
    from app.backends import register_backend_factory
    from tests.test_orphan_reaper import _CloudSweepBackend
    sweep = _CloudSweepBackend(name)
    register_backend_factory(name, lambda: sweep)
    return sweep


def test_cloud_only_expiry_pass_runs_the_cloud_sweep(app, monkeypatch):
    # Catches a cloud-only portal never reaping leaked cloud resources (C6).
    import importlib
    from app import orphan_reaper
    from app.backends import register_backend_factory
    from tests.test_orphan_reaper import NOW_EPOCH
    config = importlib.import_module("app.config")
    monkeypatch.setattr(config, "ENABLED_BACKENDS", ("aws",))
    monkeypatch.setattr(orphan_reaper, "ORPHAN_REAPER_ENABLED", True)
    monkeypatch.setattr(orphan_reaper, "_last_sweep_at", None)

    class _SweepStart(datetime):
        # The fake orphans are aged against NOW_EPOCH, not the real clock.
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(NOW_EPOCH, tz)

    monkeypatch.setattr(orphan_reaper, "datetime", _SweepStart)
    sweep = register_cloud_sweep_backend("aws")
    sweep.reset_with_old_orphans(1)
    register_backend_factory("proxmox", lambda: pytest.fail("Proxmox constructed"))
    _run_one_expiry_pass(app)
    assert sweep.resources == []


@patch("app.proxmox_service.get_proxmox_connection",
       side_effect=RuntimeError("Proxmox unreachable"))
def test_proxmox_outage_pass_still_runs_the_cloud_sweep_without_proxmox(
        _connect, app, monkeypatch):
    # Catches the pass-start Proxmox outage skip also skipping cloud cleanup,
    # or handing the reaper a client it could not open.
    import importlib
    config = importlib.import_module("app.config")
    monkeypatch.setattr(config, "ENABLED_BACKENDS", ("proxmox", "aws"))
    with patch("app.tasks.maybe_reap_orphaned_vms", return_value=[]) as reaper:
        _run_one_expiry_pass(app)
    reaper.assert_called_once_with(None)


# ---- idle cloud routers (T16) ----------------------------------------------

import importlib
import time

from app.backends import register_backend_factory, reset_backends_for_tests
from app.backends.base import BackendError, LaunchedVM
from app.backends.cloud_common import router_lock_key
from app.models import NodeMemoryReservation
from app.proxmox_service import vm_lifecycle_lock
from app.tasks import _reset_router_idle_state_for_tests, _stop_idle_cloud_routers
from tests.backends.fake_backend import FakeBackend

config = importlib.import_module("app.config")

_IDLE_VM_NAME = "router-idle-vm"


@pytest.fixture
def router_backend(app, monkeypatch):
    # Router-idle tests only: one enabled fake AWS backend with a running router.
    fake = FakeBackend('aws')
    fake.router_running = True
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws',))
    monkeypatch.setattr(config, 'AWS_ROUTER_IDLE_MINUTES', 30)
    register_backend_factory('aws', lambda: fake)
    _reset_router_idle_state_for_tests()
    with app.app_context():
        yield fake
    _reset_router_idle_state_for_tests()
    reset_backends_for_tests()


def add_live_record(backend, *, vmid=4100, expires_at=None):
    record = VMRecord(
        vmid=vmid, vm_name=_IDLE_VM_NAME, vm_type="instance", node=backend,
        backend=backend, provider_id=f"provider-{vmid}",
        template_name="cloud/vm", student_name="student",
        expires_at=expires_at or datetime.now() + timedelta(hours=1))
    db.session.add(record)
    db.session.commit()
    return record


def retire_record():
    VMRecord.query.filter_by(vm_name=_IDLE_VM_NAME).delete()
    db.session.commit()


def observe_idle_at(t):
    assert _stop_idle_cloud_routers(now_epoch=t) == []


def add_node_reservation(node, expires_at, reservation_id='r-idle'):
    db.session.add(NodeMemoryReservation(
        reservation_id=reservation_id, node=node, memory_bytes=1,
        expires_at=expires_at))
    db.session.commit()


def remove_reservation():
    NodeMemoryReservation.query.filter_by(reservation_id='r-idle').delete()
    db.session.commit()


def arrange_lock_owner_to_insert_reservation_before_yield(app, backend, expires_at):
    """A launch admission holding the router lock when the idle check arrives."""
    holding = threading.Event()

    def owner():
        with app.app_context():
            with vm_lifecycle_lock(router_lock_key(backend)):
                holding.set()
                time.sleep(0.2)
                add_node_reservation(node=backend, expires_at=expires_at)

    thread = threading.Thread(target=owner)
    thread.start()
    assert holding.wait(timeout=5), "lock owner never took the router lock"
    return thread


def test_router_stops_only_after_one_continuous_idle_window(app, router_backend):
    # Catches stopping immediately when the last row disappears.
    add_live_record(backend="aws")
    assert _stop_idle_cloud_routers(now_epoch=1_000) == []
    retire_record()
    assert _stop_idle_cloud_routers(now_epoch=1_001) == []
    assert _stop_idle_cloud_routers(now_epoch=2_800) == []
    assert _stop_idle_cloud_routers(now_epoch=2_801) == ["aws"]
    assert router_backend.router_running is False


def test_live_record_or_unexpired_admission_reservation_resets_idle_window(app, router_backend):
    # Catches a stop racing a new deployment that has reserved capacity but no VMRecord yet.
    observe_idle_at(1_000)
    add_node_reservation(node="aws", expires_at=2_000)
    assert _stop_idle_cloud_routers(now_epoch=1_100) == []
    remove_reservation()
    assert _stop_idle_cloud_routers(now_epoch=1_200) == []  # first idle observation
    assert _stop_idle_cloud_routers(now_epoch=2_999) == []
    assert router_backend.router_running is True
    assert _stop_idle_cloud_routers(now_epoch=3_000) == ["aws"]


def test_unexpired_admission_reservation_blocks_stop_without_any_record(app, router_backend):
    # Catches a stop between a launch's router start and its VMRecord commit (C4).
    observe_idle_at(0)
    add_node_reservation(node="aws", expires_at=10_060)
    assert _stop_idle_cloud_routers(now_epoch=10_000) == []
    assert router_backend.router_running is True


def test_expired_reservation_does_not_block_stop(app, router_backend):
    # Catches a leaked lease keeping the router billing forever (M13).
    observe_idle_at(0)
    add_node_reservation(node="aws", expires_at=9_999)
    assert _stop_idle_cloud_routers(now_epoch=10_000) == ["aws"]


def test_other_backends_workload_does_not_keep_router_running(app, router_backend):
    # Catches counting a Proxmox record or another cloud's lease as AWS work.
    observe_idle_at(0)
    add_live_record(backend="proxmox")
    add_node_reservation(node="gcp", expires_at=10_060)
    assert _stop_idle_cloud_routers(now_epoch=10_000) == ["aws"]


def test_destroyed_koth_record_does_not_keep_router_running(app, router_backend):
    # Catches a retained-as-destroyed KOTH row counting as a live guest.
    record = add_live_record(backend="aws")
    record.destroyed = True
    db.session.commit()
    observe_idle_at(0)
    assert _stop_idle_cloud_routers(now_epoch=10_000) == ["aws"]


def test_router_lock_rechecks_workload_before_stop(app, router_backend):
    # Catches launch admission landing while the idle worker waits for the router lock.
    observe_idle_at(0)
    owner = arrange_lock_owner_to_insert_reservation_before_yield(
        app, backend="aws", expires_at=10_060)
    try:
        assert _stop_idle_cloud_routers(now_epoch=10_000) == []
    finally:
        owner.join(timeout=5)
    assert router_backend.router_running is True


def test_worker_restart_waits_a_fresh_idle_interval(app, router_backend):
    # Catches a restart stopping at once because no in-memory history says otherwise.
    observe_idle_at(0)
    _reset_router_idle_state_for_tests()
    assert _stop_idle_cloud_routers(now_epoch=10_000) == []
    assert router_backend.router_running is True
    assert _stop_idle_cloud_routers(now_epoch=11_800) == ["aws"]


def test_stopped_router_is_not_stopped_again_every_pass(app, router_backend):
    # Catches calling the provider every minute once the router is already stopped.
    observe_idle_at(0)
    assert _stop_idle_cloud_routers(now_epoch=1_800) == ["aws"]
    router_backend.router_running = True  # only a fresh stop could clear this
    assert _stop_idle_cloud_routers(now_epoch=1_860) == []
    assert router_backend.router_running is True


def test_failed_router_stop_keeps_its_idle_timestamp_for_retry(app, router_backend):
    # Catches a failed stop being reported as stopped, or restarting the idle window.
    def refuse_stop():
        raise BackendError("router stop wait failed")

    observe_idle_at(0)
    router_backend.stop_router = refuse_stop
    assert _stop_idle_cloud_routers(now_epoch=1_800) == []
    assert router_backend.router_running is True
    del router_backend.stop_router
    assert _stop_idle_cloud_routers(now_epoch=1_860) == ["aws"]
    assert router_backend.router_running is False


def test_one_backends_stop_failure_does_not_block_the_other(
        app, router_backend, monkeypatch):
    # Catches one cloud's provider error ending the router step for every cloud.
    gcp = FakeBackend('gcp')
    gcp.router_running = True
    monkeypatch.setattr(config, 'ENABLED_BACKENDS', ('aws', 'gcp'))
    monkeypatch.setattr(config, 'GCP_ROUTER_IDLE_MINUTES', 30)
    register_backend_factory('gcp', lambda: gcp)

    def refuse_stop():
        raise BackendError("router stop wait failed")

    router_backend.stop_router = refuse_stop
    observe_idle_at(0)
    assert _stop_idle_cloud_routers(now_epoch=1_800) == ["gcp"]
    assert (router_backend.router_running, gcp.router_running) == (True, False)


def test_router_idle_uses_each_backends_configured_minutes(
        app, router_backend, monkeypatch):
    # Catches one hard-coded interval for every cloud.
    monkeypatch.setattr(config, 'AWS_ROUTER_IDLE_MINUTES', 5)
    observe_idle_at(0)
    assert _stop_idle_cloud_routers(now_epoch=299) == []
    assert _stop_idle_cloud_routers(now_epoch=300) == ["aws"]


@pytest.fixture
def expiry_events(router_backend, monkeypatch):
    events = []

    def recorder(name, result=None):
        def record(*args, **kwargs):
            events.append(name)
            return result
        return record

    monkeypatch.setattr(vm_tasks, "reconcile_missing_vm_records",
                        recorder("reconcile", []))
    monkeypatch.setattr(vm_tasks, "dispatch_pending_alerts", recorder("alerts"))
    monkeypatch.setattr(vm_tasks, "maybe_reap_orphaned_vms",
                        recorder("orphans", []))
    monkeypatch.setattr(vm_tasks, "_stop_idle_cloud_routers",
                        recorder("routers", []))

    # One expired AWS record whose guest is still there to destroy.
    vmid = 4200
    router_backend.instances[vmid] = LaunchedVM(
        vmid=vmid, vm_type="aws", location="memory-zone", network_slot=None,
        provider_id=f"provider-{vmid}")
    router_backend._instance_tags[vmid] = {
        "vm_name": _IDLE_VM_NAME, "deployment_id": None}
    add_live_record(backend="aws", vmid=vmid,
                    expires_at=datetime.now() - timedelta(minutes=5))
    destroy = router_backend.destroy

    def recording_destroy(ref, policy):
        events.append("destroy")
        return destroy(ref, policy)

    router_backend.destroy = recording_destroy
    return events


def test_router_idle_step_runs_after_reconcile_alerts_and_orphan_sweep(
        app, router_backend, expiry_events):
    # Catches shared-cost cleanup moving ahead of the safety-critical expiry phases.
    _run_one_expiry_pass(app)
    assert expiry_events == ["destroy", "reconcile", "alerts", "orphans", "routers"]
