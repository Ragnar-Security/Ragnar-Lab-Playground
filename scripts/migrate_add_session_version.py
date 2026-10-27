#!/usr/bin/env python3
"""Add the revocable-session version column to the users table."""

import os
import sys

from sqlalchemy import create_engine, inspect, text


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def check_column_exists(conn, table_name, column_name):
    """Return whether ``table_name`` already contains ``column_name``."""
    return any(
        column['name'] == column_name
        for column in inspect(conn).get_columns(table_name)
    )


def run_migration(database_url=None):
    """Add users.session_version once, preserving existing users at version 0."""
    engine = create_engine(database_url or db_url)

    print("Starting migration: add 'session_version' column to users table...")

    with engine.begin() as conn:
        if check_column_exists(conn, 'users', 'session_version'):
            print("  ✓ Column 'session_version' already exists in users")
            print("\n✓ Database schema is up to date. No migrations needed.")
            return

        print("  → Adding 'session_version' column to users...")
        conn.execute(text("""
            ALTER TABLE users
            ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0
        """))
        print("\n✓ Migration completed successfully!")
        print("  - Added: users.session_version column")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
