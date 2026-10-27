"""Tests for /create branching on group_mode."""

import json
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from app import create_app
from app.models import (
    db, User, StudentGroup, StudentGroupMember, VMRecord,
)


FAKE_CONFIG = {'deployment_groups': [
    {'display_name': 'Shared Lab', 'enabled': True, 'group_mode': True,
     'uptime_minutes': 60, 'extend_time_minutes': 30,
     'templates': [{'name': 'entry', 'type': 'qemu', 'memory': 2048,
                    'cores': 2, 'storage': 20, 'entry_point': True,
                    'template_locations': {'pve': 9001}}]},
]}


class GroupModeCreationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.alice = User(login_id='alice', full_name='Alice',
                          entry_vlan_tag=101, internal_vlan_tag=201)
        self.alice.set_password('x')
        self.bob = User(login_id='bob', full_name='Bob',
                        entry_vlan_tag=102, internal_vlan_tag=202)
        self.bob.set_password('x')
        self.admin = User(login_id='admin', full_name='Admin', is_admin=True,
                          entry_vlan_tag=103, internal_vlan_tag=203)
        self.admin.set_password('x')
        db.session.add_all([self.alice, self.bob, self.admin])
        db.session.commit()

        self.group = StudentGroup(name='team-a', display_name='Team A',
                                  entry_vlan_tag=300, internal_vlan_tag=400)
        self.other_group = StudentGroup(name='team-b', display_name='Team B',
                                        entry_vlan_tag=301, internal_vlan_tag=401)
        db.session.add_all([self.group, self.other_group])
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=self.group.id, user_id=self.alice.id))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, login_id):
        self.client.post('/auth/login', data={'login_id': login_id, 'password': 'x'})

    def _record_vm(self, owner_group_id=None, created_by_user_id=None, vmid=500):
        now = datetime.now()
        db.session.add(VMRecord(
            vmid=vmid, vm_name='x', vm_type='qemu', node='pve',
            template_name='Shared Lab/entry', student_name='team-a',
            owner_group_id=owner_group_id, created_by_user_id=created_by_user_id,
            created_at=now, expires_at=now + timedelta(hours=1),
        ))
        db.session.commit()

    def test_non_member_cannot_deploy_group_mode(self):
        self._login('bob')
        with patch('app.routes.vm_creation.load_template_config', return_value=FAKE_CONFIG):
            r = self.client.post('/create', data={'deployment_group': 'Shared Lab'})
        body = json.loads(r.data)
        self.assertFalse(body.get('success'))
        self.assertIn('group', body.get('error', '').lower())

    def test_group_slot_already_occupied(self):
        self._record_vm(owner_group_id=self.group.id, created_by_user_id=self.alice.id)
        self._login('alice')
        with patch('app.routes.vm_creation.load_template_config', return_value=FAKE_CONFIG):
            r = self.client.post('/create', data={'deployment_group': 'Shared Lab'})
        body = json.loads(r.data)
        self.assertFalse(body.get('success'))
        self.assertTrue('already' in body.get('error', '').lower()
                        or 'active' in body.get('error', '').lower())

    def test_admin_existing_group_deployment_blocked(self):
        """Admin blocked when target group already has an active deployment."""
        self._record_vm(owner_group_id=self.group.id, created_by_user_id=self.alice.id)
        self._login('admin')
        with patch('app.routes.vm_creation.load_template_config', return_value=FAKE_CONFIG):
            r = self.client.post('/create', data={
                'deployment_group': 'Shared Lab',
                'group_slug': 'team-a',
            })
        body = json.loads(r.data)
        self.assertFalse(body.get('success'))
        self.assertTrue('already' in body.get('error', '').lower()
                        or 'active' in body.get('error', '').lower())

    def test_build_group_vm_name_format(self):
        from app.routes.vm_creation import build_group_vm_name
        self.assertEqual(build_group_vm_name('team-a', 'entry'), 'group-team-a-entry')
        self.assertEqual(build_group_vm_name('Team_A!', 'Win-11'),
                         'group-teama-win-11')


if __name__ == '__main__':
    unittest.main()
