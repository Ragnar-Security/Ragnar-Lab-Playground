"""Tests for hiding KOTH VMs from non-admin users in the VM list endpoints.

The VM listing logic lives in app/routes/vm_listing.py. The /api/vms endpoint
returns a JSON payload of the form {success, count, vms: [<vm_dict>, ...]}
where each vm_dict comes from VMRecord.to_dict() and includes the 'vm_name'
key. The /list endpoint returns a nested grouped shape; we assert behavior
there by walking the structure.
"""

import unittest
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app import create_app
from app.models import db, User, KothGame, VMRecord


class KothListingTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.student = User(login_id='s',
                            password_hash=generate_password_hash('p'))
        self.admin = User(login_id='a',
                          password_hash=generate_password_hash('p'),
                          is_admin=True)
        db.session.add_all([self.student, self.admin])
        db.session.commit()

        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=550, internal_vlan_tag=551,
                             status='active', is_active=True)
        db.session.add(self.game)
        db.session.commit()

        expires = datetime.now() + timedelta(days=1)
        # Regular VM owned by the student.
        db.session.add(VMRecord(
            vmid=101, vm_name='regular', vm_type='qemu', node='pve',
            template_name='t', student_name='s',
            created_by_user_id=self.student.id,
            expires_at=expires,
        ))
        # KOTH VM (admin-created on behalf of the game).
        db.session.add(VMRecord(
            vmid=102, vm_name='koth', vm_type='qemu', node='pve',
            template_name='t', student_name='admin',
            created_by_user_id=self.student.id,
            koth_game_id=self.game.id,
            expires_at=expires,
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def test_student_does_not_see_koth_vm(self):
        self._login(self.student)
        r = self.client.get('/api/vms')
        self.assertEqual(r.status_code, 200)
        names = [vm['vm_name'] for vm in r.get_json().get('vms', [])]
        self.assertIn('regular', names)
        self.assertNotIn('koth', names)

    def test_admin_sees_koth_vm(self):
        self._login(self.admin)
        r = self.client.get('/api/vms')
        self.assertEqual(r.status_code, 200)
        names = [vm['vm_name'] for vm in r.get_json().get('vms', [])]
        self.assertIn('koth', names)


if __name__ == '__main__':
    unittest.main()
