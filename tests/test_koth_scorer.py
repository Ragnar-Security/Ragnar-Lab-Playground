import unittest
from unittest.mock import patch
from app import create_app
from app.models import db, User, StudentGroup, KothGame, KothTeam, KothBox


class ScorerTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=550, internal_vlan_tag=551,
                             status='active', is_active=True)
        db.session.add(self.game)
        sg = StudentGroup(name='a-scorer', display_name='A')
        db.session.add(sg)
        db.session.commit()
        self.team = KothTeam(game_id=self.game.id, student_group_id=sg.id,
                             color='red', submission_token='tok-scorer',
                             display_name='A')
        db.session.add(self.team)
        db.session.commit()
        self.box = KothBox(game_id=self.game.id, name='b1',
                           ips=['10.0.0.5'],
                           services=[{'name': 'SSH', 'port': 22, 'status': 'closed'}],
                           owner_team_id=self.team.id)
        db.session.add(self.box)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_tick_updates_service_status_and_records_snapshot(self):
        from app import koth_scorer
        with patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.box)
        self.assertEqual(self.box.services[0]['status'], 'open')
        from app.models import KothScoreSnapshot
        snaps = KothScoreSnapshot.query.filter_by(game_id=self.game.id).all()
        self.assertEqual(len(snaps), 1)
        self.assertGreater(snaps[0].score, 0)

    def test_tick_increments_iteration(self):
        from app import koth_scorer
        with patch('app.koth_scorer.probe_port', return_value='closed'):
            koth_scorer.run_tick(self.game.id)
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.game)
        self.assertEqual(self.game.scoring_iteration, 2)

    def test_tick_no_op_when_game_ended(self):
        self.game.status = 'ended'
        self.game.is_active = False
        db.session.commit()
        from app import koth_scorer
        koth_scorer.run_tick(self.game.id)
        from app.models import KothScoreSnapshot
        self.assertEqual(KothScoreSnapshot.query.count(), 0)

    # -------------------------------------------------------------------
    # New: time-boxed games auto-end when ends_at passes
    # -------------------------------------------------------------------
    def test_tick_auto_ends_when_duration_elapsed(self):
        from datetime import datetime, timedelta
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        self.game.duration_minutes = 1
        self.game.started_at = datetime.now() - timedelta(minutes=2)
        self.game.ends_at = datetime.now() - timedelta(seconds=10)
        db.session.commit()
        with patch('app.routes.koth_admin.cleanup_vms', return_value=None), \
             patch('app.proxmox_service.find_vms_by_name_prefix', return_value=[]), \
             patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.game)
        self.assertEqual(self.game.status, 'ended')
        self.assertFalse(self.game.is_active)
        self.assertIsNotNone(self.game.ended_at)
        # Auto-end returns BEFORE scoring, so no snapshot inserted on this tick.
        self.assertEqual(KothScoreSnapshot.query.count(), 0)

    def test_tick_does_not_auto_end_before_ends_at(self):
        from datetime import datetime, timedelta
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        self.game.duration_minutes = 60
        self.game.started_at = datetime.now()
        self.game.ends_at = datetime.now() + timedelta(hours=1)
        db.session.commit()
        with patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.game)
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(KothScoreSnapshot.query.count(), 1)

    # -------------------------------------------------------------------
    # New: claim-poll integration in run_tick
    # -------------------------------------------------------------------
    def test_tick_calls_poll_claims(self):
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        with patch('app.koth_scorer._poll_claims', return_value=1) as mock_poll, \
             patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
        # Called with the game and a list of boxes.
        mock_poll.assert_called_once()
        args, kwargs = mock_poll.call_args
        # First arg: the active game
        self.assertEqual(args[0].id, self.game.id)
        # Second arg: list of KothBox rows
        self.assertEqual(len(args[1]), 1)
        self.assertEqual(args[1][0].id, self.box.id)
        # Rest of the tick still ran -> probe + score snapshot.
        db.session.refresh(self.box)
        self.assertEqual(self.box.services[0]['status'], 'open')
        self.assertEqual(KothScoreSnapshot.query.count(), 1)

    def test_tick_skips_poll_claims_failure_gracefully(self):
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        with patch('app.koth_scorer._poll_claims',
                   side_effect=Exception('boom')) as mock_poll, \
             patch('app.koth_scorer.probe_port', return_value='open'), \
             patch.object(koth_scorer.logger, 'exception') as mock_log:
            # Must not raise.
            koth_scorer.run_tick(self.game.id)
        mock_poll.assert_called_once()
        mock_log.assert_called()
        # Rest of the tick still ran.
        db.session.refresh(self.box)
        self.assertEqual(self.box.services[0]['status'], 'open')
        self.assertEqual(KothScoreSnapshot.query.count(), 1)

    # -------------------------------------------------------------------
    # Scheduler boot reads the configured interval from the active game
    # (regression: was hard-coded to 12s and ignored the admin's POLL
    # INTERVAL setting on container restart, causing APScheduler to
    # constantly skip jobs with "max instances reached").
    # -------------------------------------------------------------------
    def test_resolve_initial_interval_uses_active_game_setting(self):
        from app import koth_scorer
        self.game.scan_interval_sec = 45
        db.session.commit()
        self.assertEqual(
            koth_scorer._resolve_initial_interval(self.app, fallback=12),
            45)

    def test_resolve_initial_interval_falls_back_when_no_active_game(self):
        from app import koth_scorer
        self.game.is_active = False
        self.game.status = 'ended'
        db.session.commit()
        self.assertEqual(
            koth_scorer._resolve_initial_interval(self.app, fallback=99),
            99)

    def test_start_scheduler_picks_up_db_interval_at_boot(self):
        # End-to-end: start_scheduler with no explicit interval_sec must
        # query the DB for the active game's setting. We mock APScheduler
        # so the test doesn't actually start a thread.
        from unittest.mock import patch as _patch, MagicMock
        from app import koth_scorer
        self.game.scan_interval_sec = 30
        db.session.commit()
        # Ensure no leftover scheduler from a prior test.
        koth_scorer._scheduler = None
        fake_sched = MagicMock()
        with _patch('apscheduler.schedulers.background.BackgroundScheduler',
                    return_value=fake_sched):
            koth_scorer.start_scheduler(self.app)
        try:
            fake_sched.add_job.assert_called_once()
            kwargs = fake_sched.add_job.call_args.kwargs
            self.assertEqual(kwargs.get('seconds'), 30)
            self.assertEqual(kwargs.get('id'), 'koth_tick')
        finally:
            koth_scorer._scheduler = None

    def test_start_scheduler_explicit_interval_overrides_db(self):
        # The deploy path passes the freshly-saved interval explicitly.
        from unittest.mock import patch as _patch, MagicMock
        from app import koth_scorer
        self.game.scan_interval_sec = 30
        db.session.commit()
        koth_scorer._scheduler = None
        fake_sched = MagicMock()
        with _patch('apscheduler.schedulers.background.BackgroundScheduler',
                    return_value=fake_sched):
            koth_scorer.start_scheduler(self.app, interval_sec=7)
        try:
            self.assertEqual(fake_sched.add_job.call_args.kwargs.get('seconds'), 7)
        finally:
            koth_scorer._scheduler = None

    def test_tick_unlimited_game_never_auto_ends(self):
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        self.game.duration_minutes = None
        self.game.ends_at = None
        db.session.commit()
        with patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
            koth_scorer.run_tick(self.game.id)
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.game)
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.scoring_iteration, 3)
        # History rows are written on wall-clock cadence
        # (KOTH_SNAPSHOT_INTERVAL_SEC / scan_interval_sec = 60/12 = every
        # 5th tick), plus the first tick so a game always has a baseline.
        # The live scoreboard reads KothTeam.score, not these rows.
        self.assertEqual(KothScoreSnapshot.query.count(), 1)
        db.session.refresh(self.team)
        self.assertGreater(self.team.score, 0)

    def test_snapshot_cadence_follows_scan_interval(self):
        # A 60s tick means every tick is also a history row; a 12s tick
        # must not write five times as many.
        from app import koth_scorer
        from app.models import KothScoreSnapshot
        self.game.scan_interval_sec = 60
        db.session.commit()
        with patch('app.koth_scorer.probe_port', return_value='closed'):
            for _ in range(3):
                koth_scorer.run_tick(self.game.id)
        self.assertEqual(KothScoreSnapshot.query.count(), 3)

    def test_tick_score_accumulates_on_the_team_row(self):
        # The scoreboard reads KothTeam.score directly, so the tick has
        # to accumulate there — not only into the history table.
        from app import koth_scorer
        with patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
            first = db.session.get(KothTeam, self.team.id).score
            koth_scorer.run_tick(self.game.id)
        second = db.session.get(KothTeam, self.team.id).score
        self.assertGreater(first, 0)
        self.assertGreater(second, first)
        self.assertAlmostEqual(
            koth_scorer.latest_scores(self.game.id)[self.team.id], second)

    def test_tick_probes_every_service_not_just_the_first(self):
        # Regression guard for the probe fan-out: a box with several
        # services must get every one probed, and the single-service case
        # must not take a different code path.
        from app import koth_scorer
        self.box.services = [
            {'name': 'SSH', 'port': 22, 'status': 'closed'},
            {'name': 'HTTP', 'port': 80, 'status': 'closed'},
            {'name': 'SMB', 'port': 445, 'status': 'closed'},
        ]
        db.session.commit()
        probed = []

        def fake_probe(host, port, timeout=None):
            probed.append((host, port))
            return 'closed' if port == 445 else 'open'

        with patch('app.koth_scorer.probe_port', side_effect=fake_probe):
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.box)
        self.assertEqual(sorted(p for _, p in probed), [22, 80, 445])
        self.assertEqual([s['status'] for s in self.box.services],
                         ['open', 'open', 'closed'])

    def test_auto_end_uses_local_wall_clock(self):
        # Regression: the deploy path wrote started_at with utcnow() while
        # the tick compared ends_at against now(). In any non-UTC timezone
        # that delayed auto-end by the full offset, leaving every VM
        # running for hours past the end of class.
        from datetime import datetime, timedelta
        from app import koth_scorer
        from app.routes.koth_admin import _compute_ends_at
        self.game.duration_minutes = 30
        self.game.started_at = datetime.now() - timedelta(minutes=31)
        self.game.ends_at = _compute_ends_at(self.game)
        db.session.commit()
        with patch('app.routes.koth_admin.cleanup_vms', return_value=None), \
             patch('app.proxmox_service.find_vms_by_name_prefix', return_value=[]), \
             patch('app.koth_scorer.probe_port', return_value='open'):
            koth_scorer.run_tick(self.game.id)
        db.session.refresh(self.game)
        self.assertEqual(self.game.status, 'ended')
