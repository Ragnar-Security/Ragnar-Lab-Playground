"""Tests for app.vlan_allocator — user and group VLAN allocation."""

import unittest
from app import create_app
from app.models import db, User, StudentWhitelist, StudentGroup
from app.vlan_allocator import (
    allocate_pair, validate_vlan_free, VlanPoolExhausted, VlanInUse,
)


class AutoAllocateTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_auto_allocate_group_returns_pool_minimums_when_empty(self):
        entry, internal = allocate_pair(scope='group')
        self.assertEqual(entry, 300)
        self.assertEqual(internal, 400)

    def test_auto_allocate_group_skips_existing_group_tags(self):
        db.session.add(StudentGroup(name='a', display_name='A',
                                    entry_vlan_tag=300, internal_vlan_tag=400))
        db.session.commit()
        entry, internal = allocate_pair(scope='group')
        self.assertEqual(entry, 301)
        self.assertEqual(internal, 401)

    def test_auto_allocate_user_returns_pool_minimums_when_empty(self):
        entry, internal = allocate_pair(scope='user')
        self.assertEqual(entry, 100)
        self.assertEqual(internal, 200)

    def test_auto_allocate_user_skips_whitelist_and_user_tags(self):
        db.session.add(StudentWhitelist(login_id='a', entry_vlan_tag=100, internal_vlan_tag=200))
        u = User(login_id='b', entry_vlan_tag=101, internal_vlan_tag=201)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        entry, internal = allocate_pair(scope='user')
        self.assertEqual(entry, 102)
        self.assertEqual(internal, 202)

    def test_pool_exhausted_raises(self):
        # Fill the group entry pool (300-400 inclusive = 101 slots)
        for i, tag in enumerate(range(300, 401)):
            db.session.add(StudentGroup(name=f'g{i}', display_name=f'g{i}',
                                        entry_vlan_tag=tag,
                                        internal_vlan_tag=400 + i))
        db.session.commit()
        with self.assertRaises(VlanPoolExhausted):
            allocate_pair(scope='group')


class ExplicitOverrideTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_explicit_pair_accepted_in_pool(self):
        entry, internal = allocate_pair(scope='group',
                                        explicit_entry=350, explicit_internal=450)
        self.assertEqual((entry, internal), (350, 450))

    def test_explicit_pair_accepted_out_of_pool(self):
        entry, internal = allocate_pair(scope='group',
                                        explicit_entry=999, explicit_internal=1000)
        self.assertEqual((entry, internal), (999, 1000))

    def test_explicit_rejects_collision_with_user(self):
        u = User(login_id='a', entry_vlan_tag=150)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with self.assertRaises(VlanInUse):
            allocate_pair(scope='group', explicit_entry=150, explicit_internal=450)

    def test_explicit_rejects_collision_with_other_group(self):
        db.session.add(StudentGroup(name='a', display_name='A',
                                    entry_vlan_tag=350, internal_vlan_tag=450))
        db.session.commit()
        with self.assertRaises(VlanInUse):
            allocate_pair(scope='group', explicit_entry=350, explicit_internal=460)

    def test_explicit_allows_self_reassignment_when_exclude_given(self):
        g = StudentGroup(name='a', display_name='A',
                         entry_vlan_tag=350, internal_vlan_tag=450)
        db.session.add(g)
        db.session.commit()
        entry, internal = allocate_pair(scope='group',
                                        explicit_entry=350, explicit_internal=450,
                                        exclude_group_id=g.id)
        self.assertEqual((entry, internal), (350, 450))

    def test_mixed_explicit_and_auto_rejected(self):
        with self.assertRaises(ValueError):
            allocate_pair(scope='group', explicit_entry=350, explicit_internal=None)

    def test_same_entry_and_internal_rejected(self):
        with self.assertRaises(ValueError):
            allocate_pair(scope='group', explicit_entry=350, explicit_internal=350)

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            allocate_pair(scope='group', explicit_entry=0, explicit_internal=5000)


class KothVlanCollisionTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_validate_vlan_free_rejects_active_koth_tag(self):
        from app.models import KothGame
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=550, internal_vlan_tag=551,
                     status='active', is_active=True)
        db.session.add(g)
        db.session.commit()
        from app.vlan_allocator import validate_vlan_free, VlanInUse
        with self.assertRaises(VlanInUse):
            validate_vlan_free(550)
        with self.assertRaises(VlanInUse):
            validate_vlan_free(551)

    def test_validate_vlan_free_allows_ended_koth_tag(self):
        """Tags from an ended KOTH game should be reusable."""
        from app.models import KothGame
        g = KothGame(name='g', deployment_group_name='x',
                     entry_vlan_tag=552, internal_vlan_tag=553,
                     status='ended', is_active=False)
        db.session.add(g)
        db.session.commit()
        from app.vlan_allocator import validate_vlan_free
        # Should NOT raise
        validate_vlan_free(552)
        validate_vlan_free(553)


if __name__ == '__main__':
    unittest.main()
