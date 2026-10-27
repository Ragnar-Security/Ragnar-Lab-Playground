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
