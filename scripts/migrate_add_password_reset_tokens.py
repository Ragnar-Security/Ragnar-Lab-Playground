#!/usr/bin/env python3
"""Add the password_reset_tokens table for admin-issued reset links.

Idempotent: a second run finds the table and changes nothing. The table is
new, so the old code ignores it and the migration may run before or after
the new code starts. Production's `db.create_all()` at container start would
also create it; this script makes the step explicit and checkable.
"""

import os
import sys

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, MetaData, String, Table,
    create_engine, inspect,
)


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def _tables():
    """The table as app.models.PasswordResetToken declares it. Declared here
    rather than imported so the script runs without the app's configuration;
    tests/test_password_reset_links.py checks the two stay identical."""
    metadata = MetaData()
    # Only so the foreign keys resolve; never created by this script.
    Table('users', metadata, Column('id', Integer, primary_key=True))
    return Table(
        'password_reset_tokens', metadata,
        Column('id', Integer, primary_key=True),
        # Links die with their user, so scripts/purge_roster.sql can still
        # delete students; the issuing admin may be removed too.
        Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'),
               nullable=False, index=True),
        Column('created_by_user_id', Integer, ForeignKey('users.id', ondelete='SET NULL'),
               nullable=True),
        Column('token_hash', String(64), unique=True, nullable=False),
        Column('created_at', DateTime, nullable=False),
        Column('expires_at', DateTime, nullable=False),
        Column('used_at', DateTime, nullable=True),
    )


def run_migration(database_url=None):
    """Create password_reset_tokens once."""
    engine = create_engine(database_url or db_url)

    print("Starting migration: add 'password_reset_tokens' table...")

    with engine.begin() as conn:
        if inspect(conn).has_table('password_reset_tokens'):
            print("  ✓ Table 'password_reset_tokens' already exists")
            print("\n✓ Database schema is up to date. No migrations needed.")
            return

        print("  → Creating 'password_reset_tokens'...")
        _tables().create(conn)
        print("\n✓ Migration completed successfully!")
        print("  - Added: password_reset_tokens table")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
