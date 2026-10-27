import unittest
from app import create_app
from app.models import db, User, StudentGroup, KothGame


class KothAdminRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='admin', password_hash=generate_password_hash('p'),
                          is_admin=True)
        self.student = User(login_id='stud', password_hash=generate_password_hash('p'))
        db.session.add_all([self.admin, self.student])
        db.session.add(StudentGroup(name='team-a-3.1', display_name='Team A'))
        db.session.add(StudentGroup(name='team-b-3.1', display_name='Team B'))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def test_create_game_requires_admin(self):
        self._login(self.student)
        r = self.client.post('/koth/api/games', json={
            'name': 'g', 'deployment_group_name': 'd',
            'entry_vlan_tag': 550, 'internal_vlan_tag': 551,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 403)

    def test_create_game_succeeds(self):
        self._login(self.admin)
        r = self.client.post('/koth/api/games', json={
            'name': 'Spring KOTH', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 550, 'internal_vlan_tag': 551,
            'team_group_ids': [g.id for g in StudentGroup.query.all()],
        })
        self.assertEqual(r.status_code, 201, r.data)
        body = r.get_json()
        self.assertEqual(body['game']['status'], 'draft')
        self.assertEqual(len(body['teams']), 2)
        self.assertIn(body['teams'][0]['color'], ['red', 'blue'])

    def test_create_game_handles_22_teams(self):
        # Regression for the 8-color-palette bottleneck: previously the
        # create_game route did `KOTH_TEAM_COLORS[i % len(KOTH_TEAM_COLORS)]`,
        # so the 9th team got "red" again and tripped UNIQUE(game_id, color).
        # 22 teams was the immediate classroom-rollout target; the palette
        # now has 24 entries and the modulo wrap is gone.
        from app.config import KOTH_TEAM_COLORS
        self._login(self.admin)
        # Need 22 student groups — the two from setUp plus 20 more.
        for i in range(20):
            db.session.add(StudentGroup(
                name=f'team-large-{i}-3.7', display_name=f'Team L{i}'))
        db.session.commit()
        all_group_ids = [g.id for g in StudentGroup.query.all()]
        self.assertEqual(len(all_group_ids), 22)

        r = self.client.post('/koth/api/games', json={
            'name': 'Spring 2027 Big', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 555, 'internal_vlan_tag': 556,
            'team_group_ids': all_group_ids,
        })
        self.assertEqual(r.status_code, 201, r.data)
        body = r.get_json()
        self.assertEqual(len(body['teams']), 22)
        # All 22 colors must be distinct (the UNIQUE constraint enforces
        # this at the DB level too — we just want a friendly assertion if
        # someone shrinks the palette below 22 in a future change).
        colors = [t['color'] for t in body['teams']]
        self.assertEqual(len(set(colors)), 22, f'duplicate colors: {colors}')
        # Colors come from the palette in selection order.
        self.assertEqual(colors, KOTH_TEAM_COLORS[:22])

    def test_create_game_rejects_more_teams_than_colors(self):
        from app.config import KOTH_TEAM_COLORS
        self._login(self.admin)
        # Seed enough groups to exceed the palette by one.
        target = len(KOTH_TEAM_COLORS) + 1
        # We start with 2 from setUp; add the rest.
        for i in range(target - 2):
            db.session.add(StudentGroup(
                name=f'overflow-{i}-3.7', display_name=f'Overflow {i}'))
        db.session.commit()
        all_group_ids = [g.id for g in StudentGroup.query.all()]
        self.assertEqual(len(all_group_ids), target)

        r = self.client.post('/koth/api/games', json={
            'name': 'overflow', 'deployment_group_name': 'X',
            'entry_vlan_tag': 565, 'internal_vlan_tag': 566,
            'team_group_ids': all_group_ids,
        })
        self.assertEqual(r.status_code, 400, r.data)
        body = r.get_json()
        self.assertIn('too many teams', body['error'])
        self.assertEqual(body['max_teams'], len(KOTH_TEAM_COLORS))
        # No partial KothGame inserted.
        self.assertEqual(KothGame.query.count(), 0)

    def test_only_one_active_game(self):
        self._login(self.admin)
        g1 = KothGame(name='g1', deployment_group_name='x',
                      entry_vlan_tag=550, internal_vlan_tag=551,
                      status='active', is_active=True)
        db.session.add(g1)
        db.session.commit()
        r = self.client.post('/koth/api/games', json={
            'name': 'g2', 'deployment_group_name': 'y',
            'entry_vlan_tag': 560, 'internal_vlan_tag': 561,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 409)

    def test_create_after_ending_previous_game(self):
        # Regression: the original singleton index was UNIQUE(is_active),
        # which on MySQL also disallowed a second is_active=0 row. So once
        # any game ended (is_active=0), no further drafts could be created
        # — surfaced as `Duplicate entry '0' for key uq_koth_active_singleton`.
        # The fix uses an app-managed `active_singleton` column that is NULL
        # when inactive, so multiple ended/draft games coexist freely.
        self._login(self.admin)
        ended = KothGame(name='old', deployment_group_name='x',
                         entry_vlan_tag=550, internal_vlan_tag=551,
                         status='ended', is_active=False)
        db.session.add(ended); db.session.commit()
        another_draft = KothGame(name='leftover-draft', deployment_group_name='y',
                                 entry_vlan_tag=552, internal_vlan_tag=553,
                                 status='draft', is_active=False)
        db.session.add(another_draft); db.session.commit()
        # Now hit the route — it must succeed even with two existing
        # is_active=False rows already in the table.
        r = self.client.post('/koth/api/games', json={
            'name': 'fresh', 'deployment_group_name': 'z',
            'entry_vlan_tag': 560, 'internal_vlan_tag': 561,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 201, r.data)
        # The new create_game wipes prior 'ended' games before inserting the
        # fresh draft, so the ended row from above is gone. We expect:
        #   leftover-draft (status='draft', untouched) + fresh (status='draft')
        # = 2 rows total; the old 'old' (ended) game is wiped.
        self.assertEqual(KothGame.query.count(), 2)
        self.assertEqual(KothGame.query.filter_by(status='ended').count(), 0)

    def test_patch_settings(self):
        self._login(self.admin)
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551,
                     status='draft')
        db.session.add(g); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}', json={
            'pause_other_deployments': True, 'show_scoreboard': False,
        })
        self.assertEqual(r.status_code, 200)
        db.session.refresh(g)
        self.assertTrue(g.pause_other_deployments)
        self.assertFalse(g.show_scoreboard)

    # -------------------------------------------------------------------
    # New: create_game wipes prior ended games (and their child rows)
    # -------------------------------------------------------------------
    def test_create_wipes_prior_ended_games(self):
        from app.models import KothBox, KothScoreSnapshot, KothClaimEvent, KothTeam
        self._login(self.admin)
        sg = StudentGroup.query.first()
        # Seed two ended games each with a snapshot + claim event child row.
        for i in range(2):
            g = KothGame(name=f'old-{i}', deployment_group_name='x',
                         entry_vlan_tag=600 + i*2, internal_vlan_tag=601 + i*2,
                         status='ended', is_active=False)
            db.session.add(g); db.session.flush()
            t = KothTeam(game_id=g.id, student_group_id=sg.id, color='red',
                         submission_token=f'tok-old-{i}', display_name='X')
            db.session.add(t); db.session.flush()
            db.session.add(KothScoreSnapshot(
                game_id=g.id, team_id=t.id, score=1.0, iteration=1))
            db.session.add(KothClaimEvent(
                game_id=g.id, team_id=t.id, event_type='claim'))
        db.session.commit()
        self.assertEqual(KothGame.query.filter_by(status='ended').count(), 2)
        self.assertEqual(KothScoreSnapshot.query.count(), 2)
        self.assertEqual(KothClaimEvent.query.count(), 2)

        r = self.client.post('/koth/api/games', json={
            'name': 'fresh', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 550, 'internal_vlan_tag': 551,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(KothGame.query.filter_by(status='ended').count(), 0)
        self.assertEqual(KothScoreSnapshot.query.count(), 0)
        self.assertEqual(KothClaimEvent.query.count(), 0)
        # New draft survives
        self.assertEqual(KothGame.query.filter_by(status='draft').count(), 1)

    def test_create_wipe_deletes_vmrecords_too(self):
        # Per project policy, KOTH game history is not preserved across
        # games. The cascade-wipe must drop VMRecord rows too — both because
        # the admin doesn't want to see them in any history view AND because
        # vm_records.koth_game_id has an FK to koth_games.id with no ON
        # DELETE action, so leaving them blocks the parent delete with
        # MySQL errno 1451.
        #
        # Also covers a SECOND FK chain that bit us: koth_boxes.vm_record_id
        # also has no ON DELETE, so deleting VMRecord before KothBox blows
        # up the same way. The wipe must order KothBox before VMRecord.
        from app.models import VMRecord, KothBox
        from datetime import datetime, timedelta
        self._login(self.admin)
        ended = KothGame(name='old', deployment_group_name='x',
                         entry_vlan_tag=600, internal_vlan_tag=601,
                         status='ended', is_active=False)
        db.session.add(ended); db.session.flush()
        vm = VMRecord(
            vmid=9999, vm_name='koth-old-box1', vm_type='qemu', node='pve',
            template_name='x/box1', student_name='admin',
            memory=2048, cores=2, storage=20,
            koth_game_id=ended.id, destroyed=True,
            destroyed_at=datetime.now(),
            expires_at=datetime.now() + timedelta(days=1),
        )
        db.session.add(vm); db.session.flush()
        # KothBox referencing the VMRecord — this is what was missing in the
        # earlier test, and it's the FK chain that triggered the second 500.
        box = KothBox(game_id=ended.id, vm_record_id=vm.id, name='box1',
                      ips=['10.0.0.5'], services=[])
        db.session.add(box); db.session.commit()
        vm_id = vm.id
        box_id = box.id

        r = self.client.post('/koth/api/games', json={
            'name': 'fresh', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 550, 'internal_vlan_tag': 551,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 201, r.data)
        # Ended game gone AND its KothBox + VMRecord gone — no history kept.
        self.assertEqual(KothGame.query.filter_by(status='ended').count(), 0)
        self.assertIsNone(db.session.get(VMRecord, vm_id),
                          "VMRecord for ended KOTH game must be wiped")
        self.assertIsNone(db.session.get(KothBox, box_id),
                          "KothBox for ended KOTH game must be wiped")

    def test_refresh_box_ips_populates_from_guest_agent(self):
        # Regression: KothBox.ips starts empty (VMRecord.ip_address is None
        # at clone time, before the VM has booted/got DHCP). Without an
        # IP-refresh path the source-IP claim verification has nothing to
        # match against and rejects every claim with 403.
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from app.models import VMRecord, KothBox
        from app.routes.koth_admin import _refresh_box_ips
        self._login(self.admin)
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551,
                     status='active', is_active=True)
        db.session.add(g); db.session.flush()
        vm = VMRecord(
            vmid=9100, vm_name='koth-g-final-dc', vm_type='qemu', node='pve',
            template_name='x/final-dc', student_name=str(g.id),
            memory=2048, cores=2, storage=20,
            koth_game_id=g.id, destroyed=False,
            expires_at=datetime.now() + timedelta(days=1),
        )
        db.session.add(vm); db.session.flush()
        box = KothBox(game_id=g.id, vm_record_id=vm.id, name='final-dc',
                      ips=[], services=[])
        db.session.add(box); db.session.commit()
        # Mock guest-agent helper to return the box's "live" IPs. Both
        # names are lazily imported inside _refresh_box_ips, so patch at
        # the definition site, not the import site.
        with patch('app.routes.utils._get_vm_ip_addresses',
                   return_value=['10.151.2.160']), \
             patch('app.proxmox_service.get_proxmox_connection',
                   return_value=object()):
            updated = _refresh_box_ips(g.id)
        self.assertEqual(updated, 1)
        db.session.refresh(box)
        self.assertEqual(box.ips, ['10.151.2.160'])

    def test_refresh_box_ips_skips_destroyed_vms(self):
        # Don't waste a Proxmox round-trip on VMs that are already gone.
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from app.models import VMRecord, KothBox
        from app.routes.koth_admin import _refresh_box_ips
        self._login(self.admin)
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=552, internal_vlan_tag=553,
                     status='active', is_active=True)
        db.session.add(g); db.session.flush()
        vm = VMRecord(
            vmid=9200, vm_name='dead', vm_type='qemu', node='pve',
            template_name='x/dead', student_name=str(g.id),
            memory=2048, cores=2, storage=20,
            koth_game_id=g.id, destroyed=True,
            destroyed_at=datetime.now(),
            expires_at=datetime.now() + timedelta(days=1),
        )
        db.session.add(vm); db.session.flush()
        box = KothBox(game_id=g.id, vm_record_id=vm.id, name='dead',
                      ips=[], services=[])
        db.session.add(box); db.session.commit()
        with patch('app.routes.utils._get_vm_ip_addresses') as mock_ips, \
             patch('app.proxmox_service.get_proxmox_connection',
                   return_value=object()):
            _refresh_box_ips(g.id)
        mock_ips.assert_not_called()

    def test_destroy_game_vms_sweeps_stranded_proxmox_vms(self):
        # Regression: a SIGTERM'd deploy worker leaves VMs in Proxmox with
        # NO matching VMRecord row (the rows are inserted as each clone
        # completes; if the worker dies first, no record exists). The
        # cleanup helper now also sweeps Proxmox by name prefix to catch
        # these.
        from unittest.mock import patch
        from app.routes.koth_admin import _destroy_game_vms
        self._login(self.admin)
        g = KothGame(name='partial', deployment_group_name='x',
                     entry_vlan_tag=700, internal_vlan_tag=701,
                     status='deploying', is_active=False)
        db.session.add(g); db.session.commit()
        # No VMRecord rows — the SIGTERM happened before any landed.
        # Mock the Proxmox sweep to report two stranded VMs and assert
        # cleanup_vms is called with both.
        stranded = [
            {'vmid': 9501, 'node': 'pve', 'vm_type': 'qemu',
             'name': f'koth-{g.id}-final-dc'},
            {'vmid': 9502, 'node': 'pve2', 'vm_type': 'qemu',
             'name': f'koth-{g.id}-final-app-portal'},
        ]
        # Note: find_vms_by_name_prefix is a lazy import inside
        # _destroy_game_vms, so patch it at its definition site
        # (app.proxmox_service), not at koth_admin's import site.
        # cleanup_vms now returns the set of vmids actually destroyed;
        # simulate a successful cleanup by echoing back every attempted vmid.
        def fake_cleanup(payload):
            return {item['vmid'] for item in payload}

        with patch('app.proxmox_service.find_vms_by_name_prefix', return_value=stranded), \
             patch('app.routes.koth_admin.cleanup_vms', side_effect=fake_cleanup) as mock_cleanup:
            n = _destroy_game_vms(g.id)
        self.assertEqual(n, 2)
        mock_cleanup.assert_called_once()
        called_with = mock_cleanup.call_args[0][0]
        # Both stranded VMs must be in the cleanup payload.
        self.assertEqual({(v['vmid'], v['node']) for v in called_with},
                         {(9501, 'pve'), (9502, 'pve2')})
        self.assertEqual(
            {v['vmid']: v['vm_name'] for v in called_with},
            {
                9501: f'koth-{g.id}-final-dc',
                9502: f'koth-{g.id}-final-app-portal',
            },
        )

    def test_create_does_not_wipe_active_or_draft(self):
        self._login(self.admin)
        active = KothGame(name='live', deployment_group_name='x',
                          entry_vlan_tag=560, internal_vlan_tag=561,
                          status='active', is_active=True)
        draft = KothGame(name='pending', deployment_group_name='y',
                         entry_vlan_tag=562, internal_vlan_tag=563,
                         status='draft', is_active=False)
        db.session.add_all([active, draft]); db.session.commit()
        active_id, draft_id = active.id, draft.id

        r = self.client.post('/koth/api/games', json={
            'name': 'attempt', 'deployment_group_name': 'z',
            'entry_vlan_tag': 570, 'internal_vlan_tag': 571,
            'team_group_ids': [],
        })
        self.assertEqual(r.status_code, 409)
        # Pre-existing rows still present
        self.assertIsNotNone(db.session.get(KothGame, active_id))
        self.assertIsNotNone(db.session.get(KothGame, draft_id))

    # -------------------------------------------------------------------
    # New: duration_minutes accepted on create
    # -------------------------------------------------------------------
    def test_create_accepts_duration_minutes(self):
        self._login(self.admin)
        r = self.client.post('/koth/api/games', json={
            'name': 'a', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 580, 'internal_vlan_tag': 581,
            'team_group_ids': [],
            'duration_minutes': 120,
        })
        self.assertEqual(r.status_code, 201, r.data)
        g1_id = r.get_json()['game']['id']
        g1 = db.session.get(KothGame, g1_id)
        self.assertEqual(g1.duration_minutes, 120)
        # Wipe + try null. Need to delete g1 first since it's a draft holding
        # the same VLANs (and create_game won't wipe drafts).
        db.session.delete(g1); db.session.commit()
        r2 = self.client.post('/koth/api/games', json={
            'name': 'b', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 582, 'internal_vlan_tag': 583,
            'team_group_ids': [],
            'duration_minutes': None,
        })
        self.assertEqual(r2.status_code, 201, r2.data)
        g2 = db.session.get(KothGame, r2.get_json()['game']['id'])
        self.assertIsNone(g2.duration_minutes)

    def test_create_rejects_bad_duration(self):
        self._login(self.admin)
        base = {
            'name': 'a', 'deployment_group_name': 'KOTH',
            'entry_vlan_tag': 584, 'internal_vlan_tag': 585,
            'team_group_ids': [],
        }
        for bad in ('abc', 0, -5):
            r = self.client.post('/koth/api/games', json={**base, 'duration_minutes': bad})
            self.assertEqual(r.status_code, 400, f'bad value {bad!r}: {r.data}')

    # -------------------------------------------------------------------
    # New: PATCH duration / extend_minutes recompute ends_at
    # -------------------------------------------------------------------
    def _make_active_game(self, *, duration_minutes=60, started_at=None):
        from datetime import datetime
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=590, internal_vlan_tag=591,
                     status='draft',
                     duration_minutes=duration_minutes)
        db.session.add(g); db.session.commit()
        # Flip to active manually so we don't run the full deploy path.
        g.status = 'active'
        g.is_active = True
        g.started_at = started_at or datetime.now()
        db.session.commit()
        return g

    def test_patch_extend_minutes_adds_to_duration(self):
        from datetime import timedelta
        self._login(self.admin)
        g = self._make_active_game(duration_minutes=60)
        started_at = g.started_at
        r = self.client.patch(f'/koth/api/games/{g.id}', json={'extend_minutes': 15})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(g)
        self.assertEqual(g.duration_minutes, 75)
        expected = started_at + timedelta(minutes=75)
        self.assertIsNotNone(g.ends_at)
        self.assertAlmostEqual((g.ends_at - expected).total_seconds(), 0, delta=2)

    def test_patch_duration_minutes_recomputes_ends_at(self):
        from datetime import timedelta
        self._login(self.admin)
        g = self._make_active_game(duration_minutes=60)
        started_at = g.started_at
        r = self.client.patch(f'/koth/api/games/{g.id}', json={'duration_minutes': 30})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(g)
        self.assertEqual(g.duration_minutes, 30)
        expected = started_at + timedelta(minutes=30)
        self.assertAlmostEqual((g.ends_at - expected).total_seconds(), 0, delta=2)

    def test_patch_extend_on_unlimited_starts_timebox(self):
        from datetime import timedelta
        self._login(self.admin)
        g = self._make_active_game(duration_minutes=None)
        started_at = g.started_at
        r = self.client.patch(f'/koth/api/games/{g.id}', json={'extend_minutes': 10})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(g)
        self.assertEqual(g.duration_minutes, 10)
        expected = started_at + timedelta(minutes=10)
        self.assertAlmostEqual((g.ends_at - expected).total_seconds(), 0, delta=2)

    # -------------------------------------------------------------------
    # New: force-reset
    # -------------------------------------------------------------------
    def test_force_reset_unsticks_deploying(self):
        from unittest.mock import patch
        self._login(self.admin)
        g = KothGame(name='stuck', deployment_group_name='x',
                     entry_vlan_tag=592, internal_vlan_tag=593,
                     status='deploying', is_active=False)
        db.session.add(g); db.session.commit()
        # Mock the Proxmox stranded-VM sweep so the test doesn't hit the
        # real cluster (the helper is now invoked by _destroy_game_vms).
        with patch('app.proxmox_service.find_vms_by_name_prefix', return_value=[]):
            r = self.client.post(f'/koth/api/games/{g.id}/force-reset')
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertEqual(body['destroyed_vms'], 0)
        db.session.refresh(g)
        self.assertEqual(g.status, 'draft')

    def test_force_reset_rejects_non_deploying_games(self):
        self._login(self.admin)
        for status, is_active in [('draft', False), ('active', True), ('ended', False)]:
            g = KothGame(name=f'g-{status}', deployment_group_name='x',
                         entry_vlan_tag=600 + hash(status) % 50,
                         internal_vlan_tag=650 + hash(status) % 50,
                         status=status, is_active=is_active)
            db.session.add(g); db.session.commit()
            r = self.client.post(f'/koth/api/games/{g.id}/force-reset')
            self.assertEqual(r.status_code, 409, f'{status} should reject: {r.data}')
            # Restore so other iterations don't trip the active-singleton index
            if is_active:
                g.is_active = False
                db.session.commit()

    def test_force_reset_destroys_partial_vms(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from app.models import VMRecord
        self._login(self.admin)
        g = KothGame(name='partial', deployment_group_name='x',
                     entry_vlan_tag=540, internal_vlan_tag=541,
                     status='deploying', is_active=False)
        db.session.add(g); db.session.commit()
        vm = VMRecord(
            vmid=99001, vm_name='partial-box',
            vm_type='qemu', node='pve',
            template_name='KOTH/box1',
            student_name=str(g.id),
            memory=2048, cores=2, storage=20,
            koth_game_id=g.id,
            destroyed=False,
            expires_at=datetime.now() + timedelta(days=1),
        )
        db.session.add(vm); db.session.commit()

        # cleanup_vms now returns the set of vmids actually destroyed;
        # echo every attempted vmid back to simulate a successful cleanup.
        with patch('app.routes.koth_admin.cleanup_vms',
                   side_effect=lambda payload: {item['vmid'] for item in payload}), \
             patch('app.proxmox_service.find_vms_by_name_prefix', return_value=[]):
            r = self.client.post(f'/koth/api/games/{g.id}/force-reset')
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertEqual(body['destroyed_vms'], 1)
        db.session.refresh(vm)
        self.assertTrue(vm.destroyed)
        db.session.refresh(g)
        self.assertEqual(g.status, 'draft')

    # -------------------------------------------------------------------
    # New: single-flag add allowed in draft, rejected on ended
    # -------------------------------------------------------------------
    def test_add_flag_works_in_draft(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='draft-flag', deployment_group_name='x',
                     entry_vlan_tag=720, internal_vlan_tag=721,
                     status='draft', is_active=False)
        db.session.add(g); db.session.commit()
        r = self.client.post(f'/koth/api/games/{g.id}/flags', json={
            'name': 'F1', 'value': 'v1', 'points': 5,
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(KothFlag.query.filter_by(game_id=g.id).count(), 1)

    def test_add_flag_rejected_on_ended_game(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='ended-flag', deployment_group_name='x',
                     entry_vlan_tag=722, internal_vlan_tag=723,
                     status='ended', is_active=False)
        db.session.add(g); db.session.commit()
        r = self.client.post(f'/koth/api/games/{g.id}/flags', json={
            'name': 'F1', 'value': 'v1', 'points': 5,
        })
        self.assertEqual(r.status_code, 409, r.data)
        body = r.get_json()
        self.assertIn('cannot add flags to an ended game', body['error'])
        self.assertEqual(KothFlag.query.filter_by(game_id=g.id).count(), 0)

    def test_current_game_returns_flags_and_boxes(self):
        # Regression: the admin UI lists existing flags from the
        # current_game payload (no separate fetch). Assert the payload
        # includes both the flags array (with name + box_name resolved)
        # and the minimal boxes array used by the edit-flag dropdown.
        from app.models import KothFlag, KothBox
        self._login(self.admin)
        g = KothGame(name='cur-payload', deployment_group_name='x',
                     entry_vlan_tag=730, internal_vlan_tag=731,
                     status='draft', is_active=False)
        db.session.add(g); db.session.flush()
        b = KothBox(game_id=g.id, name='final-dc', ips=['10.0.0.10'],
                    services=[])
        db.session.add(b); db.session.flush()
        db.session.add(KothFlag(game_id=g.id, name='Global F',
                                value='glob-uuid', points=5))
        db.session.add(KothFlag(game_id=g.id, name='DC root',
                                value='dc-uuid', points=10, box_id=b.id))
        db.session.commit()
        r = self.client.get('/koth/api/games/current')
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertEqual(len(body['flags']), 2)
        global_flag = next(f for f in body['flags'] if f['name'] == 'Global F')
        self.assertIsNone(global_flag['box_id'])
        self.assertIsNone(global_flag['box_name'])
        dc_flag = next(f for f in body['flags'] if f['name'] == 'DC root')
        self.assertEqual(dc_flag['box_id'], b.id)
        self.assertEqual(dc_flag['box_name'], 'final-dc')
        self.assertEqual(body['boxes'], [{'id': b.id, 'name': 'final-dc'}])

    def test_patch_flag_edits_fields(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='patch-flag', deployment_group_name='x',
                     entry_vlan_tag=740, internal_vlan_tag=741,
                     status='draft', is_active=False)
        db.session.add(g); db.session.flush()
        f = KothFlag(game_id=g.id, name='Old', value='old-val', points=5)
        db.session.add(f); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}', json={
            'name': 'New', 'value': 'new-val', 'points': 12,
        })
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(f)
        self.assertEqual(f.name, 'New')
        self.assertEqual(f.value, 'new-val')
        self.assertEqual(f.points, 12.0)

    def test_patch_flag_resolves_box_name(self):
        from app.models import KothFlag, KothBox
        self._login(self.admin)
        g = KothGame(name='patch-flag-box', deployment_group_name='x',
                     entry_vlan_tag=742, internal_vlan_tag=743,
                     status='draft', is_active=False)
        db.session.add(g); db.session.flush()
        b = KothBox(game_id=g.id, name='final-web', ips=[], services=[])
        db.session.add(b); db.session.flush()
        f = KothFlag(game_id=g.id, name='F', value='v', points=1)
        db.session.add(f); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}',
                              json={'box_name': 'final-web'})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(f)
        self.assertEqual(f.box_id, b.id)
        # And clearing it back to global.
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}',
                              json={'box_name': None})
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(f)
        self.assertIsNone(f.box_id)

    def test_patch_flag_rejects_unknown_box_name(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='patch-flag-bad', deployment_group_name='x',
                     entry_vlan_tag=744, internal_vlan_tag=745,
                     status='draft', is_active=False)
        db.session.add(g); db.session.flush()
        f = KothFlag(game_id=g.id, name='F', value='v', points=1)
        db.session.add(f); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}',
                              json={'box_name': 'does-not-exist'})
        self.assertEqual(r.status_code, 400, r.data)
        db.session.refresh(f)
        self.assertIsNone(f.box_id)

    def test_patch_flag_rejects_negative_points(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='patch-flag-pts', deployment_group_name='x',
                     entry_vlan_tag=746, internal_vlan_tag=747,
                     status='draft', is_active=False)
        db.session.add(g); db.session.flush()
        f = KothFlag(game_id=g.id, name='F', value='v', points=1)
        db.session.add(f); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}',
                              json={'points': -3})
        self.assertEqual(r.status_code, 400, r.data)

    def test_patch_flag_rejected_on_ended_game(self):
        from app.models import KothFlag
        self._login(self.admin)
        g = KothGame(name='ended-edit', deployment_group_name='x',
                     entry_vlan_tag=748, internal_vlan_tag=749,
                     status='ended', is_active=False)
        db.session.add(g); db.session.flush()
        f = KothFlag(game_id=g.id, name='F', value='v', points=1)
        db.session.add(f); db.session.commit()
        r = self.client.patch(f'/koth/api/games/{g.id}/flags/{f.id}',
                              json={'name': 'X'})
        self.assertEqual(r.status_code, 409, r.data)

    def test_rotate_token(self):
        self._login(self.admin)
        from app.models import KothTeam
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='draft')
        db.session.add(g); db.session.commit()
        sg = StudentGroup.query.first()
        team = KothTeam(game_id=g.id, student_group_id=sg.id, color='red',
                        submission_token='old-3.1', display_name='A')
        db.session.add(team); db.session.commit()
        r = self.client.post(f'/koth/api/games/{g.id}/teams/{team.id}/rotate-token')
        self.assertEqual(r.status_code, 200)
        db.session.refresh(team)
        self.assertNotEqual(team.submission_token, 'old-3.1')


class KothDeployTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='admin-deploy', password_hash=generate_password_hash('p'),
                          is_admin=True)
        db.session.add(self.admin); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _login(self, u):
        with self.client.session_transaction() as s:
            s['user_id'] = u.id
            s['session_version'] = u.session_version

    def test_deploy_clones_each_template(self):
        from unittest.mock import patch
        from app.models import KothBox, VMRecord
        from datetime import datetime, timedelta
        self._login(self.admin)
        g = KothGame(name='g', deployment_group_name='KOTH',
                     entry_vlan_tag=550, internal_vlan_tag=551, status='draft')
        db.session.add(g); db.session.commit()
        fake_cfg = {'deployment_groups': [{
            'display_name': 'KOTH', 'enabled': True, 'koth_mode': True,
            'uptime_minutes': 240,
            'templates': [
                {'name': 'box1', 'type': 'qemu',
                 'memory': 2048, 'cores': 2,
                 'koth_services': [{'name': 'SSH', 'port': 22}],
                 'template_locations': {'pve': 9501, 'pve2': 9601}},
                {'name': 'box2', 'type': 'qemu',
                 'memory': 2048, 'cores': 2,
                 'template_locations': {'pve': 9502, 'pve2': 9602}},
            ],
        }]}
        # Stub deploy_bundle so we don't touch Proxmox; just synthesize the
        # VMRecords the real call would have produced. KOTH's deploy() does
        # not depend on the orchestration internals, only on the returned
        # records.
        def fake_deploy_bundle(**kw):
            recs = []
            for i, t in enumerate(kw['templates']):
                rec = VMRecord(
                    vmid=9000 + i,
                    vm_name=f"koth-{kw['koth_game_id']}-{t['name']}",
                    vm_type=t.get('type', 'qemu'),
                    node='pve',
                    template_name=f"{kw['deployment_group_name']}/{t['name']}",
                    student_name=kw['owning_identifier'],
                    memory=t.get('memory', 2048),
                    cores=t.get('cores', 2),
                    storage=t.get('storage', 20),
                    koth_game_id=kw['koth_game_id'],
                    is_entry_point=t.get('entry_point', False),
                    expires_at=datetime.now() + timedelta(days=1),
                )
                db.session.add(rec)
                db.session.commit()
                recs.append(rec)
            return recs
        with patch('app.routes.koth_admin.load_template_config', return_value=fake_cfg), \
             patch('app.routes.koth_admin.deploy_bundle', side_effect=fake_deploy_bundle):
            r = self.client.post(f'/koth/api/games/{g.id}/deploy')
        self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(g)
        self.assertEqual(g.status, 'active')
        self.assertTrue(g.is_active)
        boxes = KothBox.query.filter_by(game_id=g.id).all()
        self.assertEqual(sorted(b.name for b in boxes), ['box1', 'box2'])
        # Box1 should have its koth_services configured (status defaults to 'closed')
        b1 = next(b for b in boxes if b.name == 'box1')
        self.assertEqual(b1.services, [{'name': 'SSH', 'port': 22, 'status': 'closed'}])
        # Layout positions assigned
        for b in boxes:
            self.assertIsNotNone(b.position)
            self.assertIn('x', b.position)
            self.assertIn('y', b.position)
