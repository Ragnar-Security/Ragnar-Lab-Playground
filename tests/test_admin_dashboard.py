import unittest
import json
from unittest.mock import patch, MagicMock, call
from app import create_app
from app.models import db, User, VMRecord, StudentLabAssignment
from datetime import datetime, timedelta


class TestAdminDashboard(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        # Create admin and student users
        self.admin = User(login_id='admin', full_name='Admin', is_admin=True, entry_vlan_tag=100, internal_vlan_tag=200)
        self.admin.set_password('adminpass')
        self.student = User(login_id='student1', full_name='Student One', is_admin=False, entry_vlan_tag=101, internal_vlan_tag=201)
        self.student.set_password('studentpass')
        self.student2 = User(login_id='student2', full_name='Student Two', is_admin=False, entry_vlan_tag=102, internal_vlan_tag=202)
        self.student2.set_password('studentpass2')
        db.session.add_all([self.admin, self.student, self.student2])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_as(self, login_id, password):
        return self.client.post('/auth/login', data={'login_id': login_id, 'password': password})

    def test_disabled_user_session_is_rejected(self):
        """A disabled user's existing session is treated as logged out."""
        # Login as student first
        resp = self.login_as('student1', 'studentpass')
        self.assertEqual(resp.status_code, 200)

        # Disable the student
        self.student.disabled = True
        db.session.commit()

        # Try to access a protected route (login_required)
        resp = self.client.get('/list')
        self.assertEqual(resp.status_code, 401)
        data = json.loads(resp.data)
        self.assertFalse(data['success'])
        self.assertIn('authentication required', data['error'].lower())


    def test_admin_can_disable_user(self):
        self.login_as('admin', 'adminpass')
        response = self.client.put(f'/admin/students/{self.student.id}',
            data=json.dumps({'disabled': True}), content_type='application/json')
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        updated = db.session.get(User, self.student.id)
        self.assertTrue(updated.disabled)

    def test_admin_can_toggle_admin(self):
        self.login_as('admin', 'adminpass')
        response = self.client.put(f'/admin/students/{self.student.id}',
            data=json.dumps({'is_admin': True}), content_type='application/json')
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        updated = db.session.get(User, self.student.id)
        self.assertTrue(updated.is_admin)

    def test_list_students_full(self):
        vm = VMRecord(vmid=100, vm_name='test-vm', vm_type='qemu', node='pve',
                      template_name='hw1/vm1', student_name='student1',
                      created_by_user_id=self.student.id,
                      created_at=datetime.now(), expires_at=datetime.now() + timedelta(hours=1))
        db.session.add(vm)
        db.session.commit()
        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/students/full')
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        student1_data = next(s for s in data['students'] if s['login_id'] == 'student1')
        self.assertEqual(student1_data['active_vm_count'], 1)

    def test_list_students_non_admin_forbidden(self):
        self.login_as('student1', 'studentpass')
        response = self.client.get('/admin/students/full')
        self.assertEqual(response.status_code, 403)

    def test_update_student_not_found(self):
        self.login_as('admin', 'adminpass')
        response = self.client.put('/admin/students/999',
            data=json.dumps({'disabled': True}), content_type='application/json')
        self.assertEqual(response.status_code, 404)


    def _create_test_vms(self):
        now = datetime.now()
        vms = [
            VMRecord(vmid=100, vm_name='s1-hw1-vm1', vm_type='qemu', node='pve',
                     template_name='homework1/vm1', student_name='student1',
                     created_by_user_id=self.student.id,
                     created_at=now, expires_at=now + timedelta(hours=2)),
            VMRecord(vmid=101, vm_name='s1-hw2-vm1', vm_type='qemu', node='pve2',
                     template_name='homework2/vm1', student_name='student1',
                     created_by_user_id=self.student.id,
                     created_at=now, expires_at=now + timedelta(hours=2)),
            VMRecord(vmid=102, vm_name='s2-hw1-vm1', vm_type='qemu', node='pve',
                     template_name='homework1/vm1', student_name='student2',
                     created_by_user_id=self.student2.id,
                     created_at=now, expires_at=now + timedelta(hours=2)),
        ]
        db.session.add_all(vms)
        db.session.commit()
        return vms

    def _set_inventory(self, proxmox, vms):
        proxmox.cluster.resources.get.return_value = [
            {
                'vmid': vm.vmid,
                'name': vm.vm_name,
                'node': vm.node,
                'type': vm.vm_type,
                'status': 'running',
            }
            for vm in vms
        ]

    def test_list_all_vms(self):
        self._create_test_vms()
        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/vms')
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['vms']), 3)

    def test_list_vms_filter_by_student(self):
        self._create_test_vms()
        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/vms?student=student1')
        data = json.loads(response.data)
        self.assertEqual(len(data['vms']), 2)

    def test_list_vms_filter_by_node(self):
        self._create_test_vms()
        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/vms?node=pve')
        data = json.loads(response.data)
        self.assertEqual(len(data['vms']), 2)

    def test_bulk_extend(self):
        vms = self._create_test_vms()
        original_expiry = vms[0].expires_at
        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/bulk-extend',
            data=json.dumps({'vm_ids': [vms[0].id], 'minutes': 60}),
            content_type='application/json')
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['extended'], 1)
        updated = db.session.get(VMRecord, vms[0].id)
        self.assertEqual(updated.expires_at, original_expiry + timedelta(minutes=60))


    # ---- Bulk Destroy Tests ----

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_stops_then_deletes(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """Bulk destroy should stop each VM before deleting it"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        vms = self._create_test_vms()
        self._set_inventory(mock_proxmox, vms)
        self.login_as('admin', 'adminpass')

        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [vms[0].id, vms[1].id]}),
            content_type='application/json')
        data = json.loads(response.data)

        self.assertTrue(data['success'])
        self.assertEqual(data['destroyed'], 2)

        # Verify stop was called for both VMs before delete
        nodes = mock_proxmox.nodes
        # VM 100 (qemu on pve): stop then delete
        nodes('pve').qemu(100).status.stop.post.assert_called()
        nodes('pve').qemu(100).delete.assert_called()
        # VM 101 (qemu on pve2): stop then delete
        nodes('pve2').qemu(101).status.stop.post.assert_called()
        nodes('pve2').qemu(101).delete.assert_called()

        # Verify DB records are deleted
        for vm_id in [vms[0].id, vms[1].id]:
            vm = db.session.get(VMRecord, vm_id)
            self.assertIsNone(vm)

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_multiple_vms_all_destroyed(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """All selected VMs should be destroyed, not just the first or last"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        vms = self._create_test_vms()  # Creates 3 VMs: ids 100, 101, 102
        self._set_inventory(mock_proxmox, vms)
        self.login_as('admin', 'adminpass')

        # Destroy all 3
        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [vms[0].id, vms[1].id, vms[2].id]}),
            content_type='application/json')
        data = json.loads(response.data)

        self.assertEqual(data['destroyed'], 3)
        self.assertEqual(len(data['errors']), 0)

        # ALL 3 DB records should be deleted
        for vm in vms:
            refreshed = db.session.get(VMRecord, vm.id)
            self.assertIsNone(refreshed, f"VM {vm.vmid} record should be deleted")

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_nonexistent_vm_ids_skipped(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """Non-existent VM IDs should be skipped gracefully"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        vms = self._create_test_vms()
        self._set_inventory(mock_proxmox, vms)
        self.login_as('admin', 'adminpass')

        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [vms[0].id, 99999]}),
            content_type='application/json')
        data = json.loads(response.data)

        # Only 1 should be destroyed (99999 doesn't exist, skipped)
        self.assertEqual(data['destroyed'], 1)

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_handles_404_gracefully(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """If Proxmox says VM doesn't exist (404), still clean up DB"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        # Make delete raise a 404-like error
        mock_proxmox.nodes('pve').qemu(100).delete.side_effect = Exception('VM 100 does not exist')

        vms = self._create_test_vms()
        self._set_inventory(mock_proxmox, [])
        self.login_as('admin', 'adminpass')

        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [vms[0].id]}),
            content_type='application/json')
        data = json.loads(response.data)

        # Should count as destroyed (VM gone from Proxmox = success, record deleted)
        self.assertEqual(data['destroyed'], 1)
        self.assertEqual(len(data['errors']), 0)
        vm = db.session.get(VMRecord, vms[0].id)
        self.assertIsNone(vm)

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_reports_real_errors(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """Non-404 Proxmox errors should be reported in the errors list"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        # Make delete raise a real error (not a 404)
        mock_proxmox.nodes('pve').qemu(100).delete.side_effect = Exception('Permission denied')

        vms = self._create_test_vms()
        self._set_inventory(mock_proxmox, vms)
        self.login_as('admin', 'adminpass')

        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [vms[0].id]}),
            content_type='application/json')
        data = json.loads(response.data)

        # Should report the error and NOT delete the DB record
        self.assertEqual(data['destroyed'], 0)
        self.assertEqual(len(data['errors']), 1)
        self.assertIn('Permission denied', data['errors'][0])
        vm = db.session.get(VMRecord, vms[0].id)
        self.assertIsNotNone(vm, "DB record should be preserved when Proxmox delete fails")

    def test_bulk_destroy_requires_vm_ids(self):
        """POST /admin/bulk-destroy without vm_ids returns 400"""
        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({}),
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    @patch('time.sleep')
    @patch('app.proxmox_service.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.proxmox_service.get_proxmox_connection')
    def test_bulk_destroy_lxc_vms(
            self, mock_get_proxmox, _mock_wait, mock_time):
        """Bulk destroy should handle LXC containers correctly"""
        mock_proxmox = MagicMock()
        mock_get_proxmox.return_value = mock_proxmox

        now = datetime.now()
        lxc_vm = VMRecord(vmid=200, vm_name='lxc-test', vm_type='lxc', node='pve',
                          template_name='hw1/web', student_name='student1',
                          created_by_user_id=self.student.id,
                          created_at=now, expires_at=now + timedelta(hours=2))
        db.session.add(lxc_vm)
        self._set_inventory(mock_proxmox, [lxc_vm])
        db.session.commit()

        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/bulk-destroy',
            data=json.dumps({'vm_ids': [lxc_vm.id]}),
            content_type='application/json')
        data = json.loads(response.data)

        self.assertEqual(data['destroyed'], 1)
        # Verify LXC-specific calls (not qemu)
        mock_proxmox.nodes('pve').lxc(200).status.stop.post.assert_called()
        mock_proxmox.nodes('pve').lxc(200).delete.assert_called()


    def test_list_vms_filter_by_status_active(self):
        """GET /admin/vms?status=active should return only active (non-destroyed) VMs"""
        vms = self._create_test_vms()
        # Destroy one VM
        vms[0].destroyed = True
        vms[0].destroyed_at = datetime.now()
        db.session.commit()

        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/vms?status=active')
        data = json.loads(response.data)

        self.assertTrue(data['success'])
        self.assertEqual(len(data['vms']), 2)
        for vm in data['vms']:
            self.assertFalse(vm.get('destroyed', False))

    def test_list_vms_filter_by_status_destroyed(self):
        """GET /admin/vms?status=destroyed should return only destroyed VMs"""
        vms = self._create_test_vms()
        # Destroy one VM
        vms[0].destroyed = True
        vms[0].destroyed_at = datetime.now()
        db.session.commit()

        self.login_as('admin', 'adminpass')
        response = self.client.get('/admin/vms?status=destroyed')
        data = json.loads(response.data)

        self.assertTrue(data['success'])
        self.assertEqual(len(data['vms']), 1)
        self.assertTrue(data['vms'][0]['destroyed'])

    def test_bulk_extend_skips_destroyed(self):
        """Bulk extend should skip destroyed VMs and only extend active ones"""
        vms = self._create_test_vms()
        original_expiry_1 = vms[1].expires_at
        # Destroy first VM
        vms[0].destroyed = True
        vms[0].destroyed_at = datetime.now()
        destroyed_expiry = vms[0].expires_at
        db.session.commit()

        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/bulk-extend',
            data=json.dumps({'vm_ids': [vms[0].id, vms[1].id], 'minutes': 60}),
            content_type='application/json')
        data = json.loads(response.data)

        self.assertTrue(data['success'])
        self.assertEqual(data['extended'], 1)
        # Destroyed VM should not be extended
        destroyed_vm = db.session.get(VMRecord, vms[0].id)
        self.assertEqual(destroyed_vm.expires_at, destroyed_expiry)
        # Active VM should be extended
        active_vm = db.session.get(VMRecord, vms[1].id)
        self.assertEqual(active_vm.expires_at, original_expiry_1 + timedelta(minutes=60))

    def test_bulk_extend_default_minutes(self):
        """Bulk extend without specifying minutes should default to 1440"""
        vms = self._create_test_vms()
        original_expiry = vms[0].expires_at

        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/bulk-extend',
            data=json.dumps({'vm_ids': [vms[0].id]}),
            content_type='application/json')
        data = json.loads(response.data)

        self.assertTrue(data['success'])
        self.assertEqual(data['extended'], 1)
        updated = db.session.get(VMRecord, vms[0].id)
        self.assertEqual(updated.expires_at, original_expiry + timedelta(minutes=1440))

    def test_bulk_assign_invalid_user_ids(self):
        """POST /admin/assignments/bulk with non-existent user_ids should return 400"""
        self.login_as('admin', 'adminpass')
        response = self.client.post('/admin/assignments/bulk',
            data=json.dumps({
                'user_ids': [99999, 99998],
                'deployment_group_names': ['homework1']
            }),
            content_type='application/json')

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertFalse(data['success'])
        self.assertIn('invalid', data['error'].lower())


if __name__ == '__main__':
    unittest.main()
