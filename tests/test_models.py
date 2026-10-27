"""
Unit tests for database models
Tests User and VMRecord models
"""

import unittest
from datetime import datetime, timedelta
from app import create_app
from app.models import db, User, VMRecord


class TestModels(unittest.TestCase):
    """Test cases for database models"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.app = create_app(testing=True)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        
        self.app_context = self.app.app_context()
        self.app_context.push()
        
        db.create_all()
    
    def tearDown(self):
        """Clean up test fixtures"""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
    
    def test_user_creation(self):
        """Test creating a user"""
        user = User(
            login_id='testuser',
            full_name='Test User',
            email='test@example.com',
            is_admin=False
        )
        user.set_password('password123')
        
        db.session.add(user)
        db.session.commit()
        
        # Verify user was created
        saved_user = User.query.filter_by(login_id='testuser').first()
        self.assertIsNotNone(saved_user)
        self.assertEqual(saved_user.login_id, 'testuser')
        self.assertEqual(saved_user.full_name, 'Test User')
        self.assertEqual(saved_user.email, 'test@example.com')
        self.assertFalse(saved_user.is_admin)
    
    def test_user_password_hashing(self):
        """Test password hashing and verification"""
        user = User(login_id='testuser', full_name='Test User')
        user.set_password('mypassword')
        
        db.session.add(user)
        db.session.commit()
        
        # Verify password is hashed (not stored as plaintext)
        self.assertNotEqual(user.password_hash, 'mypassword')
        
        # Verify password check works
        self.assertTrue(user.check_password('mypassword'))
        self.assertFalse(user.check_password('wrongpassword'))
    
    def test_user_unique_constraints(self):
        """Test unique constraints on user fields"""
        user1 = User(login_id='testuser', full_name='User 1')
        user1.set_password('password')
        db.session.add(user1)
        db.session.commit()
        
        # Try to create user with same login_id
        user2 = User(login_id='testuser', full_name='User 2')
        user2.set_password('password')
        db.session.add(user2)
        
        with self.assertRaises(Exception):
            db.session.commit()
    
    def test_vm_record_creation(self):
        """Test creating a VM record"""
        expires_at = datetime.now() + timedelta(hours=2)
        
        vm = VMRecord(
            vmid=100,
            vm_name='student-testuser-webserver',
            vm_type='lxc',
            node='pve1',
            template_name='homework1/webserver',
            student_name='testuser',
            memory=2048,
            cores=2,
            storage=20,
            ip_address='10.0.0.100',
            expires_at=expires_at,
            destroyed=False
        )
        
        db.session.add(vm)
        db.session.commit()
        
        # Verify VM was created
        saved_vm = VMRecord.query.filter_by(vmid=100).first()
        self.assertIsNotNone(saved_vm)
        self.assertEqual(saved_vm.vmid, 100)
        self.assertEqual(saved_vm.vm_name, 'student-testuser-webserver')
        self.assertEqual(saved_vm.vm_type, 'lxc')
        self.assertEqual(saved_vm.student_name, 'testuser')
        self.assertEqual(saved_vm.memory, 2048)
        self.assertFalse(saved_vm.destroyed)
    
    def test_vm_record_relationships(self):
        """Test VM record relationship with User"""
        user = User(login_id='testuser', full_name='Test User')
        user.set_password('password')
        db.session.add(user)
        db.session.commit()
        
        expires_at = datetime.now() + timedelta(hours=2)
        vm = VMRecord(
            vmid=100,
            vm_name='student-testuser-vm',
            vm_type='lxc',
            node='pve1',
            template_name='homework1/vm1',
            student_name='testuser',
            created_by_user_id=user.id,
            expires_at=expires_at
        )
        db.session.add(vm)
        db.session.commit()
        
        # Verify relationship
        saved_vm = VMRecord.query.filter_by(vmid=100).first()
        self.assertEqual(saved_vm.created_by_user_id, user.id)
        
        # Verify user can access their VMs
        user_vms = VMRecord.query.filter_by(created_by_user_id=user.id).all()
        self.assertEqual(len(user_vms), 1)
    
    def test_vm_record_query_by_student(self):
        """Test querying VMs by student name"""
        expires_at = datetime.now() + timedelta(hours=2)
        
        vm1 = VMRecord(
            vmid=100, vm_name='student-alice-vm1', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='alice',
            expires_at=expires_at
        )
        vm2 = VMRecord(
            vmid=101, vm_name='student-alice-vm2', vm_type='lxc',
            node='pve1', template_name='hw1/vm2', student_name='alice',
            expires_at=expires_at
        )
        vm3 = VMRecord(
            vmid=102, vm_name='student-bob-vm1', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='bob',
            expires_at=expires_at
        )
        
        db.session.add_all([vm1, vm2, vm3])
        db.session.commit()
        
        # Query Alice's VMs
        alice_vms = VMRecord.query.filter_by(student_name='alice').all()
        self.assertEqual(len(alice_vms), 2)
        
        # Query Bob's VMs
        bob_vms = VMRecord.query.filter_by(student_name='bob').all()
        self.assertEqual(len(bob_vms), 1)
    
    def test_vm_record_expiration(self):
        """Test VM expiration tracking"""
        past_time = datetime.now() - timedelta(hours=1)
        future_time = datetime.now() + timedelta(hours=1)
        
        expired_vm = VMRecord(
            vmid=100, vm_name='expired-vm', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='testuser',
            expires_at=past_time
        )
        active_vm = VMRecord(
            vmid=101, vm_name='active-vm', vm_type='lxc',
            node='pve1', template_name='hw1/vm2', student_name='testuser',
            expires_at=future_time
        )
        
        db.session.add_all([expired_vm, active_vm])
        db.session.commit()
        
        # Query expired VMs
        expired_vms = VMRecord.query.filter(
            VMRecord.expires_at < datetime.now(),
            VMRecord.destroyed == False
        ).all()
        
        self.assertEqual(len(expired_vms), 1)
        self.assertEqual(expired_vms[0].vmid, 100)
    
    def test_vm_record_destroyed_flag(self):
        """Test VM destroyed flag and timestamp"""
        vm = VMRecord(
            vmid=100, vm_name='test-vm', vm_type='lxc',
            node='pve1', template_name='hw1/vm1', student_name='testuser',
            expires_at=datetime.now() + timedelta(hours=1),
            destroyed=False
        )
        db.session.add(vm)
        db.session.commit()
        
        # Mark as destroyed
        vm.destroyed = True
        vm.destroyed_at = datetime.now()
        db.session.commit()
        
        # Verify
        saved_vm = VMRecord.query.filter_by(vmid=100).first()
        self.assertTrue(saved_vm.destroyed)
        self.assertIsNotNone(saved_vm.destroyed_at)


if __name__ == '__main__':
    unittest.main()
