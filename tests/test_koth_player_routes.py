import time
import unittest
from unittest.mock import patch

from app import create_app, koth_scorer
from app.koth_events import EventBus
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

    def _count_session_checks(self):
        """Record every SQL statement that reads users.session_version."""
        from sqlalchemy import event
        seen = []

        def before(conn, cursor, statement, params, context, executemany):
            if 'session_version' in statement:
                seen.append(statement)
        event.listen(db.engine, 'before_cursor_execute', before)
        self.addCleanup(event.remove, db.engine, 'before_cursor_execute', before)
        return seen

    def test_stream_does_not_query_the_database_for_every_frame(self):
        # Every open tab wakes on the same publish. One session check per
        # frame is one pooled connection per tab per tick, all at once:
        # at classroom scale that burst is the size of the class.
        bus = EventBus()
        self._login(self.student)
        with patch.object(koth_scorer, 'event_bus', bus):
            response = self.client.get('/koth/api/stream', buffered=False)
            self.assertEqual(next(response.response), b'retry: 5000\n\n')
            checks = self._count_session_checks()
            for i in range(5):
                bus.publish('scan', {'i': i}, coalesce_key='scan')
                frame = next(response.response)
                self.assertIn(f'"i": {i}'.encode(), frame)
            response.close()
        self.assertLessEqual(len(checks), 1, checks)

    # --- how long an open stream outlives the reason it was allowed -------
    # Revalidation runs every STREAM_REVALIDATE_SEC; shortened here so the
    # bound is measurable. SLACK covers scheduling and the test client.
    BOUND = 0.3
    SLACK = 0.7

    def _open_stream(self, bus, client=None):
        response = (client or self.client).get('/koth/api/stream', buffered=False)
        self.assertEqual(next(response.response), b'retry: 5000\n\n')
        return response

    def _seconds_until_stream_ends(self, response, limit=20.0):
        """Read until the generator stops; fail on anything but pings."""
        started = time.monotonic()
        for frame in response.response:
            self.assertEqual(frame, b': ping\n\n')
            self.assertLess(time.monotonic() - started, limit)
        return time.monotonic() - started

    def _revoke_and_measure(self, revoke):
        from app.routes import koth_player
        bus = EventBus()
        self._login(self.student)
        with patch.object(koth_scorer, 'event_bus', bus), \
                patch.object(koth_player, 'STREAM_REVALIDATE_SEC', self.BOUND):
            response = self._open_stream(bus)
            revoke()
            # No publish: an idle stream must also notice.
            return self._seconds_until_stream_ends(response)

    def test_open_stream_ends_within_the_bound_after_logout(self):
        logout_client = self.app.test_client()
        with logout_client.session_transaction() as s:
            s['user_id'] = self.student.id
            s['session_version'] = self.student.session_version
        elapsed = self._revoke_and_measure(lambda: logout_client.post('/auth/logout'))
        self.assertLess(elapsed, self.BOUND + self.SLACK)

    def test_open_stream_ends_within_the_bound_after_disabling_the_user(self):
        # Write through UPDATE: the stream view's db.session.remove() runs in
        # the test's app context, which detaches setUp's instances.
        from sqlalchemy import update
        student_id = self.student.id

        def disable():
            db.session.execute(update(User).where(User.id == student_id)
                               .values(disabled=True))
            db.session.commit()
        elapsed = self._revoke_and_measure(disable)
        self.assertLess(elapsed, self.BOUND + self.SLACK)

    def test_open_stream_ends_within_the_bound_after_deleting_the_user(self):
        from sqlalchemy import delete
        from app.models import StudentGroupMember
        student_id = self.student.id

        def remove():
            db.session.execute(delete(StudentGroupMember)
                               .where(StudentGroupMember.user_id == student_id))
            db.session.execute(delete(User).where(User.id == student_id))
            db.session.commit()
        elapsed = self._revoke_and_measure(remove)
        self.assertLess(elapsed, self.BOUND + self.SLACK)

    def test_ending_the_game_closes_open_streams_at_once(self):
        game_id = self.game.id
        admin = User(login_id='admin-3.2', is_admin=True, password_hash='x')
        db.session.add(admin)
        db.session.commit()
        admin_client = self.app.test_client()
        with admin_client.session_transaction() as s:
            s['user_id'] = admin.id
            s['session_version'] = admin.session_version
        bus = EventBus()
        self._login(self.student)
        with patch.object(koth_scorer, 'event_bus', bus), \
                patch('app.routes.koth_admin._destroy_game_vms', return_value=0):
            response = self._open_stream(bus)
            r = admin_client.post(f'/koth/api/games/{game_id}/end')
            self.assertEqual(r.status_code, 200, r.data)
            started = time.monotonic()
            self.assertTrue(next(response.response).startswith(b'event: game_ended'))
            with self.assertRaises(StopIteration):
                next(response.response)
            self.assertLess(time.monotonic() - started, self.SLACK)

    def test_stream_ends_within_the_bound_when_its_game_stops_being_active(self):
        # A path that ends or resets the game without publishing game_ended
        # (force-reset, a direct DB change) must not leave streams open.
        from sqlalchemy import update
        game_id = self.game.id

        def deactivate():
            db.session.execute(update(KothGame).where(KothGame.id == game_id)
                               .values(status='draft', is_active=False))
            db.session.commit()
        elapsed = self._revoke_and_measure(deactivate)
        self.assertLess(elapsed, self.BOUND + self.SLACK)

    def test_stream_opened_between_games_follows_the_next_game_and_ends_with_it(self):
        from app.routes import koth_player
        self.game.status = 'ended'
        self.game.is_active = False
        db.session.commit()
        bus = EventBus()
        self._login(self.student)
        with patch.object(koth_scorer, 'event_bus', bus), \
                patch.object(koth_player, 'STREAM_REVALIDATE_SEC', self.BOUND):
            response = self._open_stream(bus)
            # No game: the stream stays open (closing it would only make
            # every tab reconnect every 5 s until a game starts).
            deadline = time.monotonic() + 3 * self.BOUND
            while time.monotonic() < deadline:
                self.assertEqual(next(response.response), b': ping\n\n')
            game2 = KothGame(name='g2', deployment_group_name='x',
                             entry_vlan_tag=552, internal_vlan_tag=553,
                             status='active', is_active=True)
            db.session.add(game2)
            db.session.commit()
            deadline = time.monotonic() + 2 * self.BOUND
            while time.monotonic() < deadline:
                self.assertEqual(next(response.response), b': ping\n\n')
            game2.status = 'ended'
            game2.is_active = False
            db.session.commit()
            elapsed = self._seconds_until_stream_ends(response)
        self.assertLess(elapsed, self.BOUND + self.SLACK)
