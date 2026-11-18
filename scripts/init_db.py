"""
Database initialization script for Proxmox VM Automation
Creates all necessary tables if they don't exist and initializes admin account
"""

import time
import os
import sys
from typing import Optional

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app import production_policy
from app.config import is_production
from app.models import User, StudentWhitelist, VMRecord
from app.vlan_allocator import VlanPoolExhausted, user_pair_for_index, vlan_layout
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

    seed = production_policy.seed_admin_account(os.getenv('SEED_ADMIN_ACCOUNT'))
    if seed is None:
        raise AdminConfigurationError(
            'SEED_ADMIN_ACCOUNT must be true or false.'
        )

    # Existing accounts are not modified here, including when seeding is off:
    # retiring an unused generic account is an operator action (PUT
    # /admin/students/<id> with {"disabled": true}), not something a restart
    # may do. The password guard below only prevents a production bootstrap
    # from creating a privileged account with a password that is absent or
    # copied from one of the historical examples; with seeding off nothing is
    # created, so there is no password to vet.
    existing_admin = User.query.filter_by(login_id=admin_login).first()
    if seed and not existing_admin and is_production():
        problem = production_policy.admin_password_problem(configured_password)
        if problem:
            raise AdminConfigurationError(
                f'Refusing to seed a production admin: ADMIN_PASSWORD {problem}.'
            )

    admin_password = configured_password or 'admin123'

    if not seed:
        print("✓ Default admin seeding disabled (SEED_ADMIN_ACCOUNT=false)")
    elif not existing_admin:
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



def import_student_whitelist(csv_path: Optional[str] = None) -> None:
    """
    Import students from students_merged.csv when the whitelist is empty.
    Without `csv_path`, look in ./data/ for local use or /app/data/ for Docker.

    Students are numbered in file order with the per-student pairs of
    VLAN_BANDS (entry.first+k / internal.first+k). A file with more students
    than the entry band holds raises ValueError before any row is added.
    """
    import csv

    existing_count = StudentWhitelist.query.count()
    if existing_count > 0:
        print(f"✓ Student whitelist already populated ({existing_count} entries)")
        return

    if csv_path is None:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

        possible_paths = [
            os.path.join(data_dir, 'students_merged.csv'),
            '/app/data/students_merged.csv',
        ]

        for path in possible_paths:
            if os.path.exists(path):
                csv_path = path
                break

    if not csv_path:
        print("⚠️  No students_merged.csv file found. The student whitelist is empty.")
        return

    new_rows = []
    seen = set()
    with open(csv_path, 'r', encoding='utf-8') as csvfile:
        for row in csv.DictReader(csvfile):
            login_id = row.get('SIS Login ID', '').strip().lower()
            if not login_id or login_id in seen:
                continue
            seen.add(login_id)
            new_rows.append((login_id, row))

    pairs = []
    try:
        for k in range(len(new_rows)):
            pairs.append(user_pair_for_index(k))
    except VlanPoolExhausted:
        raise ValueError(
            f"{csv_path} has {len(new_rows)} students; the per-student VLAN pool "
            f"holds {vlan_layout().roster_max}"
        ) from None

    for (login_id, row), (entry_vlan, internal_vlan) in zip(new_rows, pairs):
        sis_user_id = row.get('SIS User ID', '').strip()
        full_name = row.get('Student', '').strip() if 'Student' in row else None
        headscale_key = row.get('headscale_key', '').strip()

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

    db.session.commit()
    print(f"✓ Imported {len(new_rows)} students into whitelist from {csv_path}")
    if pairs:
        print(f"  VLAN range: entry {pairs[0][0]}-{pairs[-1][0]}, internal {pairs[0][1]}-{pairs[-1][1]}")



if __name__ == '__main__':
    init_database()

