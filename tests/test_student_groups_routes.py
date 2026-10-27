"""Tests for /admin/groups routes."""

import io
import json
import unittest
from app import create_app
from app.models import db, User, StudentGroup


def _mk_user(login_id, password='pw', is_admin=False):
    u = User(login_id=login_id, full_name=login_id.title(), is_admin=is_admin)
    u.set_password(password)
    return u


class GroupsRoutesBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin = _mk_user('adminuser', 'pw', is_admin=True)
        self.student = _mk_user('studentuser', 'pw')
        db.session.add_all([self.admin, self.student])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self, login_id, password='pw'):
        return self.client.post('/auth/login', data={
            'login_id': login_id, 'password': password,
        })


class ListAndCreateTestCase(GroupsRoutesBaseTestCase):
    def test_non_admin_forbidden(self):
        self.login('studentuser')
        r = self.client.get('/admin/groups')
        self.assertIn(r.status_code, (401, 403))

    def test_list_empty(self):
        self.login('adminuser')
        r = self.client.get('/admin/groups')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.data), {'groups': []})

    def test_create_auto_allocates_vlan(self):
        self.login('adminuser')
        r = self.client.post('/admin/groups',
                             json={'name': 'team-alpha', 'display_name': 'Team Alpha'})
        self.assertEqual(r.status_code, 201, r.data)
        body = json.loads(r.data)
        self.assertEqual(body['group']['name'], 'team-alpha')
        self.assertEqual(body['group']['entry_vlan_tag'], 300)
        self.assertEqual(body['group']['internal_vlan_tag'], 400)

    def test_create_accepts_explicit_vlan(self):
        self.login('adminuser')
        r = self.client.post('/admin/groups', json={
            'name': 't', 'display_name': 'T',
            'entry_vlan_tag': 350, 'internal_vlan_tag': 450,
        })
        self.assertEqual(r.status_code, 201, r.data)
        body = json.loads(r.data)
        self.assertEqual(body['group']['entry_vlan_tag'], 350)
        self.assertEqual(body['group']['internal_vlan_tag'], 450)

    def test_create_rejects_bad_slug(self):
        self.login('adminuser')
        r = self.client.post('/admin/groups',
                             json={'name': 'Bad Name!', 'display_name': 'X'})
        self.assertEqual(r.status_code, 400)

    def test_create_rejects_duplicate_slug(self):
        db.session.add(StudentGroup(name='dup', display_name='Dup'))
        db.session.commit()
        self.login('adminuser')
        r = self.client.post('/admin/groups',
                             json={'name': 'dup', 'display_name': 'X'})
        self.assertEqual(r.status_code, 409)


class DetailUpdateDeleteTestCase(GroupsRoutesBaseTestCase):
    def _create(self, **overrides):
        payload = {'name': 'team-a', 'display_name': 'Team A'}
        payload.update(overrides)
        self.login('adminuser')
        r = self.client.post('/admin/groups', json=payload)
        self.assertEqual(r.status_code, 201, r.data)
        return json.loads(r.data)['group']

    def test_get_detail(self):
        g = self._create()
        r = self.client.get(f"/admin/groups/{g['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.data)['group']['name'], 'team-a')

    def test_update_display_name_and_description(self):
        g = self._create()
        r = self.client.put(f"/admin/groups/{g['id']}", json={
            'display_name': 'New Name', 'description': 'desc',
        })
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.data)['group']
        self.assertEqual(body['display_name'], 'New Name')
        self.assertEqual(body['description'], 'desc')

    def test_update_rejects_slug_change(self):
        g = self._create()
        self.client.put(f"/admin/groups/{g['id']}", json={'name': 'renamed'})
        r2 = self.client.get(f"/admin/groups/{g['id']}")
        self.assertEqual(json.loads(r2.data)['group']['name'], 'team-a')

    def test_update_vlan_when_no_active_vms(self):
        g = self._create()
        r = self.client.put(f"/admin/groups/{g['id']}", json={
            'entry_vlan_tag': 355, 'internal_vlan_tag': 455,
        })
        self.assertEqual(r.status_code, 200, r.data)
        body = json.loads(r.data)['group']
        self.assertEqual((body['entry_vlan_tag'], body['internal_vlan_tag']), (355, 455))

    def test_update_vlan_blocked_with_active_vms(self):
        from app.models import VMRecord
        from datetime import datetime, timedelta
        g = self._create()
        now = datetime.now()
        db.session.add(VMRecord(vmid=500, vm_name='x', vm_type='qemu', node='pve',
                                template_name='t', student_name='team-a',
                                owner_group_id=g['id'],
                                created_at=now, expires_at=now + timedelta(hours=1)))
        db.session.commit()
        r = self.client.put(f"/admin/groups/{g['id']}", json={
            'entry_vlan_tag': 355, 'internal_vlan_tag': 455,
        })
        self.assertEqual(r.status_code, 409)

    def test_delete_with_no_active_vms(self):
        from unittest.mock import patch
        g = self._create()
        # delete_group now always runs the Proxmox name-prefix sweep (so an
        # orphaned VM with no VMRecord still gets caught). Mock it to return
        # nothing so the test doesn't hit a real Proxmox connection.
        with patch('app.routes.student_groups.find_vms_by_name_prefix',
                   return_value=[]):
            r = self.client.delete(f"/admin/groups/{g['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(db.session.get(StudentGroup, g['id']))

    def test_delete_group_does_not_sweep_hyphenated_slug_neighbour(self):
        from unittest.mock import patch

        group = self._create(name='team', display_name='Team')
        inventory = [
            {'vmid': 501, 'node': 'node1', 'vm_type': 'qemu',
             'name': 'group-team-windows-workstation'},
            {'vmid': 502, 'node': 'node1', 'vm_type': 'qemu',
             'name': 'group-team-a-windows-workstation'},
        ]
        swept = []

        def cleanup(payload):
            swept.extend(payload)
            return {item['vmid'] for item in payload}

        with patch('app.routes.student_groups.find_vms_by_name_prefix',
                   return_value=inventory), \
             patch('app.routes.student_groups.cleanup_vms',
                   side_effect=cleanup):
            response = self.client.delete(
                f"/admin/groups/{group['id']}?confirm_destroy=1")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([vm['vmid'] for vm in swept], [501])

    def test_delete_blocked_without_confirm_when_active_vms(self):
        from unittest.mock import patch
        from app.models import VMRecord
        from datetime import datetime, timedelta
        g = self._create()
        now = datetime.now()
        db.session.add(VMRecord(vmid=500, vm_name='x', vm_type='qemu', node='pve',
                                template_name='t', student_name='team-a',
                                owner_group_id=g['id'],
                                created_at=now, expires_at=now + timedelta(hours=1)))
        db.session.commit()

        # Mock the Proxmox sweep across both DELETE calls below.
        with patch('app.routes.student_groups.find_vms_by_name_prefix',
                   return_value=[]):
            r = self.client.delete(f"/admin/groups/{g['id']}")
            self.assertEqual(r.status_code, 409)

            def fake_cleanup(items):
                destroyed = set()
                for item in items:
                    vm = VMRecord.query.filter_by(id=item['db_record_id']).first()
                    if vm:
                        vm.destroyed = True
                        vm.destroyed_at = datetime.now()
                    destroyed.add(item['vmid'])
                db.session.commit()
                return destroyed

            with patch('app.routes.student_groups.cleanup_vms', side_effect=fake_cleanup):
                r2 = self.client.delete(f"/admin/groups/{g['id']}?confirm_destroy=1")

        self.assertEqual(r2.status_code, 200)
        self.assertIsNone(db.session.get(StudentGroup, g['id']))
        self.assertTrue(VMRecord.query.filter_by(vmid=500).first().destroyed)


class MembersTestCase(GroupsRoutesBaseTestCase):
    def _create_group(self):
        self.login('adminuser')
        r = self.client.post('/admin/groups',
                             json={'name': 'team-a', 'display_name': 'A'})
        return json.loads(r.data)['group']

    def test_add_member_by_login_id(self):
        g = self._create_group()
        r = self.client.post(f"/admin/groups/{g['id']}/members",
                             json={'login_id': 'studentuser'})
        self.assertEqual(r.status_code, 201)
        body = json.loads(r.data)
        self.assertEqual(body['member']['user_login_id'], 'studentuser')

    def test_add_member_unknown_user(self):
        g = self._create_group()
        r = self.client.post(f"/admin/groups/{g['id']}/members",
                             json={'login_id': 'nobody'})
        self.assertEqual(r.status_code, 404)

    def test_add_member_already_in_other_group(self):
        from app.models import StudentGroup, StudentGroupMember
        g = self._create_group()
        other = StudentGroup(name='other', display_name='Other')
        db.session.add(other)
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=other.id, user_id=self.student.id))
        db.session.commit()
        r = self.client.post(f"/admin/groups/{g['id']}/members",
                             json={'login_id': 'studentuser'})
        self.assertEqual(r.status_code, 409)

    def test_remove_member(self):
        from app.models import StudentGroupMember
        g = self._create_group()
        self.client.post(f"/admin/groups/{g['id']}/members",
                         json={'login_id': 'studentuser'})
        r = self.client.delete(f"/admin/groups/{g['id']}/members/{self.student.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(StudentGroupMember.query.count(), 0)

    def test_remove_member_warns_on_active_vms_without_confirm(self):
        from app.models import VMRecord
        from datetime import datetime, timedelta
        g = self._create_group()
        self.client.post(f"/admin/groups/{g['id']}/members",
                         json={'login_id': 'studentuser'})
        now = datetime.now()
        db.session.add(VMRecord(vmid=600, vm_name='x', vm_type='qemu', node='pve',
                                template_name='t', student_name='team-a',
                                owner_group_id=g['id'],
                                created_at=now, expires_at=now + timedelta(hours=1)))
        db.session.commit()
        r = self.client.delete(f"/admin/groups/{g['id']}/members/{self.student.id}")
        self.assertEqual(r.status_code, 409)
        r2 = self.client.delete(
            f"/admin/groups/{g['id']}/members/{self.student.id}?confirm_revoke=1")
        self.assertEqual(r2.status_code, 200)


class YamlPreviewTestCase(GroupsRoutesBaseTestCase):
    def test_preview_happy_path(self):
        self.login('adminuser')
        yaml_blob = (
            "groups:\n"
            "  - name: team-x\n"
            "    display_name: Team X\n"
            "    members: [studentuser]\n"
        )
        r = self.client.post('/admin/groups/yaml/preview',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        body = json.loads(r.data)
        self.assertIn('plan_token', body)
        self.assertEqual(len(body['diff']['groups_to_add']), 1)
        self.assertEqual(body['diff']['groups_to_add'][0]['name'], 'team-x')
        self.assertEqual(body['errors'], [])

    def test_preview_flags_duplicate_member(self):
        self.login('adminuser')
        yaml_blob = (
            "groups:\n"
            "  - name: a\n"
            "    display_name: A\n"
            "    members: [studentuser]\n"
            "  - name: b\n"
            "    display_name: B\n"
            "    members: [studentuser]\n"
        )
        r = self.client.post('/admin/groups/yaml/preview',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        errors = json.loads(r.data)['errors']
        self.assertTrue(any('studentuser' in e for e in errors))

    def test_preview_warns_on_unknown_login(self):
        self.login('adminuser')
        yaml_blob = (
            "groups:\n"
            "  - name: a\n"
            "    display_name: A\n"
            "    members: [ghost]\n"
        )
        r = self.client.post('/admin/groups/yaml/preview',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml')},
                             content_type='multipart/form-data')
        warnings = json.loads(r.data)['warnings']
        self.assertTrue(any('ghost' in w for w in warnings))


class YamlApplyTestCase(GroupsRoutesBaseTestCase):
    def test_apply_creates_groups_and_members(self):
        self.login('adminuser')
        yaml_blob = (
            "groups:\n"
            "  - name: team-x\n"
            "    display_name: Team X\n"
            "    members: [studentuser]\n"
        )
        prev = self.client.post('/admin/groups/yaml/preview',
                                data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml')},
                                content_type='multipart/form-data')
        self.assertEqual(prev.status_code, 200, prev.data)
        token = json.loads(prev.data)['plan_token']
        r = self.client.post('/admin/groups/yaml/apply',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml'),
                                   'plan_token': token},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        body = json.loads(r.data)
        self.assertEqual(len(body['created']), 1)
        self.assertEqual(StudentGroup.query.filter_by(name='team-x').count(), 1)

    def test_apply_rejects_stale_token(self):
        self.login('adminuser')
        yaml_blob = "groups:\n  - name: team-x\n    display_name: Team X\n"
        r = self.client.post('/admin/groups/yaml/apply',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml'),
                                   'plan_token': 'wrong-token'},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 409)

    def test_apply_refuses_when_errors(self):
        self.login('adminuser')
        # Bad slug (uppercase) triggers a build_plan validation error.
        yaml_blob = (
            "groups:\n"
            "  - name: TeamX\n"
            "    display_name: Team X\n"
        )
        prev = self.client.post('/admin/groups/yaml/preview',
                                data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml')},
                                content_type='multipart/form-data')
        token = json.loads(prev.data)['plan_token']
        r = self.client.post('/admin/groups/yaml/apply',
                             data={'file': (io.BytesIO(bytes(yaml_blob, 'utf-8')), 'groups.yaml'),
                                   'plan_token': token},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
