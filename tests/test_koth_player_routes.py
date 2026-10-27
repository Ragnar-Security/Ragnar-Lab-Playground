import unittest
from unittest.mock import patch

from app import create_app, koth_scorer
from app.models import db, User, StudentGroup, KothGame


class KothPlayerRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        from app.models import StudentGroupMember, KothTeam
        self.student = User(login_id='stud-3.2',
                            password_hash=generate_password_hash('p'))
        self.outsider = User(login_id='out-3.2',
                             password_hash=generate_password_hash('p'))
        db.session.add_all([self.student, self.outsider])
        sg = StudentGroup(name='team-a-3.2', display_name='Team A')
        db.session.add(sg)
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=sg.id, user_id=self.student.id))
        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=550, internal_vlan_tag=551,
                             status='active', is_active=True)
        db.session.add(self.game); db.session.commit()
        self.team = KothTeam(game_id=self.game.id, student_group_id=sg.id,
                             color='red', submission_token='tok-3.2',
                             display_name='Team A')
        db.session.add(self.team); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def test_state_returns_game(self):
        self._login(self.student)
        r = self.client.get('/koth/api/state')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['game']['id'], self.game.id)
        self.assertIn('scores', body)

    def test_state_redacts_when_scoreboard_hidden(self):
        self.game.show_scoreboard = False
        db.session.commit()
        self._login(self.student)
        r = self.client.get('/koth/api/state')
        body = r.get_json()
        self.assertEqual(body['scores'], {})
        self.assertTrue(body['hidden']['scoreboard'])

    def test_state_redacts_map_when_hidden(self):
        self.game.show_network_map = False
        db.session.commit()
        self._login(self.student)
        r = self.client.get('/koth/api/state')
        body = r.get_json()
        self.assertEqual(body['boxes'], [])
        self.assertTrue(body['hidden']['network_map'])

    def test_my_team_returns_token_for_member(self):
        self._login(self.student)
        r = self.client.get('/koth/api/my-team')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['team']['submission_token'], 'tok-3.2')

    def test_my_team_404_for_non_member(self):
        self._login(self.outsider)
        r = self.client.get('/koth/api/my-team')
        self.assertEqual(r.status_code, 404)

    def test_no_active_game_returns_204(self):
        self.game.status = 'ended'
        self.game.is_active = False
        db.session.commit()
        self._login(self.student)
        r = self.client.get('/koth/api/state')
        self.assertEqual(r.status_code, 204)

    def test_open_stream_stops_after_logout(self):
        class ImmediateHeartbeatSubscription:
            def drain(self, timeout):
                return []

        class StubEventBus:
            def __init__(self):
                self.subscription = ImmediateHeartbeatSubscription()

            def subscribe(self):
                return self.subscription

            def unsubscribe(self, subscription):
                pass

        logout_client = self.app.test_client()
        self._login(self.student)
        with logout_client.session_transaction() as s:
            s['user_id'] = self.student.id
            s['session_version'] = self.student.session_version

        with patch.object(koth_scorer, 'event_bus', StubEventBus()):
            response = self.client.get('/koth/api/stream', buffered=False)
            first_frame = next(response.response)
            self.assertEqual(first_frame, b'retry: 5000\n\n')

            logout_client.post('/auth/logout')

            with self.assertRaises(StopIteration):
                next(response.response)
