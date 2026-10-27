"""
Component tests for API routes
Tests VM creation, listing, extending, and destroying
"""

import unittest
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from app import create_app
from app.routes.utils import VM_PRESENT
from app.models import db, User, VMRecord, StudentGroup, StudentGroupMember


class TestAPIRoutes(unittest.TestCase):
    """Test cases for API routes"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret-key'
        
        self.client = self.app.test_client()
        
        self.app_context = self.app.app_context()
        self.app_context.push()
        
        db.create_all()
        
        # Create test user with VLANs and login
        self.test_user = User(
            login_id='testuser',
            full_name='Test User',
            is_admin=False,
            entry_vlan_tag=101,  # Required for VM creation
            internal_vlan_tag=201  # Required for VM creation
        )
        self.test_user.set_password('password123')
        db.session.add(self.test_user)
        db.session.commit()
        
        # Login
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )
    
    def tearDown(self):
        """Clean up test fixtures"""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
    
    def test_get_templates_list(self):
        """Test getting list of deployment groups"""
        with patch('app.routes.templates.load_template_config') as mock_load:
            mock_load.return_value = {
                'deployment_groups': [
                    {
                        'display_name': 'homework1',
                        'description': 'Test HW1',
                        'enabled': True,
                        'uptime_minutes': 120,
                        'extend_time_minutes': 60,
                        'templates': [
                            {'name': 'vm1', 'type': 'lxc', 'memory': 2048, 'cores': 2, 'storage': 20}
                        ]
                    }
                ]
            }

            response = self.client.get('/templates')
            
            data = json.loads(response.data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data['success'])
            self.assertEqual(len(data['deployment_groups']), 1)
            self.assertEqual(data['deployment_groups'][0]['display_name'], 'homework1')
    
    def test_list_vms_empty(self):
        """Test listing VMs when none exist"""
        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox:
            mock_proxmox.return_value = MagicMock()
            response = self.client.get('/list')

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['students']), 0)
    
    def test_list_vms_with_data(self):
        """Test listing VMs with existing data"""
        # Create test VM records
        expires_at = datetime.now() + timedelta(hours=2)
        vm1 = VMRecord(
            vmid=100,
            vm_name='student-testuser-webserver',
            vm_type='lxc',
            node='pve1',
            template_name='homework1/webserver',
            student_name='testuser',
            memory=2048,
            cores=2,
            storage=20,
            ip_address='10.0.0.100',
            expires_at=expires_at,
            created_by_user_id=self.test_user.id
        )
        vm2 = VMRecord(
            vmid=101,
            vm_name='student-testuser-database',
            vm_type='lxc',
            node='pve1',
            template_name='homework1/database',
            student_name='testuser',
            memory=4096,
            cores=4,
            storage=40,
            ip_address='10.0.0.101',
            expires_at=expires_at,
            created_by_user_id=self.test_user.id
        )
        
        db.session.add_all([vm1, vm2])
        db.session.commit()

        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox, \
             patch('app.routes.vm_listing._verify_vm_exists', return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses', return_value=[]):
            mock_proxmox.return_value = MagicMock()
            response = self.client.get('/list')

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['students']), 1)
        student = data['students'][0]
        self.assertEqual(len(student['deployment_groups']), 1)
        self.assertEqual(student['deployment_groups'][0]['vm_count'], 2)
        self.assertEqual(student['deployment_groups'][0]['group_name'], 'homework1')
    
    def test_list_vms_masks_ip_for_non_admin_when_flag_set(self):
        """Non-admin should see masked IP (10.x.x.xxx) when template has mask_ip: true."""
        expires_at = datetime.now() + timedelta(hours=2)
        vm = VMRecord(
            vmid=200, vm_name='student-testuser-target', vm_type='lxc',
            node='pve1', template_name='homework1/target',
            student_name='testuser', is_entry_point=True,
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        db.session.add(vm)
        db.session.commit()

        masked_config = {
            'deployment_groups': [
                {
                    'display_name': 'homework1',
                    'templates': [{'name': 'target', 'mask_ip': True}],
                }
            ]
        }
        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox, \
             patch('app.routes.vm_listing._verify_vm_exists', return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=['10.101.4.56']), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value=masked_config):
            mock_proxmox.return_value = MagicMock()
            response = self.client.get('/list')

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        vms = data['students'][0]['deployment_groups'][0]['vms']
        self.assertEqual(vms[0]['ip_address'], '10.101.4.xxx')

    def test_list_vms_does_not_mask_when_flag_unset(self):
        """Default behavior (no mask_ip flag) shows full IP."""
        expires_at = datetime.now() + timedelta(hours=2)
        vm = VMRecord(
            vmid=201, vm_name='student-testuser-open', vm_type='lxc',
            node='pve1', template_name='homework1/open',
            student_name='testuser', is_entry_point=True,
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        db.session.add(vm)
        db.session.commit()

        unmasked_config = {
            'deployment_groups': [
                {
                    'display_name': 'homework1',
                    'templates': [{'name': 'open'}],
                }
            ]
        }
        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox, \
             patch('app.routes.vm_listing._verify_vm_exists', return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=['10.101.4.56']), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value=unmasked_config):
            mock_proxmox.return_value = MagicMock()
            response = self.client.get('/list')

        data = json.loads(response.data)
        vms = data['students'][0]['deployment_groups'][0]['vms']
        self.assertEqual(vms[0]['ip_address'], '10.101.4.56')

    def test_list_vms_admin_sees_real_ip_even_with_mask_flag(self):
        """Admin always sees the full IP, even when template has mask_ip: true."""
        admin = User(login_id='admin', full_name='Admin', is_admin=True,
                     entry_vlan_tag=102, internal_vlan_tag=202)
        admin.set_password('adminpass')
        db.session.add(admin)
        db.session.commit()

        expires_at = datetime.now() + timedelta(hours=2)
        vm = VMRecord(
            vmid=202, vm_name='student-testuser-target', vm_type='lxc',
            node='pve1', template_name='homework1/target',
            student_name='testuser', is_entry_point=True,
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        db.session.add(vm)
        db.session.commit()

        admin_client = self.app.test_client()
        admin_client.post('/auth/login',
            data=json.dumps({'login_id': 'admin', 'password': 'adminpass'}),
            content_type='application/json')

        masked_config = {
            'deployment_groups': [
                {
                    'display_name': 'homework1',
                    'templates': [{'name': 'target', 'mask_ip': True}],
                }
            ]
        }
        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox, \
             patch('app.routes.vm_listing._verify_vm_exists', return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=['10.101.4.56']), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value=masked_config):
            mock_proxmox.return_value = MagicMock()
            response = admin_client.get('/list')

        data = json.loads(response.data)
        self.assertTrue(data['success'])
        # Admin sees their student bucket containing the testuser VM
        all_vms = [
            v for s in data['students']
            for g in s['deployment_groups']
            for v in g['vms']
        ]
        self.assertEqual(all_vms[0]['ip_address'], '10.101.4.56')

    def test_list_vms_multiple_students(self):
        """Test that users only see their own VMs"""
        # Create another user
        other_user = User(login_id='otheruser', full_name='Other User')
        other_user.set_password('password')
        db.session.add(other_user)
        db.session.commit()
        
        # Create VMs for both users
        expires_at = datetime.now() + timedelta(hours=2)
        my_vm = VMRecord(
            vmid=100, vm_name='student-testuser-vm', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        other_vm = VMRecord(
            vmid=101, vm_name='student-otheruser-vm', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='otheruser',
            expires_at=expires_at, created_by_user_id=other_user.id
        )
        
        db.session.add_all([my_vm, other_vm])
        db.session.commit()

        with patch('app.routes.vm_listing.get_proxmox_connection') as mock_proxmox, \
             patch('app.routes.vm_listing._verify_vm_exists', return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses', return_value=[]):
            mock_proxmox.return_value = MagicMock()
            response = self.client.get('/list')

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])

        # Should only see own VMs
        total_vms = sum(
            group['vm_count']
            for student in data['students']
            for group in student['deployment_groups']
        )
        self.assertEqual(total_vms, 1)
    
    def test_partial_preflight_passes_expected_guest_name_to_cleanup(self):
        template = {
            'name': 'webserver',
            'type': 'qemu',
            'memory': 1024,
            'cores': 1,
            'storage': 20,
        }
        existing = [{
            'vmid': 501,
            'node': 'pve',
            'vm_type': 'qemu',
            'name': 'student-testuser-webserver',
        }]
        config = {
            'deployment_groups': [{
                'display_name': 'homework1',
                'enabled': True,
                'uptime_minutes': 60,
                'templates': [template],
            }],
        }

        with patch(
            'app.routes.vm_creation.load_template_config',
            return_value=config,
        ), patch(
            'app.routes.vm_creation._preflight_deployment_group',
            return_value=('partial', existing),
        ), patch(
            'app.routes.vm_creation.cleanup_vms',
            return_value={501},
        ) as cleanup, patch(
            'app.routes.vm_creation.deploy_bundle',
            return_value=[],
        ), patch(
            'app.routes.vm_creation.get_proxmox_connection',
            return_value=MagicMock(),
        ):
            response = self.client.post(
                '/create', data={'deployment_group': 'homework1'})

        self.assertEqual(response.status_code, 200)
        payload = cleanup.call_args.args[0]
        self.assertEqual(payload[0]['vm_name'],
                         'student-testuser-webserver')

    @patch(
        'app.routes.vm_creation.renew_vmid_reservation',
        new=lambda reservation_id: True,
    )
    @patch(
        'app.routes.vm_creation.renew_node_reservation',
        new=lambda reservation_id: True,
    )
    @patch('app.routes.vm_creation.find_vms_by_name_prefix')
    @patch('app.routes.vm_creation.wait_for_proxmox_task')
    @patch('app.routes.vm_creation.get_proxmox_connection')
    @patch('app.routes.vm_creation.get_next_available_vmid')
    @patch('app.routes.vm_creation.select_best_node')
    def test_create_vm_success(self, mock_select_node, mock_vmid, mock_proxmox, mock_wait_task, mock_find_vms):
        """Test successful VM creation with pre-allocated VMIDs"""
        # Setup mocks
        mock_vmid.return_value = 100  # Base VMID for pre-allocation
        mock_select_node.return_value = 'pve1'  # Node selection
        mock_wait_task.return_value = {'success': True, 'status': 'stopped'}  # Task monitoring
        mock_find_vms.return_value = []  # Pre-flight sweep finds no orphans

        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox

        # Mock nodes list for pre-check
        mock_prox.nodes.get.return_value = [{'node': 'pve1'}]
        mock_prox.nodes().lxc.get.return_value = []
        mock_prox.nodes().qemu.get.return_value = []
        
        with patch('app.routes.vm_creation.load_template_config') as mock_config:
            mock_config.return_value = {
                'deployment_groups': [
                    {
                        'display_name': 'homework1',
                        'enabled': True,
                        'uptime_minutes': 120,
                        'templates': [
                            {
                                'name': 'webserver',
                                'type': 'lxc',
                                'memory': 2048,
                                'cores': 2,
                                'storage': 20,
                                'storage_device': 'local-lvm',
                                'ostemplate': 'local:vztmpl/debian-11-standard_11.3-1_amd64.tar.zst'
                            }
                        ]
                    }
                ]
            }

            response = self.client.post('/create',
                data={'deployment_group': 'homework1'}
            )

            data = json.loads(response.data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data['success'])
            self.assertEqual(data['vm_count'], 1)
    
    def test_extend_deployment_group(self):
        """Test extending deployment group expiration time"""
        # /extend only succeeds when at least one VM has < 60 minutes remaining,
        # so set expiration close to now.
        expires_at = datetime.now() + timedelta(minutes=30)
        vm1 = VMRecord(
            vmid=100, vm_name='student-testuser-vm1', vm_type='lxc',
            node='pve1', template_name='homework1/vm1', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        vm2 = VMRecord(
            vmid=101, vm_name='student-testuser-vm2', vm_type='lxc',
            node='pve1', template_name='homework1/vm2', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )

        db.session.add_all([vm1, vm2])
        db.session.commit()

        original_expiration = expires_at
        
        with patch('app.template_manager.load_template_config') as mock_config, \
             patch('app.routes.vm_lifecycle.get_proxmox_connection') as mock_proxmox:
            mock_config.return_value = {
                'deployment_groups': [
                    {
                        'display_name': 'homework1',
                        'extend_time_minutes': 60
                    }
                ]
            }
            mock_proxmox.return_value = MagicMock()

            response = self.client.post('/extend',
                data={'deployment_group': 'homework1'}
            )

            data = json.loads(response.data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data['success'])

            # Verify expiration was extended
            updated_vm = VMRecord.query.filter_by(vmid=100).first()
            self.assertGreater(updated_vm.expires_at, original_expiration)

    def test_admin_extend_only_named_students_live_vms(self):
        """Admin extension must not cross student or destroyed-record boundaries."""
        admin = User(login_id='adminuser', full_name='Admin User', is_admin=True)
        admin.set_password('adminpass')
        other = User(login_id='otheruser', full_name='Other User')
        other.set_password('otherpass')
        db.session.add_all([admin, other])
        db.session.commit()

        expires_at = datetime.now() + timedelta(minutes=30)
        target = VMRecord(
            vmid=110, vm_name='student-testuser-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        other_student = VMRecord(
            vmid=111, vm_name='student-otheruser-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name='otheruser',
            expires_at=expires_at, created_by_user_id=other.id,
        )
        destroyed = VMRecord(
            vmid=112, vm_name='student-testuser-old-vm', vm_type='qemu',
            node='pve1', template_name='homework1/old-vm',
            student_name='testuser', expires_at=expires_at,
            created_by_user_id=self.test_user.id, destroyed=True,
            destroyed_at=datetime.now(),
        )
        db.session.add_all([target, other_student, destroyed])
        db.session.commit()

        self.client.post(
            '/auth/login',
            data=json.dumps({'login_id': 'adminuser', 'password': 'adminpass'}),
            content_type='application/json',
        )

        config = {
            'deployment_groups': [{
                'display_name': 'homework1',
                'extend_time_minutes': 60,
            }],
        }
        with patch('app.template_manager.load_template_config',
                   return_value=config), \
             patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
            response = self.client.post('/extend', data={
                'deployment_group': 'homework1',
                'student_name': 'testuser',
            })

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(response.get_json()['extended_count'], 1)
        self.assertEqual(db.session.get(VMRecord, target.id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, other_student.id).expires_at,
                         expires_at)
        self.assertEqual(db.session.get(VMRecord, destroyed.id).expires_at,
                         expires_at)
        self.assertEqual(
            proxmox_api.return_value.nodes.return_value.qemu.return_value
            .config.put.call_count,
            1,
        )

    def test_admin_extend_requires_student_name(self):
        admin = User(login_id='adminuser', full_name='Admin User', is_admin=True)
        admin.set_password('adminpass')
        db.session.add(admin)
        db.session.commit()
        self.client.post(
            '/auth/login',
            data=json.dumps({'login_id': 'adminuser', 'password': 'adminpass'}),
            content_type='application/json',
        )

        with patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
            response = self.client.post(
                '/extend', data={'deployment_group': 'homework1'})

        self.assertFalse(response.get_json()['success'])
        self.assertIn('student_name', response.get_json()['error'])
        proxmox_api.assert_not_called()

    def test_student_extend_does_not_touch_another_students_vm(self):
        other = User(login_id='otheruser', full_name='Other User')
        other.set_password('otherpass')
        db.session.add(other)
        db.session.commit()
        expires_at = datetime.now() + timedelta(minutes=30)
        own_vm = VMRecord(
            vmid=113, vm_name='student-testuser-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        other_vm = VMRecord(
            vmid=114, vm_name='student-otheruser-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name='otheruser',
            expires_at=expires_at, created_by_user_id=other.id,
        )
        db.session.add_all([own_vm, other_vm])
        db.session.commit()

        config = {
            'deployment_groups': [{
                'display_name': 'homework1',
                'extend_time_minutes': 60,
            }],
        }
        with patch('app.template_manager.load_template_config',
                   return_value=config), \
             patch('app.proxmox_service.ProxmoxAPI'):
            response = self.client.post(
                '/extend', data={'deployment_group': 'homework1'})

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(db.session.get(VMRecord, own_vm.id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, other_vm.id).expires_at,
                         expires_at)

    def _same_named_deployments(self, group_slug='team1'):
        """One individual and one group deployment of homework1 for testuser."""
        group = StudentGroup(name=group_slug, display_name='Team')
        db.session.add(group)
        db.session.flush()
        db.session.add(StudentGroupMember(group_id=group.id,
                                          user_id=self.test_user.id))
        expires_at = datetime.now() + timedelta(minutes=30)
        individual = VMRecord(
            vmid=120, vm_name='student-testuser-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id,
        )
        group_vm = VMRecord(
            vmid=121, vm_name=f'group-{group_slug}-vm', vm_type='qemu',
            node='pve1', template_name='homework1/vm', student_name=group_slug,
            expires_at=expires_at, created_by_user_id=self.test_user.id,
            owner_group_id=group.id,
        )
        db.session.add_all([individual, group_vm])
        db.session.commit()
        return individual.id, group_vm.id, expires_at

    def _extend(self, **form):
        config = {'deployment_groups': [{
            'display_name': 'homework1', 'extend_time_minutes': 60}]}
        with patch('app.template_manager.load_template_config',
                   return_value=config), \
             patch('app.proxmox_service.ProxmoxAPI'):
            return self.client.post(
                '/extend', data={'deployment_group': 'homework1', **form})

    def test_student_extend_individual_scope_leaves_group_deployment(self):
        individual_id, group_id, expires_at = self._same_named_deployments()

        response = self._extend(deployment_scope='individual')

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(db.session.get(VMRecord, individual_id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, group_id).expires_at,
                         expires_at)

    def test_student_extend_group_scope_leaves_individual_deployment(self):
        individual_id, group_id, expires_at = self._same_named_deployments()

        response = self._extend(deployment_scope='group')

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(db.session.get(VMRecord, group_id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, individual_id).expires_at,
                         expires_at)

    def test_student_extend_without_scope_refuses_ambiguous_name(self):
        """A cached pre-fix page must not extend two deployments at once."""
        individual_id, group_id, expires_at = self._same_named_deployments()

        response = self._extend()

        self.assertFalse(response.get_json()['success'])
        self.assertIn('reload', response.get_json()['error'].lower())
        self.assertEqual(db.session.get(VMRecord, individual_id).expires_at,
                         expires_at)
        self.assertEqual(db.session.get(VMRecord, group_id).expires_at,
                         expires_at)

    def test_admin_extend_scope_separates_student_and_same_named_group(self):
        individual_id, group_id, expires_at = self._same_named_deployments(
            group_slug='testuser')
        admin = User(login_id='adminuser', full_name='Admin User', is_admin=True)
        admin.set_password('adminpass')
        db.session.add(admin)
        db.session.commit()
        self.client.post(
            '/auth/login',
            data=json.dumps({'login_id': 'adminuser', 'password': 'adminpass'}),
            content_type='application/json',
        )

        response = self._extend(student_name='testuser',
                                deployment_scope='group')

        self.assertTrue(response.get_json()['success'])
        self.assertEqual(db.session.get(VMRecord, group_id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, individual_id).expires_at,
                         expires_at)

    def test_list_gives_each_deployment_its_own_scoped_card(self):
        """The Extend button can only name a deployment the list told it."""
        self._same_named_deployments(group_slug='testuser')

        with patch('app.routes.vm_listing.get_proxmox_connection'), \
             patch('app.routes.vm_listing._verify_vm_exists',
                   return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=[]):
            response = self.client.get('/list')

        cards = [
            (card['group_name'], card['deployment_scope'],
             [vm['vmid'] for vm in card['vms']])
            for student in response.get_json()['students']
            for card in student['deployment_groups']
        ]
        self.assertCountEqual(cards, [
            ('homework1', 'individual', [120]),
            ('homework1', 'group', [121]),
        ])

    def _destroy(self, **form):
        with patch('app.routes.vm_lifecycle.get_proxmox_connection',
                   return_value=MagicMock()), \
             patch('app.routes.vm_lifecycle.wait_for_proxmox_task',
                   return_value={'success': True, 'status': 'stopped'}):
            return self.client.post(
                '/destroy', data={'deployment_group': 'homework1', **form})

    def test_student_destroy_individual_scope_leaves_group_deployment(self):
        """The individual card's Destroy must not wipe the team's lab."""
        individual_id, group_id, _ = self._same_named_deployments()

        response = self._destroy(deployment_scope='individual')

        body = response.get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['destroyed_count'], 1)
        self.assertIsNone(db.session.get(VMRecord, individual_id))
        self.assertIsNotNone(db.session.get(VMRecord, group_id))

    def test_student_destroy_group_scope_leaves_individual_deployment(self):
        individual_id, group_id, _ = self._same_named_deployments()

        response = self._destroy(deployment_scope='group')

        body = response.get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['destroyed_count'], 1)
        self.assertIsNone(db.session.get(VMRecord, group_id))
        self.assertIsNotNone(db.session.get(VMRecord, individual_id))

    def test_student_destroy_without_scope_refuses_ambiguous_name(self):
        """A cached pre-fix page must not destroy two deployments at once."""
        individual_id, group_id, _ = self._same_named_deployments()

        with patch('app.routes.vm_lifecycle.get_proxmox_connection') as prox:
            response = self.client.post(
                '/destroy', data={'deployment_group': 'homework1'})

        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertIn('reload', body['error'].lower())
        prox.assert_not_called()
        self.assertIsNotNone(db.session.get(VMRecord, individual_id))
        self.assertIsNotNone(db.session.get(VMRecord, group_id))

    def test_destroy_rejects_unknown_scope(self):
        individual_id, group_id, _ = self._same_named_deployments()

        response = self._destroy(deployment_scope='everything')

        self.assertFalse(response.get_json()['success'])
        self.assertIsNotNone(db.session.get(VMRecord, individual_id))
        self.assertIsNotNone(db.session.get(VMRecord, group_id))

    def _two_groups_with_same_lab(self):
        """testuser deployed homework1 in team1, then moved to team2, which
        also runs homework1.

        Membership is unique per student, but the team1 lab still matches
        the student's query through created_by_user_id, so a group-scoped
        request names two group deployments.
        """
        expires_at = datetime.now() + timedelta(minutes=30)
        ids = []
        for vmid, slug in ((130, 'team1'), (131, 'team2')):
            group = StudentGroup(name=slug, display_name=slug)
            db.session.add(group)
            db.session.flush()
            record = VMRecord(
                vmid=vmid, vm_name=f'group-{slug}-vm', vm_type='qemu',
                node='pve1', template_name='homework1/vm', student_name=slug,
                expires_at=expires_at, created_by_user_id=self.test_user.id,
                owner_group_id=group.id,
            )
            db.session.add(record)
            db.session.flush()
            ids.append((group.id, record.id))
        db.session.add(StudentGroupMember(group_id=ids[1][0],
                                          user_id=self.test_user.id))
        db.session.commit()
        return ids, expires_at

    def test_extend_group_scope_refuses_when_two_groups_share_the_lab(self):
        [(_, first_id), (_, second_id)], expires_at = \
            self._two_groups_with_same_lab()

        response = self._extend(deployment_scope='group')

        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertIn('reload', body['error'].lower())
        self.assertEqual(db.session.get(VMRecord, first_id).expires_at,
                         expires_at)
        self.assertEqual(db.session.get(VMRecord, second_id).expires_at,
                         expires_at)

    def test_extend_group_scope_with_group_id_extends_only_that_group(self):
        [(_, first_id), (second_group, second_id)], expires_at = \
            self._two_groups_with_same_lab()

        response = self._extend(deployment_scope='group',
                                group_id=str(second_group))

        body = response.get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(db.session.get(VMRecord, second_id).expires_at,
                         expires_at + timedelta(minutes=60))
        self.assertEqual(db.session.get(VMRecord, first_id).expires_at,
                         expires_at)

    def test_destroy_group_scope_refuses_when_two_groups_share_the_lab(self):
        [(_, first_id), (_, second_id)], _ = self._two_groups_with_same_lab()

        response = self._destroy(deployment_scope='group')

        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertIn('reload', body['error'].lower())
        self.assertIsNotNone(db.session.get(VMRecord, first_id))
        self.assertIsNotNone(db.session.get(VMRecord, second_id))

    def test_destroy_group_scope_with_group_id_destroys_only_that_group(self):
        [(_, first_id), (second_group, second_id)], _ = \
            self._two_groups_with_same_lab()

        response = self._destroy(deployment_scope='group',
                                 group_id=str(second_group))

        body = response.get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['destroyed_count'], 1)
        self.assertIsNone(db.session.get(VMRecord, second_id))
        self.assertIsNotNone(db.session.get(VMRecord, first_id))

    def test_group_id_of_a_group_the_student_left_is_refused(self):
        [(first_group, first_id), (_, second_id)], _ = \
            self._two_groups_with_same_lab()

        response = self._destroy(deployment_scope='group',
                                 group_id=str(first_group))

        self.assertFalse(response.get_json()['success'])
        self.assertIsNotNone(db.session.get(VMRecord, first_id))
        self.assertIsNotNone(db.session.get(VMRecord, second_id))

    def test_malformed_group_id_is_refused(self):
        [(_, first_id), (_, second_id)], _ = self._two_groups_with_same_lab()

        response = self._destroy(deployment_scope='group', group_id='team1')

        self.assertFalse(response.get_json()['success'])
        self.assertIsNotNone(db.session.get(VMRecord, first_id))
        self.assertIsNotNone(db.session.get(VMRecord, second_id))

    def test_list_card_names_the_group_that_owns_it(self):
        """The buttons can only send the group id the list gave the card."""
        [(first_group, _), (second_group, _)], _ = \
            self._two_groups_with_same_lab()

        with patch('app.routes.vm_listing.get_proxmox_connection'), \
             patch('app.routes.vm_listing._verify_vm_exists',
                   return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=[]):
            response = self.client.get('/list')

        cards = [
            (card['deployment_scope'], card['group_id'],
             [vm['vmid'] for vm in card['vms']])
            for student in response.get_json()['students']
            for card in student['deployment_groups']
        ]
        self.assertCountEqual(cards, [
            ('group', first_group, [130]),
            ('group', second_group, [131]),
        ])
    
    @patch('app.routes.vm_lifecycle.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.routes.vm_lifecycle.get_proxmox_connection')
    def test_destroy_deployment_group(self, mock_proxmox, mock_wait):
        """Test destroying deployment group as student"""
        # Create test VMs
        expires_at = datetime.now() + timedelta(hours=2)
        vm1 = VMRecord(
            vmid=100, vm_name='student-testuser-vm1', vm_type='lxc',
            node='pve1', template_name='homework1/vm1', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        vm2 = VMRecord(
            vmid=101, vm_name='student-testuser-vm2', vm_type='lxc',
            node='pve1', template_name='homework1/vm2', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        
        db.session.add_all([vm1, vm2])
        db.session.commit()
        
        # Mock Proxmox API
        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox
        
        response = self.client.post('/destroy',
            data={'deployment_group': 'homework1'}
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data.get('destroyed_count'), 2)

        # /destroy hard-deletes VMRecord rows after Proxmox deletion succeeds.
        self.assertIsNone(VMRecord.query.filter_by(vmid=100).first())
        self.assertIsNone(VMRecord.query.filter_by(vmid=101).first())

    @patch('app.routes.vm_lifecycle.wait_for_proxmox_task',
           return_value={'success': True, 'status': 'stopped'})
    @patch('app.routes.vm_lifecycle.get_proxmox_connection')
    def test_destroy_deployment_group_admin_with_student_name(self, mock_proxmox, mock_wait):
        """Test admin destroying deployment group with student_name parameter"""
        # Create admin user and login
        admin_user = User(
            login_id='adminuser',
            full_name='Admin User',
            is_admin=True
        )
        admin_user.set_password('adminpass')
        db.session.add(admin_user)
        db.session.commit()
        
        # Login as admin
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'adminuser',
                'password': 'adminpass'
            }),
            content_type='application/json'
        )
        
        # Create test VMs for regular user
        expires_at = datetime.now() + timedelta(hours=2)
        vm1 = VMRecord(
            vmid=100, vm_name='student-testuser-vm1', vm_type='lxc',
            node='pve1', template_name='homework1/vm1', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        vm2 = VMRecord(
            vmid=101, vm_name='student-testuser-vm2', vm_type='lxc',
            node='pve1', template_name='homework1/vm2', student_name='testuser',
            expires_at=expires_at, created_by_user_id=self.test_user.id
        )
        
        db.session.add_all([vm1, vm2])
        db.session.commit()
        
        # Mock Proxmox API
        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox
        
        # Admin must specify student_name
        response = self.client.post('/destroy',
            data={
                'deployment_group': 'homework1',
                'student_name': 'testuser'
            }
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data.get('destroyed_count'), 2)

        # /destroy hard-deletes VMRecord rows after Proxmox deletion succeeds.
        self.assertIsNone(VMRecord.query.filter_by(vmid=100).first())
        self.assertIsNone(VMRecord.query.filter_by(vmid=101).first())

    @patch('app.routes.vm_lifecycle.get_proxmox_connection')
    def test_destroy_admin_without_student_name_fails(self, mock_proxmox):
        """Test that admin must provide student_name parameter"""
        # Create admin user and login
        admin_user = User(
            login_id='adminuser',
            full_name='Admin User',
            is_admin=True
        )
        admin_user.set_password('adminpass')
        db.session.add(admin_user)
        db.session.commit()
        
        # Login as admin
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'adminuser',
                'password': 'adminpass'
            }),
            content_type='application/json'
        )
        
        # Try to destroy without student_name
        response = self.client.post('/destroy',
            data={'deployment_group': 'homework1'}
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertIn('student_name', data['error'].lower())
    
    @patch('app.routes.vm_lifecycle.get_proxmox_connection')
    def test_destroy_student_cannot_destroy_others_vms(self, mock_proxmox):
        """Test that students cannot destroy other students' VMs"""
        # Create another user
        other_user = User(login_id='otheruser', full_name='Other User')
        other_user.set_password('password')
        db.session.add(other_user)
        db.session.commit()
        
        # Create VMs for other user
        expires_at = datetime.now() + timedelta(hours=2)
        vm1 = VMRecord(
            vmid=100, vm_name='student-otheruser-vm1', vm_type='lxc',
            node='pve1', template_name='homework1/vm1', student_name='otheruser',
            expires_at=expires_at, created_by_user_id=other_user.id
        )
        
        db.session.add(vm1)
        db.session.commit()
        
        # Try to destroy as testuser (should fail - no VMs found for testuser)
        response = self.client.post('/destroy',
            data={'deployment_group': 'homework1'}
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertIn('No VMs found', data['error'])
        
        # Verify VM was not destroyed
        vm1_check = VMRecord.query.filter_by(vmid=100).first()
        self.assertFalse(vm1_check.destroyed)


if __name__ == '__main__':
    unittest.main()
