#!/usr/bin/env python3
"""
Migration: add student_groups, student_group_members tables and
vm_records.owner_group_id column. Drops the legacy
student_group_deployment_links table if present.

Idempotent — safe to re-run.
"""

import os
import sys
from sqlalchemy import create_engine, text


db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def check_table_exists(conn, table_name):
    result = conn.execute(text("""
        SELECT COUNT(*) FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = :db_name AND TABLE_NAME = :table_name
    """), {"db_name": db_name, "table_name": table_name})
    return result.fetchone()[0] > 0


def check_column_exists(conn, table_name, column_name):
    result = conn.execute(text("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :db_name
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
    """), {"db_name": db_name, "table_name": table_name, "column_name": column_name})
    return result.fetchone()[0] > 0


def run_migration():
    engine = create_engine(db_url)
    print("Starting student_groups migration...")

    with engine.connect() as conn:
        if not check_table_exists(conn, 'student_groups'):
            print("  -> Creating 'student_groups' table...")
            conn.execute(text("""
                CREATE TABLE student_groups (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    display_name VARCHAR(255) NOT NULL,
                    description TEXT,
                    entry_vlan_tag INT DEFAULT NULL,
                    internal_vlan_tag INT DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by INT DEFAULT NULL,
                    UNIQUE KEY uq_student_groups_name (name),
                    UNIQUE KEY uq_student_groups_entry_vlan (entry_vlan_tag),
                    UNIQUE KEY uq_student_groups_internal_vlan (internal_vlan_tag),
                    INDEX idx_student_groups_name (name),
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            conn.commit()
            print("  -> 'student_groups' created")
        else:
            print("  -> 'student_groups' already exists, skipping")

        if not check_table_exists(conn, 'student_group_members'):
            print("  -> Creating 'student_group_members' table...")
            conn.execute(text("""
                CREATE TABLE student_group_members (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    group_id INT NOT NULL,
                    user_id INT NOT NULL,
                    added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    added_by INT DEFAULT NULL,
                    UNIQUE KEY uq_student_group_members_user (user_id),
                    INDEX idx_student_group_members_group (group_id),
                    FOREIGN KEY (group_id) REFERENCES student_groups(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (added_by) REFERENCES users(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            conn.commit()
            print("  -> 'student_group_members' created")
        else:
            print("  -> 'student_group_members' already exists, skipping")

        if check_table_exists(conn, 'student_group_deployment_links'):
            print("  -> Dropping legacy 'student_group_deployment_links' table...")
            conn.execute(text("DROP TABLE student_group_deployment_links"))
            conn.commit()
            print("  -> 'student_group_deployment_links' dropped")
        else:
            print("  -> 'student_group_deployment_links' already absent, skipping")

        if not check_column_exists(conn, 'vm_records', 'owner_group_id'):
            print("  -> Adding 'owner_group_id' column to vm_records...")
            conn.execute(text("""
                ALTER TABLE vm_records
                ADD COLUMN owner_group_id INT DEFAULT NULL,
                ADD INDEX idx_vm_records_owner_group (owner_group_id),
                ADD CONSTRAINT fk_vm_records_owner_group
                    FOREIGN KEY (owner_group_id) REFERENCES student_groups(id)
                    ON DELETE SET NULL
            """))
            conn.commit()
            print("  -> 'vm_records.owner_group_id' added")
        else:
            print("  -> 'vm_records.owner_group_id' already exists, skipping")

    print("\nMigration completed successfully.")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
