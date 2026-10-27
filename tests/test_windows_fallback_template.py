"""Tests for a Windows template's platform support: linked clones,
single-NIC entry points, and the connect block."""

import json
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from app import create_app
from app.models import db, User, VMRecord
from app.routes.vm_creation import extract_template_config, create_qemu_vm
from app.routes.utils import VM_PRESENT


class TestFullCloneOption(unittest.TestCase):
    def test_full_clone_defaults_to_true(self):
        """Existing templates have no full_clone key and must keep full clones."""
        cfg = extract_template_config({'name': 'legacy', 'type': 'qemu'})
        self.assertTrue(cfg['full_clone'])

    def test_full_clone_can_be_disabled(self):
        cfg = extract_template_config({'name': 'win', 'type': 'qemu', 'full_clone': False})
        self.assertFalse(cfg['full_clone'])

    def _clone_kwargs(self, full_clone, storage_locations=None):
        proxmox = MagicMock()
        cfg = {
            'memory': 6144, 'cores': 4, 'static_mac': None,
            'full_clone': full_clone, 'internal_nic': True,
        }
        if storage_locations is not None:
            cfg['storage_locations'] = storage_locations
        with patch('app.routes.vm_creation.wait_for_proxmox_task',
                   return_value={'success': True}):
            create_qemu_vm(proxmox, 'pve', 'pve', 9001, 'student-x-win', 9696,
                           cfg, datetime.utcnow() + timedelta(hours=4),
                           internal_vlan_tag=201, entry_vlan_tag=101)
        return proxmox.nodes.return_value.qemu.return_value.clone.post.call_args.kwargs

    def test_linked_clone_passes_full_zero(self):
        self.assertEqual(self._clone_kwargs(False)['full'], 0)

    def test_full_clone_passes_full_one(self):
        self.assertEqual(self._clone_kwargs(True)['full'], 1)

    def test_full_clone_targets_node_specific_storage(self):
        kwargs = self._clone_kwargs(True, {'pve': 'pve-vm-storage-zfs2', 'pve2': 'other'})
        self.assertEqual(kwargs['storage'], 'pve-vm-storage-zfs2')

    def test_linked_clone_never_sends_storage(self):
        """Proxmox accepts a target storage only for full clones."""
        kwargs = self._clone_kwargs(False, {'pve': 'pve-vm-storage-zfs2'})
        self.assertNotIn('storage', kwargs)

    def test_full_clone_without_node_storage_keeps_source_storage(self):
        """No configured storage for the node: let Proxmox use the template's
        own storage instead of a default pool that may not exist there."""
        self.assertNotIn('storage', self._clone_kwargs(True))
        self.assertNotIn('storage', self._clone_kwargs(True, {'pve2': 'other'}))


class TestInternalNicOption(unittest.TestCase):
    def test_internal_nic_defaults_to_true(self):
        cfg = extract_template_config({'name': 'legacy', 'type': 'qemu'})
        self.assertTrue(cfg['internal_nic'])

    def _net_calls(self, internal_nic):
        proxmox = MagicMock()
        cfg = {'memory': 6144, 'cores': 4, 'static_mac': None,
               'full_clone': False, 'internal_nic': internal_nic}
        with patch('app.routes.vm_creation.wait_for_proxmox_task',
                   return_value={'success': True}):
            create_qemu_vm(proxmox, 'pve', 'pve', 9001, 'student-x-win', 9696,
                           cfg, datetime.utcnow() + timedelta(hours=4),
                           internal_vlan_tag=201, entry_vlan_tag=101)
        put = proxmox.nodes.return_value.qemu.return_value.config.put
        return [c.kwargs for c in put.call_args_list]

    def test_entry_point_with_internal_nic_gets_both(self):
        kwargs = self._net_calls(True)
        self.assertTrue(any('net0' in k for k in kwargs))
        self.assertTrue(any('net1' in k for k in kwargs))

    def test_entry_point_without_internal_nic_gets_only_net0(self):
        """A single-VM Windows lab must not get a second DHCP lease,
        a second firewall profile, or an ambiguous second IP in the UI."""
        kwargs = self._net_calls(False)
        net0 = [k for k in kwargs if 'net0' in k]
        self.assertEqual(len(net0), 1)
        self.assertIn('tag=101', net0[0]['net0'])
        self.assertFalse(any('net1' in k and k.get('net1') for k in kwargs))
        self.assertTrue(any(k.get('delete') == 'net1' for k in kwargs))


class TestConnectBlock(unittest.TestCase):
    def test_connect_defaults_to_none(self):
        cfg = extract_template_config({'name': 'legacy', 'type': 'qemu'})
        self.assertIsNone(cfg['connect'])

    def test_connect_is_passed_through(self):
        cfg = extract_template_config({
            'name': 'win', 'type': 'qemu',
            'connect': {'protocol': 'rdp', 'port': 3389, 'username': 'student'}})
        self.assertEqual(cfg['connect']['protocol'], 'rdp')
        self.assertEqual(cfg['connect']['port'], 3389)
        self.assertEqual(cfg['connect']['username'], 'student')


CONNECT_CONFIG = {'deployment_groups': [{
    'display_name': 'lab', 'enabled': True,
    'templates': [
        {'name': 'win', 'type': 'qemu', 'entry_point': True,
         'connect': {'protocol': 'rdp', 'port': 3390, 'username': 'student'}},
        {'name': 'legacy', 'type': 'qemu', 'entry_point': True},
    ]}]}


class TestConnectOnApi(unittest.TestCase):
    """The browser reads connect from /templates and from /list."""

    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = User(login_id='u', full_name='U',
                         entry_vlan_tag=101, internal_vlan_tag=201)
        self.user.set_password('x')
        db.session.add(self.user)
        db.session.commit()
        self.client.post('/auth/login', data={'login_id': 'u', 'password': 'x'})

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_templates_endpoint_exposes_connect(self):
        with patch('app.routes.templates.load_template_config',
                   return_value=CONNECT_CONFIG):
            payload = json.loads(self.client.get('/templates').data)
        by_name = {t['name']: t for t in payload['deployment_groups'][0]['templates']}
        self.assertEqual(by_name['win']['connect']['protocol'], 'rdp')
        self.assertEqual(by_name['win']['connect']['port'], 3390)
        self.assertIsNone(by_name['legacy']['connect'])

    def test_list_endpoint_exposes_connect_per_vm(self):
        for vmid, name in ((300, 'win'), (301, 'legacy')):
            db.session.add(VMRecord(
                vmid=vmid, vm_name=f'student-u-{name}', vm_type='qemu',
                node='pve1', template_name=f'lab/{name}', student_name='u',
                is_entry_point=True,
                expires_at=datetime.now() + timedelta(hours=2),
                created_by_user_id=self.user.id))
        db.session.commit()
        # _verify_vm_exists returns (state, error_msg). It used to return a
        # three-tuple, and this mock still fed one, so /list answered
        # {"success": false, "error": "too many values to unpack (expected 2)"}
        # and the assertion below died on a missing 'students' key. Use the
        # real constant rather than a bare truthy value, so this breaks loudly
        # if the contract changes again.
        with patch('app.routes.vm_listing.get_proxmox_connection'), \
             patch('app.routes.vm_listing._verify_vm_exists',
                   return_value=(VM_PRESENT, None)), \
             patch('app.routes.vm_listing._get_vm_ip_addresses',
                   return_value=['10.101.4.56']), \
             patch('app.routes.vm_listing.load_template_config',
                   return_value=CONNECT_CONFIG):
            data = json.loads(self.client.get('/list').data)
        self.assertTrue(data.get('success'), data)
        vms = data['students'][0]['deployment_groups'][0]['vms']
        by_name = {v['name']: v for v in vms}
        self.assertEqual(by_name['student-u-win']['connect']['username'], 'student')
        self.assertEqual(by_name['student-u-win']['connect']['port'], 3390)
        self.assertIsNone(by_name['student-u-legacy']['connect'])


class TestLegacyTemplateIsUnchanged(unittest.TestCase):
    """Older templates carry neither new key and must retain their behaviour."""

    def test_legacy_config_gets_full_clone_and_both_nics(self):
        proxmox = MagicMock()
        cfg = extract_template_config({'name': 'legacy', 'type': 'qemu'})
        for key in ('full_clone', 'internal_nic', 'connect'):
            cfg.pop(key)
        with patch('app.routes.vm_creation.wait_for_proxmox_task',
                   return_value={'success': True}):
            create_qemu_vm(proxmox, 'pve', 'pve', 9001, 'student-x-legacy', 9000,
                           cfg, datetime.utcnow() + timedelta(hours=4),
                           internal_vlan_tag=201, entry_vlan_tag=101)
        vm = proxmox.nodes.return_value.qemu.return_value
        self.assertEqual(vm.clone.post.call_args.kwargs['full'], 1)
        puts = [c.kwargs for c in vm.config.put.call_args_list]
        self.assertTrue(any('tag=101' in k.get('net0', '') for k in puts))
        self.assertTrue(any('tag=201' in k.get('net1', '') for k in puts))
        self.assertFalse(any(k.get('delete') == 'net1' for k in puts))


if __name__ == '__main__':
    unittest.main()
