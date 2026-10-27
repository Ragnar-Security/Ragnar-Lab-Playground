"""End-to-end tests for the new POST /koth/api/flags/submit route."""

import unittest
from unittest.mock import patch

from app import create_app
from app.models import (
    db, User, StudentGroup, StudentGroupMember,
    KothGame, KothTeam, KothFlag, KothFlagSubmission, KothClaimEvent,
)


class FlagSubmitTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='admin-flag',
                          password_hash=generate_password_hash('p'),
                          is_admin=True)
        self.student = User(login_id='stud-flag',
                            password_hash=generate_password_hash('p'))
        self.outsider = User(login_id='out-flag',
                             password_hash=generate_password_hash('p'))
        db.session.add_all([self.admin, self.student, self.outsider])
        self.sg = StudentGroup(name='team-flag', display_name='Team Flag')
        db.session.add(self.sg)
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=self.sg.id,
                                          user_id=self.student.id))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def _seed_active_game(self, *, flag_value='good-uuid', points=10,
                          submission_count_limit=1):
        game = KothGame(name='g', deployment_group_name='x',
                        entry_vlan_tag=550, internal_vlan_tag=551,
                        status='active', is_active=True)
        db.session.add(game)
        db.session.commit()
        team = KothTeam(game_id=game.id, student_group_id=self.sg.id,
                        color='red', submission_token='tok-flag',
                        display_name='Team Flag')
        db.session.add(team)
        db.session.commit()
        flag = KothFlag(game_id=game.id, name='Root Flag', value=flag_value,
                        points=points,
                        submission_count_limit=submission_count_limit)
        db.session.add(flag)
        db.session.commit()
        return game, team, flag

    # -----------------------------------------------------------------
    # Authorisation / shape errors
    # -----------------------------------------------------------------
    def test_404_when_no_active_game(self):
        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit', json={'value': 'x'})
        self.assertEqual(r.status_code, 404)

    def test_403_when_user_not_on_team(self):
        self._seed_active_game()
        self._login(self.outsider)
        r = self.client.post('/koth/api/flags/submit', json={'value': 'good-uuid'})
        self.assertEqual(r.status_code, 403, r.data)

    def test_400_when_value_missing(self):
        self._seed_active_game()
        self._login(self.student)
        r1 = self.client.post('/koth/api/flags/submit', json={})
        self.assertEqual(r1.status_code, 400)
        r2 = self.client.post('/koth/api/flags/submit', json={'value': ''})
        self.assertEqual(r2.status_code, 400)

    # -----------------------------------------------------------------
    # Happy path
    # -----------------------------------------------------------------
    def test_correct_value_accepted_and_credits_team(self):
        game, team, flag = self._seed_active_game(flag_value='good-uuid',
                                                  points=10)
        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit',
                             json={'value': 'good-uuid'})
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body['accepted'])
        self.assertEqual(body['points'], 10)
        self.assertEqual(body['flag_name'], 'Root Flag')

        accepted = KothFlagSubmission.query.filter_by(
            flag_id=flag.id, accepted=True).all()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].team_id, team.id)
        self.assertEqual(accepted[0].submitted_by_user_id, self.student.id)

        events = KothClaimEvent.query.filter_by(
            game_id=game.id, event_type='flag').all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_id, team.id)
        self.assertEqual(events[0].points_delta, 10)

    def test_accepted_flag_shows_in_latest_scores(self):
        # Regression: previously submit_flag only wrote a KothClaimEvent
        # (audit only), and a follow-up "fix" wrote a KothScoreSnapshot
        # bump that raced with the tick — the tick read its `prev` before
        # the bump committed, then overwrote the snapshot at the same
        # iteration, silently losing the bonus (user-reported and
        # reproduced in the live stack). The fix splits the sources: the
        # tick owns KothTeam.score outright, flag bonuses live in
        # KothFlagSubmission, and latest_scores adds them at read time.
        # No race is possible — the two writers no longer share a value.
        from app.models import KothScoreSnapshot
        from app import koth_scorer
        game, team, flag = self._seed_active_game(flag_value='good-uuid',
                                                  points=7)
        # Pre-existing tick score worth 12 points (cumulative).
        team.score = 12.0
        game.scoring_iteration = 3
        db.session.commit()

        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit',
                             json={'value': 'good-uuid'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.get_json()['accepted'])

        # submit_flag must not touch either score store.
        db.session.refresh(team)
        self.assertEqual(team.score, 12.0,
                         'submit_flag must not write the tick score')
        self.assertEqual(
            KothScoreSnapshot.query.filter_by(game_id=game.id).count(), 0,
            'submit_flag must not write snapshots')
        self.assertAlmostEqual(koth_scorer.latest_scores(game.id)[team.id],
                               19.0)  # 12 (tick cumulative) + 7 (flag)

    def test_first_flag_with_no_prior_score_shows_bonus_only(self):
        from app import koth_scorer
        game, team, flag = self._seed_active_game(flag_value='good-uuid',
                                                  points=5)
        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit',
                             json={'value': 'good-uuid'})
        self.assertEqual(r.status_code, 200, r.data)
        # Team has never scored a tick — latest_scores still surfaces the bonus.
        self.assertAlmostEqual(koth_scorer.latest_scores(game.id)[team.id],
                               5.0)

    def test_concurrent_tick_does_not_lose_bonus(self):
        # Repro of the user-reported bug, against the current design: a
        # real tick runs after the flag submit and rewrites the team's
        # score. The bonus must survive, because the tick never reads or
        # writes it. Pre-fix this silently returned 0.
        from app import koth_scorer
        game, team, flag = self._seed_active_game(flag_value='good-uuid',
                                                  points=4)
        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit',
                             json={'value': 'good-uuid'})
        self.assertEqual(r.status_code, 200, r.data)
        # Two real ticks against an unowned board: each recomputes and
        # commits team.score. Under the old shared-row model this is
        # exactly what clobbered the bonus.
        with patch('app.koth_scorer.probe_port', return_value='closed'), \
             patch('app.koth_scorer._poll_claims', return_value=0):
            koth_scorer.run_tick(game.id)
            koth_scorer.run_tick(game.id)
        db.session.refresh(team)
        self.assertEqual(team.score, 0.0, 'no boxes owned, so no tick score')
        self.assertAlmostEqual(koth_scorer.latest_scores(game.id)[team.id],
                               4.0, msg='flag bonus must survive ticks')

    def test_only_accepted_flags_count_toward_bonus(self):
        # Wrong-value submissions insert KothFlagSubmission(accepted=False)
        # for audit; they must NOT contribute to the score.
        from app import koth_scorer
        game, team, flag = self._seed_active_game(flag_value='good-uuid',
                                                  points=10)
        self._login(self.student)
        # 5 wrong attempts.
        for _ in range(5):
            r = self.client.post('/koth/api/flags/submit',
                                 json={'value': 'wrong-uuid'})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()['accepted'])
        # Score is still 0 — only accepted submissions count.
        self.assertEqual(
            koth_scorer.latest_scores(game.id).get(team.id, 0.0), 0.0)

    def test_wrong_value_records_audit_row_and_returns_false(self):
        game, team, flag = self._seed_active_game(flag_value='good-uuid')
        self._login(self.student)
        r = self.client.post('/koth/api/flags/submit',
                             json={'value': 'wrong-uuid'})
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertFalse(body['accepted'])

        rows = KothFlagSubmission.query.all()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].accepted)
        self.assertEqual(rows[0].submitted_by_user_id, self.student.id)
        self.assertEqual(rows[0].team_id, team.id)
        # Wrong submissions do NOT create flag claim events.
        self.assertEqual(KothClaimEvent.query.filter_by(
            game_id=game.id, event_type='flag').count(), 0)

    def test_duplicate_submission_rejected(self):
        game, team, flag = self._seed_active_game(flag_value='dup-uuid',
                                                  points=5)
        self._login(self.student)
        r1 = self.client.post('/koth/api/flags/submit',
                              json={'value': 'dup-uuid'})
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertTrue(r1.get_json()['accepted'])

        r2 = self.client.post('/koth/api/flags/submit',
                              json={'value': 'dup-uuid'})
        self.assertEqual(r2.status_code, 200, r2.data)
        body2 = r2.get_json()
        self.assertFalse(body2['accepted'])
        self.assertEqual(body2.get('reason'), 'duplicate')

        accepted = KothFlagSubmission.query.filter_by(
            flag_id=flag.id, accepted=True).count()
        self.assertEqual(accepted, 1, 'only the first submission should land')

    def test_one_team_one_accept_regardless_of_submission_count_limit(self):
        # User-confirmed semantic: a team can claim each flag's bonus exactly
        # once. The KothFlag.submission_count_limit column is dead weight —
        # the partial unique index uq_koth_flag_sub_accepted on
        # (flag_id, team_id) where accepted=True enforces one-per-team at the
        # DB level, and the route matches. Even setting limit=2 doesn't let a
        # team submit twice.
        game, team, flag = self._seed_active_game(
            flag_value='multi-uuid', points=2, submission_count_limit=2)
        self._login(self.student)
        r1 = self.client.post('/koth/api/flags/submit',
                              json={'value': 'multi-uuid'})
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertTrue(r1.get_json()['accepted'])
        # Second attempt rejected — the unique index makes this so even if
        # submission_count_limit said otherwise.
        r2 = self.client.post('/koth/api/flags/submit',
                              json={'value': 'multi-uuid'})
        self.assertEqual(r2.status_code, 200)
        body2 = r2.get_json()
        self.assertFalse(body2['accepted'])
        self.assertEqual(body2.get('reason'), 'duplicate')
        accepted = KothFlagSubmission.query.filter_by(
            flag_id=flag.id, accepted=True).count()
        self.assertEqual(accepted, 1, 'exactly one accept per (flag, team)')


if __name__ == '__main__':
    unittest.main()
