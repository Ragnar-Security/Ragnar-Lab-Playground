"""Tests for KOTH SQLAlchemy models."""

import unittest
from sqlalchemy.exc import IntegrityError
from app import create_app
from app.models import (
    db, User, StudentGroup, VMRecord,
    KothGame, KothTeam, KothBox, KothFlag, KothFlagSubmission,
    KothClaimEvent, KothScoreSnapshot,
)


class KothModelTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin = User(login_id='admin', password_hash='x', is_admin=True)
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_group(self, name='team-a'):
        g = StudentGroup(name=name, display_name=name.title())
        db.session.add(g)
        db.session.commit()
        return g

    def test_create_game(self):
        g = KothGame(
            name='Example KOTH Game',
            deployment_group_name='Example KOTH Group',
            entry_vlan_tag=550,
            internal_vlan_tag=551,
            status='draft',
            created_by=self.admin.id,
        )
        db.session.add(g)
        db.session.commit()
        self.assertIsNotNone(g.id)
        self.assertFalse(g.is_active)
        self.assertFalse(g.pause_other_deployments)
        self.assertTrue(g.show_scoreboard)
        self.assertTrue(g.show_network_map)

    def test_only_one_active_game(self):
        g1 = KothGame(name='g1', deployment_group_name='x',
                      entry_vlan_tag=550, internal_vlan_tag=551,
                      status='active', is_active=True)
        db.session.add(g1)
        db.session.commit()
        g2 = KothGame(name='g2', deployment_group_name='y',
                      entry_vlan_tag=560, internal_vlan_tag=561,
                      status='active', is_active=True)
        db.session.add(g2)
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_team_color_unique_per_game(self):
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='draft')
        db.session.add(g)
        sg1, sg2 = self._make_group('a'), self._make_group('b')
        db.session.commit()
        db.session.add(KothTeam(game_id=g.id, student_group_id=sg1.id,
                                color='red', submission_token='t1', display_name='A'))
        db.session.commit()
        db.session.add(KothTeam(game_id=g.id, student_group_id=sg2.id,
                                color='red', submission_token='t2', display_name='B'))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_flag_submission_unique_per_team(self):
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='active', is_active=True)
        db.session.add(g)
        sg = self._make_group()
        db.session.commit()
        team = KothTeam(game_id=g.id, student_group_id=sg.id,
                        color='red', submission_token='tok', display_name='A')
        db.session.add(team)
        db.session.commit()
        flag = KothFlag(game_id=g.id, name='f1', value='secret', points=10)
        db.session.add(flag)
        db.session.commit()
        db.session.add(KothFlagSubmission(
            game_id=g.id, flag_id=flag.id, team_id=team.id, accepted=True))
        db.session.commit()
        db.session.add(KothFlagSubmission(
            game_id=g.id, flag_id=flag.id, team_id=team.id, accepted=True))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_vmrecord_has_koth_game_id(self):
        # Ensures the VMRecord migration column is wired.
        col = VMRecord.__table__.c.get('koth_game_id')
        self.assertIsNotNone(col)
        self.assertTrue(col.nullable)

    def test_vmrecord_koth_game_id_round_trip(self):
        from datetime import datetime, timedelta
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='draft')
        db.session.add(g); db.session.commit()
        vm = VMRecord(vmid=1234, vm_name='koth-vm', vm_type='qemu',
                      node='pve', template_name='t', student_name='admin',
                      expires_at=datetime.now() + timedelta(days=1),
                      koth_game_id=g.id)
        db.session.add(vm); db.session.commit()
        fetched = VMRecord.query.filter_by(koth_game_id=g.id).first()
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.vm_name, 'koth-vm')

    def test_to_dict_emits_offset_aware_iso_for_time_fields(self):
        # Regression, two halves of one bug:
        #   1. The deploy path wrote started_at with utcnow() while the
        #      scoring tick compared ends_at against now(), so in any
        #      non-UTC timezone a time-boxed game auto-ended a full tz
        #      offset late and left every VM running.
        #   2. to_dict() hid half of it by appending a bare 'Z' to a value
        #      that was not reliably UTC, so the browser countdown looked
        #      right while the server-side gate was wrong.
        # Both columns are now naive LOCAL (datetime.now(), the repo-wide
        # convention) and serialize with the host's real UTC offset.
        from datetime import datetime, timedelta
        started = datetime(2026, 5, 4, 5, 30, 0)
        ends = started + timedelta(minutes=2)
        ended = started + timedelta(minutes=10)
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551,
                     status='ended', is_active=False,
                     duration_minutes=2,
                     started_at=started, ends_at=ends, ended_at=ended)
        db.session.add(g); db.session.commit()
        d = g.to_dict()
        for field, value in (('started_at', started), ('ends_at', ends),
                             ('ended_at', ended)):
            parsed = datetime.fromisoformat(d[field])
            self.assertIsNotNone(
                parsed.tzinfo,
                f"{field} must carry a UTC offset so the browser lands on "
                f"the right instant, got {d[field]!r}")
            # Wall-clock reading unchanged; only the offset is added.
            self.assertEqual(parsed.replace(tzinfo=None), value)
            # And it names the same instant the stored local value does.
            self.assertEqual(parsed, value.astimezone())

    def test_relationships_load(self):
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='active', is_active=True)
        db.session.add(g)
        sg = self._make_group()
        db.session.commit()
        team = KothTeam(game_id=g.id, student_group_id=sg.id, color='red',
                        submission_token='tok', display_name='A')
        db.session.add(team); db.session.commit()
        flag = KothFlag(game_id=g.id, name='f', value='v', points=1)
        db.session.add(flag); db.session.commit()
        sub = KothFlagSubmission(game_id=g.id, flag_id=flag.id, team_id=team.id, accepted=True)
        db.session.add(sub); db.session.commit()
        ev = KothClaimEvent(game_id=g.id, team_id=team.id, event_type='flag')
        db.session.add(ev); db.session.commit()
        snap = KothScoreSnapshot(game_id=g.id, team_id=team.id, score=10.5, iteration=1)
        db.session.add(snap); db.session.commit()
        self.assertEqual(sub.team.color, 'red')
        self.assertEqual(sub.flag.name, 'f')
        self.assertEqual(ev.team.display_name, 'A')
        self.assertEqual(snap.team.id, team.id)
