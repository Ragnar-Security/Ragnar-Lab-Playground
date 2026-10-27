"""Tests for user_can_access_vm() helper in app.routes.utils."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from app import create_app
from app.models import db, User, VMRecord, StudentGroup, StudentGroupMember
from app.routes.utils import user_can_access_vm


class AccessControlTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.alice = User(login_id='alice', full_name='Alice')
        self.alice.set_password('x')
        self.bob = User(login_id='bob', full_name='Bob')
        self.bob.set_password('x')
        self.admin = User(login_id='admin', full_name='Admin', is_admin=True)
        self.admin.set_password('x')
        db.session.add_all([self.alice, self.bob, self.admin])
        db.session.commit()

        now = datetime.now()
        self.indiv_vm = VMRecord(
            vmid=100, vm_name='student-alice-vm', vm_type='qemu', node='pve',
            template_name='HW1/vm', student_name='alice',
            created_by_user_id=self.alice.id,
            created_at=now, expires_at=now + timedelta(hours=1),
        )
        self.group = StudentGroup(name='team-a', display_name='A')
        db.session.add_all([self.indiv_vm, self.group])
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=self.group.id, user_id=self.alice.id))
        db.session.commit()
        self.group_vm = VMRecord(
            vmid=101, vm_name='group-team-a-entry', vm_type='qemu', node='pve',
            template_name='Team 1/entry', student_name='team-a',
            created_by_user_id=self.alice.id, owner_group_id=self.group.id,
            created_at=now, expires_at=now + timedelta(hours=1),
        )
        db.session.add(self.group_vm)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_admin_can_access_anything(self):
        self.assertTrue(user_can_access_vm(self.admin, self.indiv_vm))
        self.assertTrue(user_can_access_vm(self.admin, self.group_vm))

    def test_creator_can_access_own_individual_vm(self):
        self.assertTrue(user_can_access_vm(self.alice, self.indiv_vm))

    def test_non_creator_cannot_access_individual_vm(self):
        self.assertFalse(user_can_access_vm(self.bob, self.indiv_vm))

    def test_group_member_can_access_group_vm(self):
        self.assertTrue(user_can_access_vm(self.alice, self.group_vm))

    def test_non_member_cannot_access_group_vm(self):
        self.assertFalse(user_can_access_vm(self.bob, self.group_vm))

    def test_removed_member_loses_access(self):
        StudentGroupMember.query.filter_by(user_id=self.alice.id).delete()
        db.session.commit()
        self.assertFalse(user_can_access_vm(self.alice, self.group_vm))

    def test_koth_vm_accessible_to_team_member(self):
        from app.models import (StudentGroup, StudentGroupMember, KothGame,
                                KothTeam, VMRecord)
        from datetime import datetime, timedelta
        sg = StudentGroup(name='a-koth', display_name='A')
        db.session.add(sg); db.session.commit()
        db.session.add(StudentGroupMember(group_id=sg.id, user_id=self.bob.id))
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551,
                     status='active', is_active=True)
        db.session.add(g); db.session.commit()
        db.session.add(KothTeam(game_id=g.id, student_group_id=sg.id, color='red',
                                submission_token='ttok-koth-access', display_name='A'))
        vm = VMRecord(vmid=999, vm_name='koth', vm_type='qemu', node='pve',
                      template_name='t', student_name='admin',
                      koth_game_id=g.id,
                      expires_at=datetime.now() + timedelta(days=1))
        db.session.add(vm); db.session.commit()
        from app.routes.utils import user_can_access_vm
        self.assertTrue(user_can_access_vm(self.bob, vm))

    def test_koth_vm_not_accessible_to_non_member(self):
        from app.models import KothGame, VMRecord
        from datetime import datetime, timedelta
        g = KothGame(name='g2', deployment_group_name='x',
                     entry_vlan_tag=560, internal_vlan_tag=561,
                     status='active', is_active=True)
        db.session.add(g); db.session.commit()
        vm = VMRecord(vmid=998, vm_name='koth2', vm_type='qemu', node='pve',
                      template_name='t', student_name='admin',
                      koth_game_id=g.id,
                      expires_at=datetime.now() + timedelta(days=1))
        db.session.add(vm); db.session.commit()
        from app.routes.utils import user_can_access_vm
        from app.models import User
        from werkzeug.security import generate_password_hash
        outsider = User(login_id='outsider-koth',
                        password_hash=generate_password_hash('p'))
        db.session.add(outsider); db.session.commit()
        self.assertFalse(user_can_access_vm(outsider, vm))


class RouteAccessTestCase(unittest.TestCase):
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
        db.session.add_all([self.alice, self.bob])
        db.session.commit()
        now = datetime.now()
        self.group = StudentGroup(name='team-a', display_name='A',
                                  entry_vlan_tag=300, internal_vlan_tag=400)
        db.session.add(self.group)
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=self.group.id, user_id=self.alice.id))
        db.session.commit()
        self.group_vm = VMRecord(
            vmid=900, vm_name='group-team-a-entry', vm_type='qemu', node='pve',
            template_name='Shared/entry', student_name='team-a',
            owner_group_id=self.group.id, created_by_user_id=self.alice.id,
            created_at=now, expires_at=now + timedelta(hours=1),
        )
        db.session.add(self.group_vm)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, login_id):
        self.client.post('/auth/login', data={'login_id': login_id, 'password': 'x'})

    def test_list_includes_group_vm_for_member(self):
        self._login('alice')
        r = self.client.get('/api/vms')
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        # Response shape varies; locate the VMs list:
        vms = payload.get('vms') or payload.get('virtual_machines') or []
        vmids = [vm.get('vmid') for vm in vms]
        self.assertIn(900, vmids)

    def test_list_excludes_group_vm_for_non_member(self):
        self._login('bob')
        r = self.client.get('/api/vms')
        payload = r.get_json()
        vms = payload.get('vms') or payload.get('virtual_machines') or []
        vmids = [vm.get('vmid') for vm in vms]
        self.assertNotIn(900, vmids)

    def test_group_member_can_view_group_vm_detail(self):
        group_vm = VMRecord(
            vmid=901, vm_name='group-team-a-member-vm', vm_type='qemu',
            node='pve', template_name='Shared/member-vm',
            student_name='team-a', owner_group_id=self.group.id,
            created_by_user_id=self.bob.id, created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )
        db.session.add(group_vm)
        db.session.commit()
        proxmox = MagicMock()
        proxmox.nodes.get.return_value = [{'node': 'pve'}]
        node_api = proxmox.nodes.return_value
        node_api.qemu.get.return_value = [{
            'vmid': 901,
            'name': 'group-team-a-member-vm',
        }]
        node_api.qemu.return_value.config.get.return_value = {
            'memory': 2048,
            'cores': 2,
        }
        node_api.qemu.return_value.status.current.get.return_value = {
            'status': 'running',
            'uptime': 60,
        }

        self._login('alice')
        with patch('app.proxmox_service.ProxmoxAPI', return_value=proxmox):
            response = self.client.get('/api/vms/901')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(response.get_json()['vm']['vmid'], 901)

    def test_student_cannot_view_another_students_vm_detail(self):
        individual_vm = VMRecord(
            vmid=902, vm_name='student-bob-vm', vm_type='qemu',
            node='pve', template_name='Individual/vm', student_name='bob',
            created_by_user_id=self.bob.id, created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )
        db.session.add(individual_vm)
        db.session.commit()

        self._login('alice')
        with patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
            response = self.client.get('/api/vms/902')

        self.assertEqual(response.status_code, 403)
        proxmox_api.return_value.nodes.get.assert_not_called()

    def test_anonymous_time_remaining_is_rejected_without_proxmox_call(self):
        with patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
            response = self.client.get('/time-remaining?vm_id=900')

        self.assertEqual(response.status_code, 401)
        proxmox_api.assert_not_called()

    def test_time_remaining_rejects_vm_owned_by_another_student(self):
        individual_vm = VMRecord(
            vmid=903, vm_name='student-bob-vm', vm_type='qemu',
            node='pve', template_name='Individual/vm', student_name='bob',
            created_by_user_id=self.bob.id, created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )
        db.session.add(individual_vm)
        db.session.commit()

        self._login('alice')
        with patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
            response = self.client.get('/time-remaining?vm_id=903')

        self.assertEqual(response.status_code, 403)
        proxmox_api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
