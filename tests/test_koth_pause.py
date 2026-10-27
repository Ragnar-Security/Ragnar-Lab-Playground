import unittest
from app import create_app
from app.models import db, User, KothGame
from werkzeug.security import generate_password_hash


class PauseTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.student = User(login_id='s', password_hash=generate_password_hash('p'))
        self.admin = User(login_id='a', password_hash=generate_password_hash('p'),
                          is_admin=True)
        db.session.add_all([self.student, self.admin]); db.session.commit()
        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=550, internal_vlan_tag=551,
                             status='active', is_active=True,
                             pause_other_deployments=True)
        db.session.add(self.game); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _login(self, u):
        with self.client.session_transaction() as s:
            s['user_id'] = u.id
            s['session_version'] = u.session_version

    def test_student_blocked_when_paused(self):
        self._login(self.student)
        r = self.client.post('/create', json={'template_name': 'whatever'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('paused', r.get_json()['error'].lower())

    def test_admin_bypass_when_paused(self):
        self._login(self.admin)
        # We don't care about success here, only that we don't get the pause 409.
        r = self.client.post('/create', json={'template_name': 'whatever'})
        self.assertNotEqual(r.status_code, 409)
