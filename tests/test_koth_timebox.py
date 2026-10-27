"""Regression tests for KOTH time-boxing across timezones.

The bug these exist for: the deploy path stamped `started_at` with
`datetime.utcnow()`, `_compute_ends_at` derived `ends_at` from it, and the
scoring tick compared `ends_at` against `datetime.now()`. On a UTC-4 host
a 3-hour game therefore ran for 7 hours with every VM up and billable.

The first version of the fix only changed the serializer, which removed
the symptom (a visibly wrong countdown) and left the bug. These tests
assert on the stored values and on the actual auto-end, not on display.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app, koth_scorer
from app.models import db, User, StudentGroup, KothGame, KothTeam, KothBox


def _naive_utc():
    """The old `utcnow()` value, spelled without the deprecated API. The
    mixed-convention tests need a UTC-stamped naive datetime on purpose."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _skew():
    """How far the host's local clock is from UTC, in seconds.

    0 on a UTC host, which is why this bug survived CI and only bit in a
    classroom. The assertions below are written so they hold either way,
    and the mixed-convention assertions are the ones that actually catch
    a regression on a non-UTC host.
    """
    return (datetime.now() - _naive_utc()).total_seconds()


class TimeboxTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='tb-admin', is_admin=True,
                          password_hash=generate_password_hash('p'))
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self):
        with self.client.session_transaction() as s:
            s['user_id'] = self.admin.id
            s['session_version'] = self.admin.session_version

    def _game(self, **kw):
        fields = dict(name='g', deployment_group_name='x',
                      entry_vlan_tag=580, internal_vlan_tag=581,
                      status='active', is_active=True)
        fields.update(kw)
        g = KothGame(**fields)
        db.session.add(g)
        db.session.commit()
        return g

    def test_deploy_stamps_started_at_on_the_same_clock_the_tick_reads(self):
        # The writer and the comparator must use one convention. Anchor on
        # datetime.now(), which is what run_tick compares against.
        from app.routes import koth_admin
        before = datetime.now()
        g = self._game(status='draft', is_active=False, duration_minutes=30)

        captured = {}

        def fake_deploy_bundle(**kwargs):
            return []

        with patch.object(koth_admin, 'deploy_bundle', side_effect=fake_deploy_bundle), \
             patch.object(koth_admin, '_resolve_koth_group',
                          return_value=({'templates': [], 'uptime_minutes': 1}, None)), \
             patch.object(koth_admin, '_materialize_koth_boxes', return_value=[]), \
             patch.object(koth_admin, '_start_scheduler'):
            self._login()
            r = self.client.post('/koth/api/games/%d/deploy' % g.id)
        self.assertEqual(r.status_code, 200, r.data)
        after = datetime.now()

        db.session.refresh(g)
        captured['started_at'] = g.started_at
        self.assertIsNotNone(g.started_at)
        self.assertGreaterEqual(g.started_at, before - timedelta(seconds=5))
        self.assertLessEqual(g.started_at, after + timedelta(seconds=5),
                             'started_at is ahead of local now() — it was '
                             'written on a different clock than the tick reads')
        # ends_at must land duration_minutes into the local future.
        self.assertAlmostEqual(
            (g.ends_at - datetime.now()).total_seconds(), 30 * 60, delta=10)

    def test_game_auto_ends_after_its_duration_not_a_tz_offset_later(self):
        # Walk the real lifecycle: a game whose duration has just elapsed,
        # measured on the local clock, must end on the very next tick.
        started = datetime.now() - timedelta(minutes=31)
        g = self._game(duration_minutes=30, started_at=started)
        from app.routes.koth_admin import _compute_ends_at
        g.ends_at = _compute_ends_at(g)
        db.session.commit()
        with patch('app.routes.koth_admin.cleanup_vms', return_value=None), \
             patch('app.proxmox_service.find_vms_by_name_prefix', return_value=[]):
            koth_scorer.run_tick(g.id)
        db.session.refresh(g)
        self.assertEqual(g.status, 'ended')
        self.assertIsNotNone(g.ended_at)

    def test_a_game_still_inside_its_duration_does_not_end_early(self):
        started = datetime.now() - timedelta(minutes=29)
        g = self._game(duration_minutes=30, started_at=started)
        from app.routes.koth_admin import _compute_ends_at
        g.ends_at = _compute_ends_at(g)
        db.session.commit()
        koth_scorer.run_tick(g.id)
        db.session.refresh(g)
        self.assertEqual(g.status, 'active')

    def test_utc_written_started_at_would_overrun(self):
        # Pins WHY the convention matters. A game stamped the old way
        # (utcnow) does not end on time on a non-UTC host; on a UTC host
        # the two clocks coincide and there is nothing to detect, so this
        # only asserts where it can.
        skew = _skew()
        if abs(skew) < 60:
            self.skipTest('host runs on UTC; the two conventions coincide')
        from app.routes.koth_admin import _compute_ends_at
        g = self._game(duration_minutes=1,
                       started_at=_naive_utc() - timedelta(minutes=2))
        g.ends_at = _compute_ends_at(g)
        db.session.commit()
        koth_scorer.run_tick(g.id)
        db.session.refresh(g)
        # Demonstrates the hazard rather than endorsing it: a UTC-stamped
        # started_at on a host behind UTC does NOT end when it should.
        if skew < 0:
            self.assertEqual(g.status, 'active')

    def test_end_game_and_manual_claim_use_the_same_clock(self):
        g = self._game()
        sg = StudentGroup(name='tb-a', display_name='A')
        db.session.add(sg)
        db.session.commit()
        team = KothTeam(game_id=g.id, student_group_id=sg.id, color='red',
                        submission_token='tok-tb', display_name='A')
        db.session.add(team)
        db.session.commit()
        box = KothBox(game_id=g.id, name='b1', ips=[], services=[])
        db.session.add(box)
        db.session.commit()

        self._login()
        before = datetime.now()
        r = self.client.post(
            '/koth/api/games/%d/boxes/%d/manual-claim' % (g.id, box.id),
            json={'team_id': team.id})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(box)
        self.assertLessEqual(box.claimed_at, datetime.now() + timedelta(seconds=5))
        self.assertGreaterEqual(box.claimed_at, before - timedelta(seconds=5))
        # A claimed_at in the future relative to now() would make the
        # per-box claim cooldown (which does `datetime.now() - last`) treat
        # the box as permanently cooled down.
        self.assertLess(box.claimed_at, datetime.now() + timedelta(seconds=5))

        with patch('app.routes.koth_admin._destroy_game_vms', return_value=0):
            r = self.client.post('/koth/api/games/%d/end' % g.id)
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(g)
        self.assertLessEqual(g.ended_at, datetime.now() + timedelta(seconds=5))
        self.assertGreaterEqual(g.ended_at, before - timedelta(seconds=5))


if __name__ == '__main__':
    unittest.main()
