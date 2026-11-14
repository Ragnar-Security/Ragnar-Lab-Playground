"""Tests for /create branching on group_mode."""

import copy
import json
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pytest

from app import create_app
from app.backends import register_backend_factory
from app.backends.proxmox import ProxmoxBackend
from app.models import (
    db, User, StudentGroup, StudentGroupMember, VMIDReservation, VMRecord,
)
from tests.backends.fake_backend import registered_fake_backend
from tests.test_live_contract_guards import FakeProxmox


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


# --- group-mode cloud deploys: the group's tags are the network slots -------

def _cloud_machine(name, entry_point):
    return {'name': name, 'entry_point': entry_point,
            'aws': {'image_id': 'ami-placeholder', 'instance_type': 't3.micro',
                    'disk_gb': 20}}


CLOUD_GROUP_CONFIG = {'deployment_groups': [
    {'display_name': 'Cloud Shared Lab', 'enabled': True, 'group_mode': True,
     'backend': 'aws', 'uptime_minutes': 60,
     'templates': [_cloud_machine('entry', True), _cloud_machine('inner', False)]},
]}


@pytest.fixture
def group_member_client(app):
    """A member of team-a whose own tags differ from the group's."""
    with app.app_context():
        member = User(login_id='cloud-member', entry_vlan_tag=101,
                      internal_vlan_tag=201, session_version=1)
        member.set_password('Correct-Horse-9')
        group = StudentGroup(name='team-a', display_name='Team A',
                             entry_vlan_tag=300, internal_vlan_tag=400)
        db.session.add_all([member, group])
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=group.id, user_id=member.id))
        db.session.commit()
        user_id = member.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['session_version'] = 1
    return client


# Catches a group-mode cloud deploy taking the member's tags instead of the group's.
def test_group_mode_cloud_deploy_uses_the_group_tags_as_slots(app, group_member_client):
    with registered_fake_backend('aws') as fake, patch(
            'app.routes.vm_creation.load_template_config',
            return_value=copy.deepcopy(CLOUD_GROUP_CONFIG)):
        # Proxmox stays enabled, so the allocator reads its (empty) inventory.
        register_backend_factory(
            'proxmox', lambda: ProxmoxBackend(client=FakeProxmox()))
        body = group_member_client.post(
            '/create', data={'deployment_group': 'Cloud Shared Lab'}).get_json()
    assert body['success'] is True, body
    with app.app_context():
        slots = {record.vm_name: (record.backend, record.network_slot)
                 for record in VMRecord.query.all()}
    assert slots == {'group-team-a-entry': ('aws', 300),
                     'group-team-a-inner': ('aws', 400)}
    assert sorted(launched.network_slot for launched in fake.instances.values()) == [300, 400]


# Catches a group without an internal tag reaching allocation on a cloud (I9).
def test_group_mode_cloud_refuses_a_group_without_internal_tag(app, group_member_client):
    with app.app_context():
        StudentGroup.query.filter_by(name='team-a').one().internal_vlan_tag = None
        db.session.commit()
    with registered_fake_backend('aws'), patch(
            'app.routes.vm_creation.load_template_config',
            return_value=copy.deepcopy(CLOUD_GROUP_CONFIG)):
        body = group_member_client.post(
            '/create', data={'deployment_group': 'Cloud Shared Lab'}).get_json()
    assert body == {'success': False,
                    'error': 'Cloud deployment needs internal_vlan_tag assigned. '
                             'Please contact an administrator.'}
    with app.app_context():
        assert VMIDReservation.query.count() == 0


if __name__ == '__main__':
    unittest.main()
