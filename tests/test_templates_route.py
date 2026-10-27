"""Tests for /templates filtering when group-mode is involved."""

import json
import unittest
from unittest.mock import patch
from app import create_app
from app.models import db, User, StudentGroup, StudentGroupMember


FAKE_CONFIG = {'deployment_groups': [
    {'display_name': 'Individual Lab', 'enabled': True, 'group_mode': False,
     'templates': [{'name': 'vm', 'entry_point': True}]},
    {'display_name': 'Shared Lab', 'enabled': True, 'group_mode': True,
     'templates': [{'name': 'vm', 'entry_point': True}]},
]}


class TemplatesGroupFilterTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.solo = User(login_id='solo', full_name='Solo',
                          entry_vlan_tag=101, internal_vlan_tag=201)
        self.solo.set_password('x')
        self.member = User(login_id='member', full_name='Member',
                           entry_vlan_tag=102, internal_vlan_tag=202)
        self.member.set_password('x')
        self.admin = User(login_id='admin', full_name='Admin',
                          entry_vlan_tag=103, internal_vlan_tag=203,
                          is_admin=True)
        self.admin.set_password('x')
        db.session.add_all([self.solo, self.member, self.admin])
        db.session.commit()
        g = StudentGroup(name='team-a', display_name='Team A',
                         entry_vlan_tag=300, internal_vlan_tag=400)
        db.session.add(g)
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=g.id, user_id=self.member.id))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, login_id):
        return self.client.post('/auth/login', data={'login_id': login_id, 'password': 'x'})

    def test_non_group_user_does_not_see_group_mode_labs(self):
        self._login('solo')
        with patch('app.routes.templates.load_template_config', return_value=FAKE_CONFIG):
            r = self.client.get('/templates')
        payload = json.loads(r.data)
        # The response might be {'deployment_groups': [...]} or {'success': True, 'deployment_groups': [...]} or similar.
        # Locate the list of deployment groups in whatever key the current route uses.
        dgs = payload.get('deployment_groups') or payload.get('groups') or []
        names = [g.get('display_name') for g in dgs]
        self.assertIn('Individual Lab', names)
        self.assertNotIn('Shared Lab', names)

    def test_group_member_sees_group_mode_lab(self):
        self._login('member')
        with patch('app.routes.templates.load_template_config', return_value=FAKE_CONFIG):
            r = self.client.get('/templates')
        payload = json.loads(r.data)
        dgs = payload.get('deployment_groups') or payload.get('groups') or []
        names = [g.get('display_name') for g in dgs]
        self.assertIn('Shared Lab', names)

    def test_koth_mode_group_hidden_from_students(self):
        cfg = {'deployment_groups': [
            {'display_name': 'Regular', 'enabled': True, 'templates': []},
            {'display_name': 'KOTH', 'enabled': True, 'koth_mode': True, 'templates': []},
        ], 'templates': []}
        self._login('solo')
        with patch('app.routes.templates.load_template_config', return_value=cfg):
            r = self.client.get('/templates')
        payload = json.loads(r.data)
        dgs = payload.get('deployment_groups') or payload.get('groups') or []
        names = [g.get('display_name') for g in dgs]
        self.assertIn('Regular', names)
        self.assertNotIn('KOTH', names)

    def test_koth_mode_group_visible_to_admin(self):
        cfg = {'deployment_groups': [
            {'display_name': 'Regular', 'enabled': True, 'templates': []},
            {'display_name': 'KOTH', 'enabled': True, 'koth_mode': True, 'templates': []},
        ], 'templates': []}
        self._login('admin')
        with patch('app.routes.templates.load_template_config', return_value=cfg):
            r = self.client.get('/templates')
        payload = json.loads(r.data)
        dgs = payload.get('deployment_groups') or payload.get('groups') or []
        names = [g.get('display_name') for g in dgs]
        self.assertIn('Regular', names)
        self.assertIn('KOTH', names)


if __name__ == '__main__':
    unittest.main()
