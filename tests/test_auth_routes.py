"""
Component tests for authentication routes
Tests login, logout, registration functionality
"""

import os
import unittest
import json
from unittest import mock

import app as app_package
from app import create_app
from app.models import db, User, StudentWhitelist


class TestAuthRoutes(unittest.TestCase):
    """Test cases for authentication routes"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret-key'
        # Off by default for new installs; the reset tests below exercise it.
        self.app.config['SELF_SERVICE_PASSWORD_RESET'] = True
        
        self.client = self.app.test_client()
        
        self.app_context = self.app.app_context()
        self.app_context.push()
        
        db.create_all()
        
        # Create test user
        self.test_user = User(
            login_id='testuser',
            full_name='Test User',
            email='test@example.com',
            is_admin=False
        )
        self.test_user.set_password('password123')
        db.session.add(self.test_user)
        db.session.commit()
    
    def tearDown(self):
        """Clean up test fixtures"""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
    def _login(self, client=None, login_id='testuser', password='password123'):
        client = client or self.client
        return client.post('/auth/login',
            data=json.dumps({'login_id': login_id, 'password': password}),
            content_type='application/json'
        )

    def _session_cookie_value(self, client=None):
        client = client or self.client
        name = self.app.config.get('SESSION_COOKIE_NAME', 'session')
        return client.get_cookie(name).value

    
    def test_register_success(self):
        """Test successful user registration with whitelist entry"""
        # Must have a whitelist entry for registration to work
        whitelist = StudentWhitelist(login_id='newuser', sis_user_id='987654321', full_name='New User')
        db.session.add(whitelist)
        db.session.commit()

        response = self.client.post('/auth/register',
            data=json.dumps({
                'login_id': 'newuser',
                'sis_user_id': '987654321',
                'password': 'newpass123'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])

        # Verify user was created
        user = User.query.filter_by(login_id='newuser').first()
        self.assertIsNotNone(user)

    def test_register_duplicate_login(self):
        """Test registration with existing login_id"""
        # Create whitelist entry for testuser (user already exists from setUp)
        whitelist = StudentWhitelist(login_id='testuser', sis_user_id='111111111', full_name='Test User')
        db.session.add(whitelist)
        db.session.commit()

        response = self.client.post('/auth/register',
            data=json.dumps({
                'login_id': 'testuser',
                'sis_user_id': '111111111',
                'password': 'password123'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertIn('already', data['error'].lower())

    def test_register_refused_when_roster_row_has_no_sis(self):
        """A roster row without a SIS ID has nothing to check the submission
        against; accepting one would hand the account to whoever asks first."""
        for stored in (None, ''):
            for submitted in ('987654321', '000000000', 'anything'):
                with self.subTest(stored=stored, submitted=submitted):
                    db.session.add(StudentWhitelist(login_id='nosis', sis_user_id=stored, full_name='No Sis'))
                    db.session.commit()
                    try:
                        response = self.client.post('/auth/register', json={
                            'login_id': 'nosis',
                            'sis_user_id': submitted,
                            'password': 'newpass123',
                        })

                        self.assertFalse(response.get_json()['success'])
                        self.assertIsNone(User.query.filter_by(login_id='nosis').first())
                    finally:
                        User.query.filter_by(login_id='nosis').delete()
                        StudentWhitelist.query.filter_by(login_id='nosis').delete()
                        db.session.commit()
    
    def test_register_missing_fields(self):
        """Test registration with missing required fields"""
        response = self.client.post('/auth/register',
            data=json.dumps({
                'login_id': 'newuser'
                # Missing other required fields
            }),
            content_type='application/json'
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
    
    def test_login_success(self):
        """Test successful login"""
        response = self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        
        # Verify session was created
        with self.client.session_transaction() as sess:
            self.assertIn('user_id', sess)
            self.assertIn('login_id', sess)
    
    def test_login_wrong_password(self):
        """Test login with incorrect password"""
        response = self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'wrongpassword'
            }),
            content_type='application/json'
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
        self.assertIn('invalid', data['error'].lower())
    
    def test_login_nonexistent_user(self):
        """Test login with non-existent user"""
        response = self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'nonexistent',
                'password': 'password'
            }),
            content_type='application/json'
        )
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['success'])
    
    def test_logout(self):
        """Test logout functionality"""
        # First login
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )
        
        # Then logout
        response = self.client.post('/auth/logout')
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        
        # Verify session was cleared
        with self.client.session_transaction() as sess:
            self.assertNotIn('user_id', sess)
    def test_logout_revokes_a_copied_session_cookie(self):
        self._login()
        old_cookie = self._session_cookie_value()

        self.client.post('/auth/logout')
        self.client.set_cookie('session', old_cookie)

        response = self.client.get('/auth/me')

        self.assertEqual(response.status_code, 401)

    def test_stale_in_flight_response_cannot_reauthenticate_client(self):
        self._login()
        old_cookie = self._session_cookie_value()

        self.client.post('/auth/logout')
        self.client.set_cookie('session', old_cookie)
        stale_response = self.client.get('/auth/me')
        next_response = self.client.get('/auth/me')

        self.assertEqual(stale_response.status_code, 401)
        self.assertEqual(next_response.status_code, 401)
        self.assertIsNone(self.client.get_cookie('session'))

    def test_login_again_after_logout_uses_current_session_version(self):
        self._login()
        self.client.post('/auth/logout')

        login_response = self._login()
        me_response = self.client.get('/auth/me')

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.get_json()['user']['login_id'], 'testuser')

    def test_disabling_user_revokes_existing_session(self):
        self._login()
        original_version = self.test_user.session_version

        admin = User(login_id='admin', full_name='Admin', is_admin=True)
        admin.set_password('adminpass123')
        db.session.add(admin)
        db.session.commit()

        admin_client = self.app.test_client()
        self._login(admin_client, 'admin', 'adminpass123')
        disable_response = admin_client.put(
            f'/admin/students/{self.test_user.id}',
            json={'disabled': True},
        )
        db.session.refresh(self.test_user)

        self.assertEqual(disable_response.status_code, 200)
        self.assertEqual(self.test_user.session_version, original_version + 1)
        self.assertEqual(self.client.get('/auth/me').status_code, 401)

    def test_cookie_without_session_version_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.test_user.id

        response = self.client.get('/auth/me')

        self.assertEqual(response.status_code, 401)
    
    def test_get_current_user_authenticated(self):
        """Test getting current user info when logged in"""
        # Login first
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )
        
        # Get user info
        response = self.client.get('/auth/me')
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['user']['login_id'], 'testuser')
        self.assertEqual(data['user']['full_name'], 'Test User')
    
    def test_get_current_user_not_authenticated(self):
        """Test getting current user info when not logged in returns 401"""
        response = self.client.get('/auth/me')

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(data['success'])
    
    def test_health_check(self):
        """Test health check endpoint"""
        response = self.client.get('/auth/health')
        
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['status'], 'ok')
        self.assertIn('timestamp', data)

    def test_health_check_reports_version_and_revision(self):
        """Operators and monitoring read the running release from health."""
        with mock.patch.dict(os.environ, {'APP_REVISION': 'abc1234'}):
            data = self.client.get('/auth/health').get_json()

        self.assertEqual(data['version'], app_package.__version__)
        self.assertEqual(data['revision'], 'abc1234')

    def test_health_check_revision_is_null_without_build_revision(self):
        """Source builds on the droplet pass no APP_REVISION; the image then
        sets it empty, which must read as unknown, not as a revision."""
        for value in (None, ''):
            with self.subTest(value=value), mock.patch.dict(os.environ):
                os.environ.pop('APP_REVISION', None)
                if value is not None:
                    os.environ['APP_REVISION'] = value
                data = self.client.get('/auth/health').get_json()

                self.assertEqual(data['version'], app_package.__version__)
                self.assertIsNone(data['revision'])


    def test_disabled_user_cannot_login(self):
        """A disabled user should get 403 when attempting to login"""
        self.test_user.disabled = True
        db.session.commit()

        response = self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 403)
        data = json.loads(response.data)
        self.assertFalse(data['success'])
        self.assertIn('disabled', data['error'].lower())

    def test_check_auth_when_logged_in(self):
        """GET /auth/check should return authenticated=true and user data when logged in"""
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )

        response = self.client.get('/auth/check')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['authenticated'])
        self.assertIn('user', data)
        self.assertEqual(data['user']['login_id'], 'testuser')

    def test_check_auth_when_not_logged_in(self):
        """GET /auth/check should return authenticated=false when not logged in"""
        response = self.client.get('/auth/check')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['authenticated'])

    def test_check_auth_disabled_user(self):
        """GET /auth/check should return authenticated=false for a disabled user"""
        # Login first
        self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'password123'
            }),
            content_type='application/json'
        )

        # Disable the user
        self.test_user.disabled = True
        db.session.commit()

        response = self.client.get('/auth/check')
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(data['authenticated'])

    def test_reset_password_success(self):
        """POST /auth/reset-password with valid data should reset password"""
        whitelist = StudentWhitelist(login_id='testuser', sis_user_id='123456789', full_name='Test User')
        db.session.add(whitelist)
        db.session.commit()

        response = self.client.post('/auth/reset-password',
            data=json.dumps({
                'login_id': 'testuser',
                'sis_user_id': '123456789',
                'password': 'newpassword123'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])

        # Verify new password works
        login_response = self.client.post('/auth/login',
            data=json.dumps({
                'login_id': 'testuser',
                'password': 'newpassword123'
            }),
            content_type='application/json'
        )
        login_data = json.loads(login_response.data)
        self.assertTrue(login_data['success'])

    def test_reset_password_revokes_existing_session(self):
        whitelist = StudentWhitelist(
            login_id='testuser',
            sis_user_id='123456789',
            full_name='Test User',
        )
        db.session.add(whitelist)
        db.session.commit()
        self._login()
        original_version = self.test_user.session_version

        response = self.client.post('/auth/reset-password', json={
            'login_id': 'testuser',
            'sis_user_id': '123456789',
            'password': 'newpassword123',
        })
        db.session.refresh(self.test_user)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.test_user.session_version, original_version + 1)
        self.assertEqual(self.client.get('/auth/me').status_code, 401)

    def test_reset_password_wrong_sis_id(self):
        """POST /auth/reset-password with wrong sis_user_id should fail"""
        whitelist = StudentWhitelist(login_id='testuser', sis_user_id='123456789', full_name='Test User')
        db.session.add(whitelist)
        db.session.commit()

        response = self.client.post('/auth/reset-password',
            data=json.dumps({
                'login_id': 'testuser',
                'sis_user_id': '999999999',
                'password': 'newpassword123'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertFalse(data['success'])
        self.assertTrue(self.test_user.check_password('password123'))

    def test_reset_password_nonexistent_user(self):
        """POST /auth/reset-password with non-existent login_id should fail"""
        response = self.client.post('/auth/reset-password',
            data=json.dumps({
                'login_id': 'nonexistent',
                'sis_user_id': '123456789',
                'password': 'newpassword123'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertFalse(data['success'])

    def test_reset_password_short_password(self):
        """POST /auth/reset-password with password < 8 chars should fail"""
        whitelist = StudentWhitelist(login_id='testuser', sis_user_id='123456789', full_name='Test User')
        db.session.add(whitelist)
        db.session.commit()

        response = self.client.post('/auth/reset-password',
            data=json.dumps({
                'login_id': 'testuser',
                'sis_user_id': '123456789',
                'password': 'short'
            }),
            content_type='application/json'
        )

        data = json.loads(response.data)
        self.assertFalse(data['success'])
        self.assertIn('8 characters', data['error'].lower())


if __name__ == '__main__':
    unittest.main()
