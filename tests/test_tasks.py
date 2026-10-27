"""
Test cases for background tasks module
Tests VM expiration checking, deletion retry logic, and admin alerts
"""

import pytest
import os
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock, mock_open
from app.tasks import (
    _stop_vm, _delete_vm, _send_deletion_failure_alert,
    _log_alert_to_file, _create_database_alert,
    VM_DELETION_MAX_RETRIES
)
from app.models import VMRecord, AdminAlert, db


class TestVMStopAndDelete:
    """Test VM stop and delete helper functions"""
    
    def test_stop_vm_success(self, app):
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
            
            result = _stop_vm(mock_proxmox, vm_record)
            
            assert result is True
            mock_proxmox.nodes().qemu().status.stop.post.assert_called_once()

    @patch('app.tasks.wait_for_proxmox_task',
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

            assert _stop_vm(mock_proxmox, vm_record) is True

            mock_wait.assert_called_once_with(
                mock_proxmox, 'pve', 'UPID:stop',
                timeout=60, poll_interval=1)
    
    def test_stop_vm_already_stopped(self, app):
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
            
            result = _stop_vm(mock_proxmox, vm_record)
            
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
            
            result = _stop_vm(mock_proxmox, vm_record)
            
            assert result is False
    
    @patch('app.tasks.wait_for_proxmox_task',
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
            
            result = _delete_vm(mock_proxmox, vm_record)
            
            assert result is True
            mock_proxmox.nodes().qemu().delete.assert_called_once()
    
    def test_delete_vm_not_found(self, app):
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
            mock_proxmox.cluster.resources.get.return_value = []
            
            result = _delete_vm(mock_proxmox, vm_record)
            
            # Should return True (VM already gone)
            assert result is True
    
    def test_delete_vm_failure(self, app):
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
            
            result = _delete_vm(mock_proxmox, vm_record)
            
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
                mock_smtp.assert_called_once_with('smtp.gmail.com', 587)
    
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
            assert [r.vmid for r in _load_live_vm_records()] == [101]
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
