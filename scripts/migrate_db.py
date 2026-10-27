#!/usr/bin/env python3
"""
Migration script to update database schema with new columns and tables
"""

import os
import sys
from sqlalchemy import create_engine, text

# Get database URL from environment
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

def check_column_exists(conn, table_name, column_name, db_name):
    """Check if a column exists in a table"""
    result = conn.execute(text("""
        SELECT COUNT(*) as count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :db_name
        AND TABLE_NAME = :table_name
        AND COLUMN_NAME = :column_name
    """), {"db_name": db_name, "table_name": table_name, "column_name": column_name})
    return result.fetchone()[0] > 0

def check_table_exists(conn, table_name, db_name):
    """Check if a table exists"""
    result = conn.execute(text("""
        SELECT COUNT(*) as count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = :db_name
        AND TABLE_NAME = :table_name
    """), {"db_name": db_name, "table_name": table_name})
    return result.fetchone()[0] > 0

def run_migration():
    """Run all database migrations"""
    engine = create_engine(db_url)
    
    print("Starting database migration...")
    
    with engine.connect() as conn:
        migrations_applied = []
        
        # Migration 1: Add created_by_user_id column
        if not check_column_exists(conn, 'vm_records', 'created_by_user_id', db_name):
            print("  → Adding 'created_by_user_id' column to vm_records...")
            conn.execute(text("""
                ALTER TABLE vm_records
                ADD COLUMN created_by_user_id INT DEFAULT NULL,
                ADD INDEX idx_created_by_user_id (created_by_user_id),
                ADD FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            """))
            conn.commit()
            migrations_applied.append("created_by_user_id column")
        else:
            print("  ✓ Column 'created_by_user_id' already exists")
        
        # Migration 2: Add deletion_retry_count column
        if not check_column_exists(conn, 'vm_records', 'deletion_retry_count', db_name):
            print("  → Adding 'deletion_retry_count' column to vm_records...")
            conn.execute(text("""
                ALTER TABLE vm_records
                ADD COLUMN deletion_retry_count INT DEFAULT 0 NOT NULL
            """))
            conn.commit()
            migrations_applied.append("deletion_retry_count column")
        else:
            print("  ✓ Column 'deletion_retry_count' already exists")
        
        # Migration 3: Add headscale_key to student_whitelist
        if not check_column_exists(conn, 'student_whitelist', 'headscale_key', db_name):
            print("  → Adding 'headscale_key' column to student_whitelist...")
            conn.execute(text("""
                ALTER TABLE student_whitelist
                ADD COLUMN headscale_key VARCHAR(255) DEFAULT NULL
            """))
            conn.commit()
            migrations_applied.append("student_whitelist.headscale_key column")
        else:
            print("  ✓ Column 'headscale_key' already exists in student_whitelist")

        # Migration 4: Add headscale_key to users
        if not check_column_exists(conn, 'users', 'headscale_key', db_name):
            print("  → Adding 'headscale_key' column to users...")
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN headscale_key VARCHAR(255) DEFAULT NULL
            """))
            conn.commit()
            migrations_applied.append("users.headscale_key column")
        else:
            print("  ✓ Column 'headscale_key' already exists in users")

        # Migration 5: Create admin_alerts table if it doesn't exist
        if not check_table_exists(conn, 'admin_alerts', db_name):
            print("  → Creating 'admin_alerts' table...")
            conn.execute(text("""
                CREATE TABLE admin_alerts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    alert_type VARCHAR(100) NOT NULL,
                    severity VARCHAR(50) NOT NULL,
                    message TEXT NOT NULL,
                    vmid INT,
                    vm_name VARCHAR(255),
                    details TEXT,
                    created_at DATETIME NOT NULL,
                    acknowledged BOOLEAN DEFAULT FALSE,
                    acknowledged_at DATETIME,
                    INDEX idx_alert_type (alert_type),
                    INDEX idx_created_at (created_at),
                    INDEX idx_acknowledged (acknowledged),
                    INDEX idx_vmid (vmid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            conn.commit()
            migrations_applied.append("admin_alerts table")
        else:
            print("  ✓ Table 'admin_alerts' already exists")
        
        if migrations_applied:
            print("\n✓ Migration completed successfully!")
            for item in migrations_applied:
                print(f"  - Added: {item}")
        else:
            print("\n✓ Database schema is up to date. No migrations needed.")

if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
