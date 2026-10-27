#!/usr/bin/env python3
"""
Migration script to add student_lab_assignments table
Allows admins to restrict which deployment groups students can access
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
    """Run migration to add student_lab_assignments table"""
    engine = create_engine(db_url)

    print("Starting lab assignments migration...")

    with engine.connect() as conn:
        if not check_table_exists(conn, 'student_lab_assignments', db_name):
            print("  -> Creating 'student_lab_assignments' table...")
            conn.execute(text("""
                CREATE TABLE student_lab_assignments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    deployment_group_name VARCHAR(255) NOT NULL,
                    assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    assigned_by INT DEFAULT NULL,
                    INDEX idx_user_id (user_id),
                    INDEX idx_deployment_group (deployment_group_name),
                    UNIQUE KEY uq_user_deployment (user_id, deployment_group_name),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            conn.commit()
            print("  -> Table 'student_lab_assignments' created successfully")
        else:
            print("  -> Table 'student_lab_assignments' already exists")

    print("\nMigration completed successfully!")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
