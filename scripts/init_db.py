"""
Database initialization script for Proxmox VM Automation
Creates all necessary tables if they don't exist and initializes admin account
"""

import time
import os
import sys

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app import production_policy
from app.config import is_production
from app.models import User, StudentWhitelist, VMRecord
from app.template_manager import TemplateConfigError
from sqlalchemy import inspect


class AdminConfigurationError(RuntimeError):
    """Raised when privileged account seeding would use unsafe configuration."""



def init_database():
    """Initialize the database by creating all tables with retries"""
    max_retries = 30
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            app = create_app()
            with app.app_context():
                # Create all tables
                db.create_all()
                print("✓ Database initialized successfully")
                
                # Verify schema
                verify_schema()
                
                # Create admin account if it doesn't exist
                create_admin_account()
                
                # Import students from CSV into whitelist
                import_student_whitelist()

                # The integration suite signs in as a seeded student. Production
                # must not carry that account, so the test compose file asks for
                # it and nothing else does.
                if os.getenv('SEED_TEST_STUDENT') == '1':
                    create_test_student()

                return
        except (AdminConfigurationError, TemplateConfigError, ValueError):
            # Configuration errors (validate_config, SESSION_COOKIE_SAMESITE,
            # the catalog, the admin password) never fix themselves; retrying
            # them would report a misconfigured .env as "Database not ready".
            raise
        except Exception as e:
            retry_count += 1
            if retry_count < max_retries:
                print(
                    f"Database not ready, retrying "
                    f"({retry_count}/{max_retries}): {e}"
                )
                time.sleep(1)
            else:
                print(
                    f"Failed to initialize database after {max_retries} "
                    f"attempts: {e}"
                )
                raise


def verify_schema():
    """Verify that the database schema matches the models"""
    try:
        inspector = inspect(db.engine)
        
        # Check vm_records table
        if 'vm_records' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('vm_records')]

            if 'created_by_user_id' in columns:
                print("✓ Schema verified: vm_records.created_by_user_id exists")
            else:
                print("⚠️  WARNING: vm_records table exists but missing created_by_user_id column")
                print("   Run migration: docker exec proxmox-vm-automation python scripts/migrate_db.py")
                print("   Or drop volume and recreate: docker compose down -v && docker compose up -d")

            # Migration: vm_name used to be UNIQUE, which blocked re-launching a lab
            # after a previous run left a (soft-deleted or otherwise stale) row behind.
            # Drop the unique constraint if present and replace with a plain index.
            try:
                indexes = inspector.get_indexes('vm_records')
                vm_name_idx = next((i for i in indexes if i.get('column_names') == ['vm_name']), None)
                if vm_name_idx and vm_name_idx.get('unique'):
                    idx_name = vm_name_idx['name']
                    print(f"  Migrating: dropping UNIQUE index {idx_name} on vm_records.vm_name")
                    with db.engine.begin() as conn:
                        from sqlalchemy import text
                        conn.execute(text(f"ALTER TABLE vm_records DROP INDEX {idx_name}"))
                        conn.execute(text(f"CREATE INDEX {idx_name} ON vm_records (vm_name)"))
                    print("✓ Schema migrated: vm_records.vm_name is no longer UNIQUE")
                else:
                    print("✓ Schema verified: vm_records.vm_name index is non-unique")
            except Exception as mig_err:
                print(f"⚠️  Could not migrate vm_records.vm_name uniqueness: {mig_err}")

        # Check headscale_key columns
        if 'users' in inspector.get_table_names():
            user_columns = [col['name'] for col in inspector.get_columns('users')]
            if 'headscale_key' in user_columns:
                print("✓ Schema verified: users.headscale_key exists")
            else:
                print("⚠️  WARNING: users table missing headscale_key column")
                print("   Run migration: docker exec proxmox-vm-automation python scripts/migrate_db.py")

        if 'student_whitelist' in inspector.get_table_names():
            wl_columns = [col['name'] for col in inspector.get_columns('student_whitelist')]
            if 'headscale_key' in wl_columns:
                print("✓ Schema verified: student_whitelist.headscale_key exists")
            else:
                print("⚠️  WARNING: student_whitelist table missing headscale_key column")
                print("   Run migration: docker exec proxmox-vm-automation python scripts/migrate_db.py")
    except Exception as e:
        print(f"Note: Could not verify schema: {e}")


def create_admin_account():
    """
    Create default admin account if it doesn't exist
    Also creates accounts for additional admin users listed in ADMIN_USERS
    """
    admin_login = os.getenv('ADMIN_LOGIN_ID', 'admin')
    configured_password = os.getenv('ADMIN_PASSWORD')

    # Existing accounts are not modified here. This guard only prevents a
    # production bootstrap from creating a privileged account with a password
    # that is absent or copied from one of the historical examples.
    existing_admin = User.query.filter_by(login_id=admin_login).first()
    if not existing_admin and is_production():
        if production_policy.admin_password_problem(configured_password):
            raise AdminConfigurationError(
                'Refusing to seed a production admin: set ADMIN_PASSWORD '
                'to an operator-supplied password that is not a known default.'
            )

    admin_password = configured_password or 'admin123'

    if not existing_admin:
        admin = User(
            login_id=admin_login,
            is_admin=True
        )
        admin.set_password(admin_password)
        
        db.session.add(admin)
        db.session.commit()
        
        print(f"✓ Admin account created: {admin_login}")
        print("  The configured password will not be printed.")
    else:
        print(f"✓ Admin account '{admin_login}' already exists")
    
    # Create additional admin accounts from ADMIN_USERS list
    admin_users = os.getenv('ADMIN_USERS', '').strip()
    if admin_users:
        admin_list = [u.strip() for u in admin_users.split(',') if u.strip()]
        
        for admin_id in admin_list:
            existing = User.query.filter_by(login_id=admin_id).first()
            
            if existing:
                # Update existing user to admin if not already
                if not existing.is_admin:
                    existing.is_admin = True
                    db.session.commit()
                    print(f"✓ Promoted '{admin_id}' to admin")
                else:
                    print(f"✓ Admin user '{admin_id}' already exists")
            else:
                print(f"  Note: Admin user '{admin_id}' not registered yet (will be admin upon registration)")



def create_test_student():
    """
    Seed the student account that the integration suite signs in as.

    Call this ONLY when SEED_TEST_STUDENT is "1". A live whitelist must hold
    the real roster and nothing else. The account carries a known password, so
    a copy in production would be a way in.
    """
    if os.getenv('SEED_TEST_STUDENT') != '1':
        print("✓ Test student not requested; skipping")
        return

    test_login = os.getenv('TEST_STUDENT_LOGIN_ID', 'teststudent')
    test_password = os.getenv('TEST_STUDENT_PASSWORD', 'test123')

    if User.query.filter_by(login_id=test_login).first():
        print(f"✓ Test student account '{test_login}' already exists")
        return

    if not StudentWhitelist.query.filter_by(login_id=test_login).first():
        db.session.add(StudentWhitelist(login_id=test_login))

    student = User(login_id=test_login, is_admin=False)
    student.set_password(test_password)
    db.session.add(student)
    db.session.commit()
    print(f"✓ Test student account created: {test_login}")



def import_student_whitelist():
    """
    Import students from students_merged.csv when the whitelist is empty.
    Look in /app/data/ for Docker or ./data/ for local use.
    """
    import csv

    existing_count = StudentWhitelist.query.count()
    if existing_count > 0:
        print(f"✓ Student whitelist already populated ({existing_count} entries)")
        return

    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

    possible_paths = [
        os.path.join(data_dir, 'students_merged.csv'),
        '/app/data/students_merged.csv',
    ]

    csv_path = None
    for path in possible_paths:
        if os.path.exists(path):
            csv_path = path
            break

    if not csv_path:
        print("⚠️  No students_merged.csv file found. The student whitelist is empty.")
        return

    # Determine next available VLAN offset (in case some students already exist)
    max_entry_vlan = db.session.query(db.func.max(StudentWhitelist.entry_vlan_tag)).scalar() or 99
    vlan_offset = max(max_entry_vlan - 100 + 1, 0)

    imported_count = 0
    with open(csv_path, 'r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            login_id = row.get('SIS Login ID', '').strip().lower()
            sis_user_id = row.get('SIS User ID', '').strip()
            full_name = row.get('Student', '').strip() if 'Student' in row else None
            headscale_key = row.get('headscale_key', '').strip()

            if not login_id:
                continue

            existing = StudentWhitelist.query.filter_by(login_id=login_id).first()
            if existing:
                continue

            entry_vlan = 100 + vlan_offset
            internal_vlan = 200 + vlan_offset
            vlan_offset += 1

            student = StudentWhitelist(
                login_id=login_id,
                sis_user_id=sis_user_id,
                full_name=full_name,
                entry_vlan_tag=entry_vlan,
                internal_vlan_tag=internal_vlan,
                headscale_key=headscale_key or None,
            )
            db.session.add(student)
            key_info = f", headscale key: {'yes' if headscale_key else 'no'}"
            print(f"  Added {login_id} (entry VLAN: {entry_vlan}, internal VLAN: {internal_vlan}{key_info})")
            imported_count += 1

    db.session.commit()
    print(f"✓ Imported {imported_count} students into whitelist from {csv_path}")
    print(f"  VLAN range: entry 100-{99 + vlan_offset}, internal 200-{199 + vlan_offset}")



if __name__ == '__main__':
    init_database()

