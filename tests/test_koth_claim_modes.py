"""Tests for how `_poll_claims` chooses and uses a claim transport.

`KothGame.claim_mode` picks between the Proxmox guest-agent claim file
(one API call per box per tick) and the TCP claim beacon (zero API calls —
it rides the service probe the scorer already makes). The cost difference
is the whole point, so these tests assert on call counts, not just on the
resulting ownership.
"""

import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app, koth_scorer
from app.models import db, KothBox, KothGame, KothTeam, StudentGroup, VMRecord


class ClaimModeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        koth_scorer._claim_read_backoff.clear()
        koth_scorer._last_ip_refresh.clear()

        self.game = KothGame(name='g', deployment_group_name='x',
                             entry_vlan_tag=560, internal_vlan_tag=561,
                             status='active', is_active=True,
                             claim_cooldown_sec=0, claim_port=31337)
        db.session.add(self.game)
        sg = StudentGroup(name='cm-a', display_name='A')
        db.session.add(sg)
        db.session.commit()
        self.team = KothTeam(game_id=self.game.id, student_group_id=sg.id,
                             color='red', submission_token='tok-A',
                             display_name='A')
        db.session.add(self.team)
        vm = VMRecord(vmid=9001, vm_name='b1', vm_type='qemu', node='pve',
                      template_name='x/b1', student_name='s',
                      expires_at=datetime.now() + timedelta(hours=3))
        db.session.add(vm)
        db.session.commit()
        self.box = KothBox(game_id=self.game.id, name='b1',
                           vm_record_id=vm.id, ips=['10.0.0.5'],
                           services=[], owner_team_id=None,
                           claim_file_path='/koth/claim')
        db.session.add(self.box)
        db.session.commit()

    def tearDown(self):
        koth_scorer._claim_read_backoff.clear()
        koth_scorer._last_ip_refresh.clear()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # -- transport selection --------------------------------------------
    def test_beacon_mode_makes_no_proxmox_calls(self):
        self.game.claim_mode = 'beacon'
        db.session.commit()
        with patch('app.koth_scorer.probe_beacon',
                   return_value=('open', 'tok-A')) as beacon, \
             patch('app.koth_scorer._read_claim_files') as read_files:
            flips = koth_scorer._poll_claims(self.game, [self.box])
        self.assertEqual(flips, 1)
        read_files.assert_not_called()
        beacon.assert_called_once()
        self.assertEqual(beacon.call_args[0][0], '10.0.0.5')
        self.assertEqual(beacon.call_args[0][1], 31337)
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)

    def test_file_mode_does_not_touch_the_beacon_port(self):
        self.game.claim_mode = 'file'
        db.session.commit()
        with patch('app.koth_scorer.probe_beacon') as beacon, \
             patch('app.proxmox_service.get_proxmox_connection'), \
             patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=['tok-A\n']):
            flips = koth_scorer._poll_claims(self.game, [self.box])
        self.assertEqual(flips, 1)
        beacon.assert_not_called()
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)

    def test_both_mode_prefers_the_beacon(self):
        # A live listener is stronger evidence the team still holds the
        # box than a file somebody wrote once, so the beacon wins.
        sg = StudentGroup(name='cm-b', display_name='B')
        db.session.add(sg)
        db.session.commit()
        other = KothTeam(game_id=self.game.id, student_group_id=sg.id,
                         color='blue', submission_token='tok-B',
                         display_name='B')
        db.session.add(other)
        self.game.claim_mode = 'both'
        db.session.commit()
        with patch('app.koth_scorer.probe_beacon',
                   return_value=('open', 'tok-A')), \
             patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=['tok-B\n']):
            koth_scorer._poll_claims(self.game, [self.box])
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)

    def test_both_mode_falls_back_to_the_file_when_no_beacon(self):
        self.game.claim_mode = 'both'
        db.session.commit()
        with patch('app.koth_scorer.probe_beacon',
                   return_value=('closed', None)), \
             patch('app.proxmox_service.ProxmoxAPI'), \
             patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=['tok-A\n']):
            koth_scorer._poll_claims(self.game, [self.box])
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)

    def test_an_up_but_silent_beacon_does_not_clear_ownership(self):
        # A team that already owns a box keeps it if the listener answers
        # but serves nothing; only another team's token takes it away.
        self.box.owner_team_id = self.team.id
        self.game.claim_mode = 'beacon'
        db.session.commit()
        with patch('app.koth_scorer.probe_beacon', return_value=('open', None)):
            flips = koth_scorer._poll_claims(self.game, [self.box])
        self.assertEqual(flips, 0)
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)

    # -- guest-agent backoff --------------------------------------------
    def test_failed_guest_reads_back_off_instead_of_retrying_every_tick(self):
        # A powered-off box used to cost a full Proxmox round trip (and
        # its timeout) on every single tick, forever.
        self.game.claim_mode = 'file'
        db.session.commit()
        with patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=[None]) as reads, \
             patch('app.proxmox_service.get_proxmox_connection'):
            koth_scorer._poll_claims(self.game, [self.box])
            self.assertEqual(reads.call_count, 1)
            # Second tick immediately after: the box is in backoff, so it
            # is not even included in a batch.
            koth_scorer._poll_claims(self.game, [self.box])
            self.assertEqual(reads.call_count, 1,
                             'box in backoff must not be re-read')

    def test_backoff_expires_and_the_box_is_retried(self):
        self.game.claim_mode = 'file'
        db.session.commit()
        with patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=[None]), \
             patch('app.proxmox_service.get_proxmox_connection'):
            koth_scorer._poll_claims(self.game, [self.box])
        self.assertIn(self.box.id, koth_scorer._claim_read_backoff)
        # Rewind the deadline rather than sleeping through it.
        failures, _deadline = koth_scorer._claim_read_backoff[self.box.id]
        koth_scorer._claim_read_backoff[self.box.id] = (failures, time.monotonic() - 1)
        with patch('app.koth_scorer.map_concurrent_guest_reads',
                   return_value=['tok-A\n']) as reads, \
             patch('app.proxmox_service.get_proxmox_connection'):
            koth_scorer._poll_claims(self.game, [self.box])
        reads.assert_called_once()
        db.session.refresh(self.box)
        self.assertEqual(self.box.owner_team_id, self.team.id)
        self.assertNotIn(self.box.id, koth_scorer._claim_read_backoff,
                         'a successful read must clear the backoff')

    def test_destroyed_vm_is_not_read(self):
        vm = db.session.get(VMRecord, self.box.vm_record_id)
        vm.destroyed = True
        self.game.claim_mode = 'file'
        db.session.commit()
        with patch('app.koth_scorer.map_concurrent_guest_reads') as reads, \
             patch('app.proxmox_service.get_proxmox_connection'):
            koth_scorer._poll_claims(self.game, [self.box])
        reads.assert_not_called()

    def test_forget_game_clears_per_box_state(self):
        koth_scorer._claim_read_backoff[self.box.id] = (3, time.monotonic() + 999)
        koth_scorer._last_ip_refresh[self.game.id] = time.monotonic()
        koth_scorer.forget_game(self.game.id)
        self.assertNotIn(self.box.id, koth_scorer._claim_read_backoff)
        self.assertNotIn(self.game.id, koth_scorer._last_ip_refresh)

    # -- IP refresh rate limiting ---------------------------------------
    def test_ip_refresh_is_rate_limited(self):
        # A box that never reports an IP qualified for the refresh sweep
        # on every tick, costing N guest-agent calls forever.
        self.box.ips = []
        db.session.commit()
        with patch('app.routes.koth_admin._refresh_box_ips', return_value=0) as refresh:
            koth_scorer._maybe_refresh_ips(self.game, [self.box])
            koth_scorer._maybe_refresh_ips(self.game, [self.box])
            koth_scorer._maybe_refresh_ips(self.game, [self.box])
        refresh.assert_called_once()

    def test_ip_refresh_skipped_entirely_when_all_boxes_have_ips(self):
        with patch('app.routes.koth_admin._refresh_box_ips') as refresh:
            koth_scorer._maybe_refresh_ips(self.game, [self.box])
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
