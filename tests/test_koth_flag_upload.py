"""End-to-end tests for POST /koth/api/games/<id>/flags/upload."""

import unittest

from app import create_app
from app.models import (
    db, User, StudentGroup, KothGame, KothBox, KothFlag,
)


YAML_TWO_ROWS = """\
- name: Box Flag
  value: uuid-box-1
  points: 5
  box_name: final-dc
- name: Global Flag
  value: uuid-global-1
  points: 10
"""


class FlagUploadTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from werkzeug.security import generate_password_hash
        self.admin = User(login_id='admin-up',
                          password_hash=generate_password_hash('p'),
                          is_admin=True)
        db.session.add(self.admin)
        db.session.add(StudentGroup(name='team-up', display_name='Team Up'))
        db.session.commit()
        self._make_game(status='draft')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, user=None):
        user = user or self.admin
        with self.client.session_transaction() as s:
            s['user_id'] = user.id
            s['session_version'] = user.session_version

    def _make_game(self, *, status='draft'):
        # Tear down any previous game so this is the only one.
        KothBox.query.delete()
        KothFlag.query.delete()
        KothGame.query.delete()
        db.session.commit()
        game = KothGame(name='g', deployment_group_name='x',
                        entry_vlan_tag=550, internal_vlan_tag=551,
                        status=status,
                        is_active=(status == 'active'))
        db.session.add(game)
        db.session.commit()
        # Seed a KothBox so box_name resolution works.
        box = KothBox(game_id=game.id, name='final-dc', ips=[], services=[])
        db.session.add(box)
        db.session.commit()
        self.game = game
        self.box = box
        return game

    # -----------------------------------------------------------------
    def test_yaml_uploaded_via_xyaml_content_type(self):
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data=YAML_TWO_ROWS,
            content_type='application/x-yaml')
        self.assertEqual(r.status_code, 201, r.data)
        flags = KothFlag.query.filter_by(game_id=self.game.id).order_by(
            KothFlag.id).all()
        self.assertEqual(len(flags), 2)
        # Box-bound first, global second
        box_bound = next(f for f in flags if f.value == 'uuid-box-1')
        global_flag = next(f for f in flags if f.value == 'uuid-global-1')
        self.assertEqual(box_bound.box_id, self.box.id)
        self.assertIsNone(global_flag.box_id)

    def test_yaml_uploaded_via_json_wrapper(self):
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            json={'yaml': YAML_TWO_ROWS})
        self.assertEqual(r.status_code, 201, r.data)
        flags = KothFlag.query.filter_by(game_id=self.game.id).all()
        self.assertEqual(len(flags), 2)

    def test_invalid_yaml_returns_400_inserts_nothing(self):
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data='not: valid: yaml: ::',
            content_type='application/x-yaml')
        self.assertEqual(r.status_code, 400, r.data)
        body = r.get_json()
        self.assertIn('error', body)
        self.assertIn('parse', body['error'].lower())
        self.assertEqual(KothFlag.query.count(), 0)

    def test_top_level_must_be_list(self):
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data='name: foo\nvalue: bar\n',
            content_type='application/x-yaml')
        self.assertEqual(r.status_code, 400, r.data)
        body = r.get_json()
        self.assertIn('error', body)
        self.assertIn('list', body['error'].lower())
        self.assertEqual(KothFlag.query.count(), 0)

    def test_unknown_box_name_aborts_entire_upload(self):
        self._login()
        bad_yaml = """\
- name: Good 1
  value: ok-1
- name: Good 2
  value: ok-2
- name: Good 3
  value: ok-3
- name: Bad
  value: ok-4
  box_name: does-not-exist
"""
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data=bad_yaml, content_type='application/x-yaml')
        self.assertEqual(r.status_code, 400, r.data)
        body = r.get_json()
        # Error message should reference the bad row index AND the bad name.
        self.assertIn('error', body)
        self.assertIn('does-not-exist', body['error'])
        # Atomic: ZERO inserted.
        self.assertEqual(KothFlag.query.count(), 0)

    def test_rejects_against_ended_game(self):
        self.game.status = 'ended'
        self.game.is_active = False
        db.session.commit()
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data=YAML_TWO_ROWS,
            content_type='application/x-yaml')
        self.assertEqual(r.status_code, 409, r.data)
        body = r.get_json()
        self.assertIn('cannot add flags to an ended game', body['error'])

    def test_allowed_in_draft(self):
        # game starts as draft from setUp; add a single-row YAML.
        self._login()
        r = self.client.post(
            f'/koth/api/games/{self.game.id}/flags/upload',
            data='- name: Solo\n  value: solo-1\n  points: 3\n',
            content_type='application/x-yaml')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(KothFlag.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
