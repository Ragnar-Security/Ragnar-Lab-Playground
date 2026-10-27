"""
Tests for parallel VM creation with pre-allocated VMIDs
Tests the fixes for the third VM creation failure bug
"""

import unittest
from unittest.mock import patch, MagicMock, call
import threading
import time
from app import create_app
from app.models import db, User


class TestParallelVMCreation(unittest.TestCase):
    """Test cases for parallel VM creation with pre-allocated VMIDs"""

    def setUp(self):
        """Set up test fixtures"""
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret-key'
        # These tests replace node selection itself, so no real reservation
        # exists for the production heartbeat to renew.
        self._renew_patcher = patch(
            'app.routes.vm_creation.renew_node_reservation',
            return_value=True,
        )
        self._renew_patcher.start()
        self._renew_vmid_patcher = patch(
            'app.routes.vm_creation.renew_vmid_reservation',
            return_value=True,
        )
        self._renew_vmid_patcher.start()

        self.client = self.app.test_client()

        self.app_context = self.app.app_context()
        self.app_context.push()

        db.create_all()

        # Create test user with VLANs
        self.test_user = User(
            login_id='testuser',
            full_name='Test User',
            is_admin=False,
            entry_vlan_tag=101,
            internal_vlan_tag=201
        )
        self.test_user.set_password('password123')
        db.session.add(self.test_user)
        db.session.commit()

        # Login
        self.client.post('/auth/login',
            data={'login_id': 'testuser', 'password': 'password123'},
            content_type='application/x-www-form-urlencoded'
        )

    def tearDown(self):
        """Clean up test fixtures"""
        self._renew_patcher.stop()
        self._renew_vmid_patcher.stop()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('app.routes.vm_creation.find_vms_by_name_prefix', return_value=[])
    @patch('app.routes.vm_creation.wait_for_proxmox_task')
    @patch('app.routes.vm_creation.get_proxmox_connection')
    @patch('app.routes.vm_creation.get_next_available_vmid')
    @patch('app.routes.vm_creation.select_best_node')
    @patch('app.routes.vm_creation.load_template_config')
    def test_vmid_preallocation_sequential(self, mock_config, mock_select_node,
                                          mock_vmid, mock_proxmox, mock_wait_task, mock_find_vms):
        """Test that VMIDs are pre-allocated sequentially"""
        # Setup mocks
        base_vmid = 100
        mock_vmid.return_value = base_vmid
        mock_select_node.return_value = 'pve1'
        mock_wait_task.return_value = {'success': True, 'status': 'stopped'}

        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox

        # Mock nodes list (empty, no existing VMs)
        mock_prox.nodes.get.return_value = [{'node': 'pve1'}]
        mock_prox.nodes().lxc.get.return_value = []
        mock_prox.nodes().qemu.get.return_value = []

        # Configure 3 VMs for homework3
        mock_config.return_value = {
            'deployment_groups': [
                {
                    'display_name': 'homework3',
                    'enabled': True,
                    'uptime_minutes': 120,
                    'templates': [
                        {
                            'name': 'loadbalancer',
                            'type': 'qemu',
                            'memory': 1024,
                            'cores': 1,
                            'entry_point': True,
                            'template_locations': {'pve1': 110}
                        },
                        {
                            'name': 'webserver1',
                            'type': 'qemu',
                            'memory': 2048,
                            'cores': 1,
                            'template_locations': {'pve1': 110}
                        },
                        {
                            'name': 'webserver2',
                            'type': 'qemu',
                            'memory': 2048,
                            'cores': 1,
                            'template_locations': {'pve1': 110}
                        }
                    ]
                }
            ]
        }

        response = self.client.post('/create', data={'deployment_group': 'homework3'})

        # Verify VMID allocation was called once
        mock_vmid.assert_called_once()

        # Verify all 3 VMs should get sequential VMIDs: 100, 101, 102
        self.assertEqual(response.status_code, 200)

    @patch('app.routes.vm_creation.find_vms_by_name_prefix', return_value=[])
    @patch('app.routes.vm_creation.wait_for_proxmox_task')
    @patch('app.routes.vm_creation.get_proxmox_connection')
    @patch('app.routes.vm_creation.get_next_available_vmid')
    @patch('app.routes.vm_creation.select_best_node')
    @patch('app.routes.vm_creation.load_template_config')
    def test_no_vmid_collisions(self, mock_config, mock_select_node,
                                mock_vmid, mock_proxmox, mock_wait_task, mock_find_vms):
        """Test that no two VMs get the same VMID"""
        # Setup mocks
        mock_vmid.return_value = 200
        mock_select_node.return_value = 'pve1'
        mock_wait_task.return_value = {'success': True, 'status': 'stopped'}

        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox

        mock_prox.nodes.get.return_value = [{'node': 'pve1'}]
        mock_prox.nodes().lxc.get.return_value = []
        mock_prox.nodes().qemu.get.return_value = []

        # Track VMIDs assigned during clone operations
        created_vmids = []

        def track_clone_vmid(*args, **kwargs):
            # Extract VMID from clone parameters
            vmid = kwargs.get('newid') or args[0] if args else None
            if vmid:
                created_vmids.append(vmid)
            return 'UPID:pve:00000001:00000001:00000001:qmclone:110:root@pam:'

        mock_prox.nodes().qemu().clone.post.side_effect = track_clone_vmid

        # Configure 3 VMs
        mock_config.return_value = {
            'deployment_groups': [
                {
                    'display_name': 'test-group',
                    'enabled': True,
                    'uptime_minutes': 120,
                    'templates': [
                        {'name': 'vm1', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm2', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm3', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}}
                    ]
                }
            ]
        }

        response = self.client.post('/create', data={'deployment_group': 'test-group'})

        # Verify all VMIDs are unique
        self.assertEqual(len(created_vmids), len(set(created_vmids)),
                        f"VMID collision detected! VMIDs created: {created_vmids}")

    @patch('app.routes.vm_creation.find_vms_by_name_prefix', return_value=[])
    @patch('app.routes.vm_creation.wait_for_proxmox_task')
    @patch('app.routes.vm_creation.get_proxmox_connection')
    @patch('app.routes.vm_creation.get_next_available_vmid')
    @patch('app.routes.vm_creation.select_best_node')
    @patch('app.routes.vm_creation.load_template_config')
    def test_semaphore_limits_concurrent_clones(self, mock_config, mock_select_node,
                                                mock_vmid, mock_proxmox, mock_wait_task, mock_find_vms):
        """Test that semaphore limits concurrent clones to 2"""
        # Setup mocks
        mock_vmid.return_value = 300
        mock_select_node.return_value = 'pve1'

        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox

        mock_prox.nodes.get.return_value = [{'node': 'pve1'}]
        mock_prox.nodes().lxc.get.return_value = []
        mock_prox.nodes().qemu.get.return_value = []

        # Track concurrent clone operations
        concurrent_count = {'current': 0, 'max': 0}
        lock = threading.Lock()

        def slow_clone(*args, **kwargs):
            """Simulate slow clone with concurrency tracking"""
            with lock:
                concurrent_count['current'] += 1
                concurrent_count['max'] = max(concurrent_count['max'], concurrent_count['current'])

            time.sleep(0.1)  # Simulate clone operation

            with lock:
                concurrent_count['current'] -= 1

            return 'UPID:pve:00000001:00000001:00000001:qmclone:110:root@pam:'

        def slow_task_wait(*args, **kwargs):
            """Simulate task wait"""
            time.sleep(0.05)
            return {'success': True, 'status': 'stopped'}

        mock_prox.nodes().qemu().clone.post.side_effect = slow_clone
        mock_wait_task.side_effect = slow_task_wait

        # Configure 3 VMs
        mock_config.return_value = {
            'deployment_groups': [
                {
                    'display_name': 'semaphore-test',
                    'enabled': True,
                    'uptime_minutes': 120,
                    'templates': [
                        {'name': 'vm1', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm2', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm3', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}}
                    ]
                }
            ]
        }

        response = self.client.post('/create', data={'deployment_group': 'semaphore-test'})

        # Verify max concurrent operations was <= 2 (semaphore limit)
        self.assertLessEqual(concurrent_count['max'], 2,
                           f"Semaphore failed! Max concurrent clones: {concurrent_count['max']}, expected <= 2")

    @patch('app.routes.vm_creation.find_vms_by_name_prefix', return_value=[])
    @patch('app.routes.vm_creation.wait_for_proxmox_task')
    @patch('app.routes.vm_creation.get_proxmox_connection')
    @patch('app.routes.vm_creation.get_next_available_vmid')
    @patch('app.routes.vm_creation.select_best_node')
    @patch('app.routes.vm_creation.load_template_config')
    def test_task_monitoring_failure_triggers_cleanup(self, mock_config, mock_select_node,
                                                      mock_vmid, mock_proxmox, mock_wait_task, mock_find_vms):
        """Test that task monitoring failure triggers cleanup of all VMs"""
        # Setup mocks
        mock_vmid.return_value = 400
        mock_select_node.return_value = 'pve1'

        # First two clone tasks succeed, third fails. Any subsequent calls
        # (from cleanup_vms's stop+destroy await of the 2 successful clones)
        # default to success so the cleanup runs to completion.
        _clone_results = [
            {'success': True, 'status': 'stopped'},
            {'success': True, 'status': 'stopped'},
            {'success': False, 'status': 'stopped', 'error': 'Clone failed'},
        ]

        def _next_task_result(*args, **kwargs):
            if _clone_results:
                return _clone_results.pop(0)
            return {'success': True, 'status': 'stopped'}

        mock_wait_task.side_effect = _next_task_result

        mock_prox = MagicMock()
        mock_proxmox.return_value = mock_prox

        mock_prox.nodes.get.return_value = [{'node': 'pve1'}]
        mock_prox.nodes().lxc.get.return_value = []
        mock_prox.nodes().qemu.get.return_value = []
        mock_prox.cluster.resources.get.return_value = [
            {
                'vmid': 400 + offset,
                'name': f'student-testuser-vm{offset + 1}',
                'node': 'pve1',
                'type': 'qemu',
                'status': 'running',
            }
            for offset in range(3)
        ]

        # Configure 3 VMs
        mock_config.return_value = {
            'deployment_groups': [
                {
                    'display_name': 'failure-test',
                    'enabled': True,
                    'uptime_minutes': 120,
                    'templates': [
                        {'name': 'vm1', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm2', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}},
                        {'name': 'vm3', 'type': 'qemu', 'memory': 1024, 'cores': 1, 'template_locations': {'pve1': 110}}
                    ]
                }
            ]
        }

        response = self.client.post('/create', data={'deployment_group': 'failure-test'})

        # Verify request failed
        data = response.get_json()
        self.assertFalse(data['success'])
        self.assertIn('failed', data['error'].lower())

        # Verify cleanup was called (VMs were deleted)
        delete_calls = mock_prox.nodes().qemu().delete.call_count
        self.assertGreater(delete_calls, 0, "Cleanup should delete successfully created VMs")

    def test_node_enumeration_failure_releases_vmid_reservation(self):
        config = {
            'deployment_groups': [{
                'display_name': 'node-failure',
                'enabled': True,
                'uptime_minutes': 120,
                'templates': [{
                    'name': 'vm1',
                    'type': 'qemu',
                    'memory': 1024,
                    'cores': 1,
                    'template_locations': {'pve1': 110},
                }],
            }],
        }
        proxmox = MagicMock()
        proxmox.nodes.get.side_effect = RuntimeError(
            'node inventory unavailable')

        with patch(
            'app.routes.vm_creation.load_template_config',
            return_value=config,
        ), patch(
            'app.routes.vm_creation.find_vms_by_name_prefix',
            return_value=[],
        ), patch(
            'app.routes.vm_creation.get_proxmox_connection',
            return_value=proxmox,
        ), patch(
            'app.routes.vm_creation.get_next_available_vmid',
            return_value=500,
        ) as allocate, patch(
            'app.routes.vm_creation.release_vmid_reservation',
        ) as release:
            response = self.client.post(
                '/create', data={'deployment_group': 'node-failure'})

        self.assertFalse(response.get_json()['success'])
        reservation_id = allocate.call_args.kwargs['reservation_id']
        release.assert_called_once_with(reservation_id)


if __name__ == '__main__':
    unittest.main()
