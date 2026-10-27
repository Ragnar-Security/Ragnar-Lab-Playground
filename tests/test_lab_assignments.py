"""
Component tests for lab assignment functionality
Tests StudentLabAssignment model, template filtering, and admin assignment routes
"""

import unittest
import json
from unittest.mock import patch
from sqlalchemy.exc import IntegrityError
from app import create_app
from app.models import db, User, StudentLabAssignment


MOCK_TEMPLATE_CONFIG = {
    'deployment_groups': [
        {
            'display_name': 'homework1',
            'description': 'HW1',
            'enabled': True,
            'uptime_minutes': 120,
            'extend_time_minutes': 60,
            'templates': [
                {'name': 'vm1', 'type': 'lxc', 'memory': 2048, 'cores': 2, 'storage': 20}
            ]
        },
        {
            'display_name': 'homework2',
            'description': 'HW2',
            'enabled': True,
            'uptime_minutes': 120,
            'extend_time_minutes': 60,
            'templates': [
                {'name': 'vm2', 'type': 'lxc', 'memory': 2048, 'cores': 2, 'storage': 20}
            ]
        }
    ]
}


class TestLabAssignments(unittest.TestCase):
    """Test cases for lab assignment model and routes"""

    def setUp(self):
        """Set up test fixtures"""
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret-key'

        self.client = self.app.test_client()

        self.app_context = self.app.app_context()
        self.app_context.push()

        db.create_all()

        # Create non-admin test user
        self.test_user = User(
            login_id='testuser',
            full_name='Test User',
            is_admin=False,
            entry_vlan_tag=101,
            internal_vlan_tag=201
        )
        self.test_user.set_password('password123')
        db.session.add(self.test_user)

        # Create admin user
        self.admin_user = User(
            login_id='adminuser',
            full_name='Admin User',
            is_admin=True
        )
        self.admin_user.set_password('adminpass')
        db.session.add(self.admin_user)

        db.session.commit()

        # Login as admin by default
        self.login_as('adminuser', 'adminpass')

    def tearDown(self):
        """Clean up test fixtures"""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_as(self, login_id, password):
        """Helper to login as a specific user"""
        return self.client.post('/auth/login', data={
            'login_id': login_id,
            'password': password
        })

    # ---- Model tests ----

    def test_model_creation(self):
        """StudentLabAssignment can be created and queried"""
        assignment = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1',
            assigned_by=self.admin_user.id
        )
        db.session.add(assignment)
        db.session.commit()

        result = StudentLabAssignment.query.filter_by(user_id=self.test_user.id).first()
        self.assertIsNotNone(result)
        self.assertEqual(result.deployment_group_name, 'homework1')
        self.assertEqual(result.assigned_by, self.admin_user.id)
        self.assertEqual(result.user.login_id, 'testuser')

    def test_unique_constraint(self):
        """Duplicate user_id + deployment_group_name raises IntegrityError"""
        a1 = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        db.session.add(a1)
        db.session.commit()

        a2 = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        db.session.add(a2)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    # ---- Template filtering tests ----

    @patch('app.routes.templates.load_template_config')
    def test_get_templates_no_assignments_returns_all(self, mock_load):
        """GET /templates with no assignments anywhere returns all groups"""
        mock_load.return_value = MOCK_TEMPLATE_CONFIG

        # Login as non-admin student (no assignments exist for any group)
        self.login_as('testuser', 'password123')

        response = self.client.get('/templates')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['deployment_groups']), 2)

    @patch('app.routes.templates.load_template_config')
    def test_get_templates_restricted_group_hidden_from_unassigned(self, mock_load):
        """When a group has assignments, unassigned students cannot see it"""
        mock_load.return_value = MOCK_TEMPLATE_CONFIG

        # Assign test_user to homework1 — this restricts homework1
        assignment = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        db.session.add(assignment)
        db.session.commit()

        # Create another student who is NOT assigned
        other_student = User(login_id='other', full_name='Other', is_admin=False,
                            entry_vlan_tag=103, internal_vlan_tag=203)
        other_student.set_password('password123')
        db.session.add(other_student)
        db.session.commit()

        # Login as other_student — should NOT see homework1 (restricted), but should see homework2 (unrestricted)
        self.login_as('other', 'password123')
        response = self.client.get('/templates')
        data = json.loads(response.data)

        self.assertEqual(len(data['deployment_groups']), 1)
        self.assertEqual(data['deployment_groups'][0]['display_name'], 'homework2')

    @patch('app.routes.templates.load_template_config')
    def test_get_templates_assigned_student_sees_restricted_group(self, mock_load):
        """Assigned student can see restricted group plus unrestricted groups"""
        mock_load.return_value = MOCK_TEMPLATE_CONFIG

        # Assign test_user to homework1
        assignment = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        db.session.add(assignment)
        db.session.commit()

        # Login as test_user — should see homework1 (assigned) AND homework2 (unrestricted)
        self.login_as('testuser', 'password123')
        response = self.client.get('/templates')
        data = json.loads(response.data)

        self.assertEqual(len(data['deployment_groups']), 2)
        names = {g['display_name'] for g in data['deployment_groups']}
        self.assertIn('homework1', names)
        self.assertIn('homework2', names)

    @patch('app.routes.templates.load_template_config')
    def test_get_templates_admin_sees_all(self, mock_load):
        """Admin always sees all groups regardless of assignments"""
        mock_load.return_value = MOCK_TEMPLATE_CONFIG

        # Restrict homework1 by assigning a student
        assignment = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        db.session.add(assignment)
        db.session.commit()

        # Login as admin — should see both
        self.login_as('adminuser', 'adminpass')
        response = self.client.get('/templates')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['deployment_groups']), 2)

    # ---- Admin assignment routes ----

    def test_create_assignment_admin_only(self):
        """POST /admin/assignments creates assignment (admin only)"""
        response = self.client.post('/admin/assignments', data={
            'user_id': self.test_user.id,
            'deployment_group_name': 'homework1'
        })
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(data['success'])
        self.assertEqual(data['assignment']['deployment_group_name'], 'homework1')

        # Verify in database
        assignment = StudentLabAssignment.query.filter_by(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        ).first()
        self.assertIsNotNone(assignment)

    def test_create_assignment_non_admin_forbidden(self):
        """Non-admin cannot create assignments"""
        self.login_as('testuser', 'password123')

        response = self.client.post('/admin/assignments', data={
            'user_id': self.test_user.id,
            'deployment_group_name': 'homework1'
        })

        # Should be forbidden (403) or redirect
        self.assertIn(response.status_code, [403, 302])

    def test_create_assignment_rejects_duplicate(self):
        """POST /admin/assignments rejects duplicate assignment"""
        # Create first assignment
        self.client.post('/admin/assignments', data={
            'user_id': self.test_user.id,
            'deployment_group_name': 'homework1'
        })

        # Try to create duplicate
        response = self.client.post('/admin/assignments', data={
            'user_id': self.test_user.id,
            'deployment_group_name': 'homework1'
        })
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(data['success'])

    def test_delete_assignment(self):
        """DELETE /admin/assignments/<id> removes assignment"""
        # Create assignment
        response = self.client.post('/admin/assignments', data={
            'user_id': self.test_user.id,
            'deployment_group_name': 'homework1'
        })
        assignment_id = json.loads(response.data)['assignment']['id']

        # Delete it
        response = self.client.delete(f'/admin/assignments/{assignment_id}')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])

        # Verify gone
        self.assertIsNone(db.session.get(StudentLabAssignment, assignment_id))

    def test_bulk_assign(self):
        """POST /admin/assignments/bulk creates multiple assignments"""
        # Create a second user
        user2 = User(login_id='student2', full_name='Student Two')
        user2.set_password('password123')
        db.session.add(user2)
        db.session.commit()

        response = self.client.post('/admin/assignments/bulk',
            data=json.dumps({
                'user_ids': [self.test_user.id, user2.id],
                'deployment_group_names': ['homework1']
            }),
            content_type='application/json'
        )
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['created'], 2)
        self.assertEqual(data['skipped'], 0)

    def test_clear_assignments(self):
        """POST /admin/assignments/clear removes all for a group"""
        # Create multiple assignments for homework1
        user2 = User(login_id='student2', full_name='Student Two')
        user2.set_password('password123')
        db.session.add(user2)
        db.session.commit()

        for uid in [self.test_user.id, user2.id]:
            a = StudentLabAssignment(user_id=uid, deployment_group_name='homework1')
            db.session.add(a)
        db.session.commit()

        self.assertEqual(
            StudentLabAssignment.query.filter_by(deployment_group_name='homework1').count(), 2
        )

        response = self.client.post('/admin/assignments/clear',
            data=json.dumps({'deployment_group_name': 'homework1'}),
            content_type='application/json'
        )
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['deleted'], 2)

        # Verify gone
        self.assertEqual(
            StudentLabAssignment.query.filter_by(deployment_group_name='homework1').count(), 0
        )

    def test_list_assignments(self):
        """GET /admin/assignments lists all assignments"""
        # Create some assignments
        a1 = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        )
        a2 = StudentLabAssignment(
            user_id=self.test_user.id,
            deployment_group_name='homework2'
        )
        db.session.add_all([a1, a2])
        db.session.commit()

        response = self.client.get('/admin/assignments')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['assignments']), 2)

    def test_list_students_admin_only(self):
        """GET /admin/students returns student list for admins"""
        response = self.client.get('/admin/students')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        # Should include both testuser and adminuser
        login_ids = [s['login_id'] for s in data['students']]
        self.assertIn('testuser', login_ids)
        self.assertIn('adminuser', login_ids)


    def test_list_assignments_filter_by_user(self):
        """GET /admin/assignments?user_id=X should return only that user's assignments"""
        user2 = User(login_id='student2', full_name='Student Two')
        user2.set_password('password123')
        db.session.add(user2)
        db.session.commit()

        a1 = StudentLabAssignment(user_id=self.test_user.id, deployment_group_name='homework1')
        a2 = StudentLabAssignment(user_id=user2.id, deployment_group_name='homework2')
        db.session.add_all([a1, a2])
        db.session.commit()

        response = self.client.get(f'/admin/assignments?user_id={self.test_user.id}')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['assignments']), 1)
        self.assertEqual(data['assignments'][0]['user_id'], self.test_user.id)

    def test_list_assignments_filter_by_group(self):
        """GET /admin/assignments?group=X should return only assignments for that group"""
        a1 = StudentLabAssignment(user_id=self.test_user.id, deployment_group_name='homework1')
        a2 = StudentLabAssignment(user_id=self.test_user.id, deployment_group_name='homework2')
        db.session.add_all([a1, a2])
        db.session.commit()

        response = self.client.get('/admin/assignments?group=homework1')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['assignments']), 1)
        self.assertEqual(data['assignments'][0]['deployment_group_name'], 'homework1')

    def test_clear_nonexistent_group(self):
        """POST /admin/assignments/clear with a group that has no assignments should succeed with deleted=0"""
        response = self.client.post('/admin/assignments/clear',
            data=json.dumps({'deployment_group_name': 'nonexistent_group'}),
            content_type='application/json'
        )
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['deleted'], 0)

    def test_create_assignment_json_body(self):
        """POST /admin/assignments with JSON content-type should work"""
        response = self.client.post('/admin/assignments',
            data=json.dumps({
                'user_id': self.test_user.id,
                'deployment_group_name': 'homework1'
            }),
            content_type='application/json'
        )
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(data['success'])
        self.assertEqual(data['assignment']['deployment_group_name'], 'homework1')

        # Verify in database
        assignment = StudentLabAssignment.query.filter_by(
            user_id=self.test_user.id,
            deployment_group_name='homework1'
        ).first()
        self.assertIsNotNone(assignment)


if __name__ == '__main__':
    unittest.main()
