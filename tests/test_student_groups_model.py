"""Tests for StudentGroup, StudentGroupMember."""

import unittest
from sqlalchemy.exc import IntegrityError
from app import create_app
from app.models import db, User, StudentGroup, StudentGroupMember, VMRecord


class StudentGroupsModelTestCase(unittest.TestCase):
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

    def test_create_student_group(self):
        g = StudentGroup(name='team-alpha', display_name='Team Alpha')
        db.session.add(g)
        db.session.commit()
        self.assertIsNotNone(g.id)
        self.assertEqual(g.name, 'team-alpha')
        self.assertEqual(g.display_name, 'Team Alpha')
        self.assertIsNone(g.entry_vlan_tag)
        self.assertIsNone(g.internal_vlan_tag)

    def test_slug_must_be_unique(self):
        db.session.add(StudentGroup(name='team-alpha', display_name='A'))
        db.session.commit()
        db.session.add(StudentGroup(name='team-alpha', display_name='B'))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_vlan_tags_unique_across_groups(self):
        db.session.add(StudentGroup(name='a', display_name='A', entry_vlan_tag=350))
        db.session.commit()
        db.session.add(StudentGroup(name='b', display_name='B', entry_vlan_tag=350))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_to_dict(self):
        g = StudentGroup(name='team-alpha', display_name='Team Alpha',
                         description='desc', entry_vlan_tag=350, internal_vlan_tag=450)
        db.session.add(g)
        db.session.commit()
        d = g.to_dict()
        self.assertEqual(d['name'], 'team-alpha')
        self.assertEqual(d['display_name'], 'Team Alpha')
        self.assertEqual(d['description'], 'desc')
        self.assertEqual(d['entry_vlan_tag'], 350)
        self.assertEqual(d['internal_vlan_tag'], 450)


    def test_add_member_to_group(self):
        u = User(login_id='alice', full_name='Alice')
        u.set_password('x')
        g = StudentGroup(name='team-alpha', display_name='A')
        db.session.add_all([u, g])
        db.session.commit()
        m = StudentGroupMember(group_id=g.id, user_id=u.id)
        db.session.add(m)
        db.session.commit()
        self.assertIsNotNone(m.id)
        self.assertEqual(m.group_id, g.id)
        self.assertEqual(m.user_id, u.id)

    def test_user_cannot_be_in_two_groups(self):
        u = User(login_id='alice', full_name='Alice')
        u.set_password('x')
        g1 = StudentGroup(name='team-a', display_name='A')
        g2 = StudentGroup(name='team-b', display_name='B')
        db.session.add_all([u, g1, g2])
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=g1.id, user_id=u.id))
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=g2.id, user_id=u.id))
        with self.assertRaises(IntegrityError):
            db.session.commit()

    def test_member_cascade_on_group_delete(self):
        u = User(login_id='alice', full_name='Alice')
        u.set_password('x')
        g = StudentGroup(name='team-a', display_name='A')
        db.session.add_all([u, g])
        db.session.commit()
        db.session.add(StudentGroupMember(group_id=g.id, user_id=u.id))
        db.session.commit()
        db.session.delete(g)
        db.session.commit()
        self.assertEqual(StudentGroupMember.query.count(), 0)


    def test_vmrecord_owner_group_id_optional(self):
        from datetime import datetime, timedelta
        g = StudentGroup(name='team-a', display_name='A')
        db.session.add(g)
        db.session.commit()
        now = datetime.now()
        vm = VMRecord(vmid=100, vm_name='group-team-a-entry', vm_type='qemu',
                      node='pve', template_name='Midterm Team 1/entry',
                      student_name='team-a', owner_group_id=g.id,
                      created_at=now, expires_at=now + timedelta(hours=1))
        db.session.add(vm)
        db.session.commit()
        self.assertEqual(vm.owner_group_id, g.id)
        self.assertEqual(vm.owner_group.name, 'team-a')

    def test_vmrecord_owner_group_id_nullable(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        vm = VMRecord(vmid=101, vm_name='student-alice-vm', vm_type='qemu',
                      node='pve', template_name='HW1/vm', student_name='alice',
                      created_at=now, expires_at=now + timedelta(hours=1))
        db.session.add(vm)
        db.session.commit()
        self.assertIsNone(vm.owner_group_id)
        self.assertIsNone(vm.owner_group)


if __name__ == '__main__':
    unittest.main()
