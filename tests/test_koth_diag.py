"""Tests for GET /koth/api/_diag.

Every failure that took the server down during a classroom game was
invisible while it was happening. This endpoint is the instrument, so it
has to answer even when there is no game and must never be reachable by a
student.
"""

import unittest

from app import create_app, koth_scorer
from app.models import db, User, StudentGroup, KothGame, KothTeam, KothBox


class DiagTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='diag-admin', is_admin=True,
                          password_hash=generate_password_hash('p'))
        self.student = User(login_id='diag-stud',
                            password_hash=generate_password_hash('p'))
        db.session.add_all([self.admin, self.student])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def _seed_game(self):
        game = KothGame(name='g', deployment_group_name='x',
                        entry_vlan_tag=570, internal_vlan_tag=571,
                        status='active', is_active=True,
                        claim_mode='beacon', claim_port=31337,
                        scoring_iteration=9)
        db.session.add(game)
        sg = StudentGroup(name='diag-a', display_name='A')
        db.session.add(sg)
        db.session.commit()
        team = KothTeam(game_id=game.id, student_group_id=sg.id, color='red',
                        submission_token='tok-diag', display_name='A')
        db.session.add(team)
        db.session.commit()
        db.session.add_all([
            KothBox(game_id=game.id, name='b1', ips=['10.0.0.1'],
                    services=[{'name': 'ssh', 'port': 22, 'status': 'open'}],
                    owner_team_id=team.id),
            KothBox(game_id=game.id, name='b2', ips=[],
                    services=[{'name': 'http', 'port': 80, 'status': 'closed'},
                              {'name': 'smb', 'port': 445, 'status': 'closed'}],
                    owner_team_id=None),
        ])
        db.session.commit()
        return game

    def test_requires_admin(self):
        self._login(self.student)
        self.assertEqual(self.client.get('/koth/api/_diag').status_code, 403)

    def test_requires_login(self):
        r = self.client.get('/koth/api/_diag')
        self.assertIn(r.status_code, (401, 302))

    def test_answers_with_no_active_game(self):
        # The admin will hit this before creating a game; it must not 500.
        self._login(self.admin)
        r = self.client.get('/koth/api/_diag')
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertIsNone(body['game'])
        self.assertIn('events', body)
        self.assertIn('tick', body)

    def test_reports_the_numbers_that_matter_during_a_game(self):
        game = self._seed_game()
        self._login(self.admin)
        sub = koth_scorer.event_bus.subscribe()
        try:
            r = self.client.get('/koth/api/_diag')
            self.assertEqual(r.status_code, 200, r.data)
            body = r.get_json()
            self.assertGreaterEqual(body['events']['subscribers'], 1)
            self.assertEqual(body['game']['id'], game.id)
            self.assertEqual(body['game']['iteration'], 9)
            self.assertEqual(body['game']['claim_mode'], 'beacon')
            self.assertEqual(body['game']['claim_port'], 31337)
            self.assertEqual(body['game']['boxes'], 2)
            self.assertEqual(body['game']['boxes_owned'], 1)
            # A box with no IP can be neither probed nor beaconed — this is
            # the number that explains "why is nothing scoring".
            self.assertEqual(body['game']['boxes_without_ip'], 1)
            # Tells the admin how much network work one tick is doing.
            self.assertEqual(body['game']['services_probed_per_tick'], 3)
        finally:
            koth_scorer.event_bus.unsubscribe(sub)

    def test_surfaces_tick_overruns(self):
        # A tick that outruns its interval makes APScheduler silently skip
        # runs; before this counter existed there was no way to see it.
        self._seed_game()
        self._login(self.admin)
        koth_scorer._tick_stats.update(
            {'last_sec': 31.0, 'max_sec': 31.0, 'count': 4, 'overruns': 2})
        try:
            body = self.client.get('/koth/api/_diag').get_json()
            self.assertEqual(body['tick']['overruns'], 2)
            self.assertEqual(body['tick']['max_sec'], 31.0)
        finally:
            koth_scorer._tick_stats.update(
                {'last_sec': 0.0, 'max_sec': 0.0, 'count': 0, 'overruns': 0})


if __name__ == '__main__':
    unittest.main()
