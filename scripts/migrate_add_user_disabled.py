#!/usr/bin/env python3
"""
Migration script to add 'disabled' column to users table
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


def run_migration():
    """Run migration to add disabled column to users table"""
    engine = create_engine(db_url)

    print("Starting migration: add 'disabled' column to users table...")

    with engine.connect() as conn:
        if not check_column_exists(conn, 'users', 'disabled', db_name):
            print("  → Adding 'disabled' column to users...")
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE,
                ADD INDEX idx_disabled (disabled)
            """))
            conn.commit()
            print("\n✓ Migration completed successfully!")
            print("  - Added: users.disabled column")
        else:
            print("  ✓ Column 'disabled' already exists in users")
            print("\n✓ Database schema is up to date. No migrations needed.")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
