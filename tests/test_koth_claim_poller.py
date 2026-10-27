"""Unit tests for the KOTH claim-capture helpers in app.koth_scorer.

Covers `_parse_claim_token` (string parsing) and `_apply_claim` (ownership
flip + cooldown logic). `_apply_claim` is transport-agnostic: the same
state machine serves the guest-agent claim-file read and the TCP claim
beacon, distinguished only by the `source` recorded on the audit event.
The bigger `_poll_claims` orchestration is exercised in
`test_koth_scorer.py`; here we focus on the per-box state machine.
"""

import unittest
from datetime import datetime, timedelta

from app import create_app
from app.koth_scorer import _apply_claim, _parse_claim_token
from app.models import (
    db, KothBox, KothClaimEvent, KothGame, KothTeam, StudentGroup,
)


class ParseClaimTokenTests(unittest.TestCase):
    def test_parse_handles_empty_and_whitespace(self):
        self.assertIsNone(_parse_claim_token(None))
        self.assertIsNone(_parse_claim_token(''))
        self.assertIsNone(_parse_claim_token('  \n  '))
        self.assertIsNone(_parse_claim_token('\n\n\t\n'))

    def test_parse_returns_first_nonempty_line(self):
        # Plain single line (trimmed)
        self.assertEqual(_parse_claim_token('  abc-123  '), 'abc-123')
        # Multi-line: first non-empty wins
        self.assertEqual(_parse_claim_token('\n\n  tok-A  \nignored\n'), 'tok-A')


class ApplyClaimTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # A minimal active game with two teams + one box.
        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=550, internal_vlan_tag=551,
                             status='active', is_active=True,
                             claim_cooldown_sec=0)
        db.session.add(self.game)
        sg_a = StudentGroup(name='cp-a', display_name='A')
        sg_b = StudentGroup(name='cp-b', display_name='B')
        db.session.add_all([sg_a, sg_b])
        db.session.commit()
        self.team_a = KothTeam(game_id=self.game.id, student_group_id=sg_a.id,
                               color='red', submission_token='tok-A',
                               display_name='A')
        self.team_b = KothTeam(game_id=self.game.id, student_group_id=sg_b.id,
                               color='blue', submission_token='tok-B',
                               display_name='B')
        db.session.add_all([self.team_a, self.team_b])
        db.session.commit()
        self.box = KothBox(game_id=self.game.id, name='b1', ips=[],
                           services=[], owner_team_id=None,
                           claim_file_path='/koth/claim')
        db.session.add(self.box)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _claim_event_count(self):
        return KothClaimEvent.query.filter_by(
            game_id=self.game.id, box_id=self.box.id).count()

    def test_apply_no_op_on_empty_token(self):
        self.assertFalse(_apply_claim(self.box, None, 'file_poll', self.game))
        self.assertIsNone(self.box.owner_team_id)
        self.assertEqual(self._claim_event_count(), 0)
        self.assertFalse(
            _apply_claim(self.box, _parse_claim_token(''), 'file_poll', self.game))
        self.assertIsNone(self.box.owner_team_id)
        self.assertEqual(self._claim_event_count(), 0)

    def test_apply_no_op_on_unknown_token(self):
        with self.assertLogs('app.koth_scorer', level='DEBUG') as logs:
            result = _apply_claim(self.box, 'no-such-token', 'file_poll', self.game)
        self.assertFalse(result)
        self.assertIsNone(self.box.owner_team_id)
        self.assertEqual(self._claim_event_count(), 0)
        self.assertTrue(any('unknown token' in m for m in logs.output),
                        f'expected "unknown token" log, got {logs.output!r}')

    def test_apply_no_op_when_already_owner(self):
        self.box.owner_team_id = self.team_a.id
        db.session.commit()
        result = _apply_claim(self.box, 'tok-A', 'file_poll', self.game)
        self.assertFalse(result)
        self.assertEqual(self.box.owner_team_id, self.team_a.id)
        self.assertEqual(self._claim_event_count(), 0)

    def test_apply_flips_owner_with_no_cooldown(self):
        self.game.claim_cooldown_sec = 0
        db.session.commit()
        result = _apply_claim(self.box, 'tok-A', 'file_poll', self.game)
        self.assertTrue(result)
        # Commit so we can inspect the inserted event row.
        db.session.commit()
        self.assertEqual(self.box.owner_team_id, self.team_a.id)
        self.assertIsNotNone(self.box.claimed_at)
        events = KothClaimEvent.query.filter_by(
            game_id=self.game.id, box_id=self.box.id,
            event_type='claim').all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_id, self.team_a.id)
        self.assertIsNotNone(events[0].meta)
        self.assertEqual(events[0].meta.get('source'), 'file_poll')

    def test_apply_records_the_capturing_transport(self):
        # The audit row has to say which mechanism won, otherwise a game
        # run in 'both' mode gives no way to tell whether the beacon or
        # the file flipped a box.
        result = _apply_claim(self.box, 'tok-B', 'beacon', self.game)
        self.assertTrue(result)
        db.session.commit()
        event = KothClaimEvent.query.filter_by(
            game_id=self.game.id, box_id=self.box.id,
            event_type='claim').one()
        self.assertEqual(event.meta.get('source'), 'beacon')
        self.assertIsNone(event.meta.get('previous_owner_team_id'))

    def test_apply_respects_cooldown(self):
        self.game.claim_cooldown_sec = 60
        db.session.commit()
        # Pre-seed a recent claim within the cooldown window.
        prior = KothClaimEvent(
            game_id=self.game.id, box_id=self.box.id,
            team_id=self.team_b.id, event_type='claim',
            meta={'source': 'file_poll'},
            created_at=datetime.now() - timedelta(seconds=10),
        )
        db.session.add(prior)
        self.box.owner_team_id = self.team_b.id
        db.session.commit()
        result = _apply_claim(self.box, 'tok-A', 'file_poll', self.game)
        self.assertFalse(result)
        # Owner should NOT have flipped.
        self.assertEqual(self.box.owner_team_id, self.team_b.id)
        # Still only the one pre-seeded event.
        self.assertEqual(self._claim_event_count(), 1)

    def test_apply_succeeds_after_cooldown_elapses(self):
        self.game.claim_cooldown_sec = 60
        db.session.commit()
        prior = KothClaimEvent(
            game_id=self.game.id, box_id=self.box.id,
            team_id=self.team_b.id, event_type='claim',
            meta={'source': 'file_poll'},
            created_at=datetime.now() - timedelta(seconds=120),
        )
        db.session.add(prior)
        self.box.owner_team_id = self.team_b.id
        db.session.commit()
        result = _apply_claim(self.box, 'tok-A', 'file_poll', self.game)
        self.assertTrue(result)
        db.session.commit()
        self.assertEqual(self.box.owner_team_id, self.team_a.id)
        # Pre-seeded event + new flip event.
        self.assertEqual(self._claim_event_count(), 2)

    def test_cooldown_uses_the_latest_claim_not_the_first(self):
        # Regression: the batched `_last_claim_times` lookup must take
        # MAX(created_at) per box. Taking any other row lets a team that
        # claimed long ago keep a box permanently cooled down, or lets a
        # fresh claim slip through the window.
        self.game.claim_cooldown_sec = 60
        db.session.commit()
        db.session.add_all([
            KothClaimEvent(game_id=self.game.id, box_id=self.box.id,
                           team_id=self.team_a.id, event_type='claim',
                           created_at=datetime.now() - timedelta(seconds=600)),
            KothClaimEvent(game_id=self.game.id, box_id=self.box.id,
                           team_id=self.team_b.id, event_type='claim',
                           created_at=datetime.now() - timedelta(seconds=5)),
        ])
        self.box.owner_team_id = self.team_b.id
        db.session.commit()
        self.assertFalse(_apply_claim(self.box, 'tok-A', 'beacon', self.game))
        self.assertEqual(self.box.owner_team_id, self.team_b.id)


if __name__ == '__main__':
    unittest.main()
