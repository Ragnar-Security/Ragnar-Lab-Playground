#!/usr/bin/env python3
"""Add the password_reset_tokens table for admin-issued reset links, with at
most one row per user.

Idempotent for every starting state:

- No table: create it, user_id unique from the start.
- The table as this migration's first release (or db.create_all() of that
  release's model) made it, with a plain index on user_id: keep each user's
  newest row, add the unique index, drop the plain one.
- Already unique: change nothing.

Run it before the new code serves reset links; docs/PRODUCTION_DEPLOYMENT.md
step 3 runs every migration first. On MySQL each DDL statement commits by
itself, so an old-code link issued between the duplicate cleanup and the
CREATE UNIQUE INDEX can make that statement fail; nothing is half-applied,
and a re-run finishes the job.
"""

import os
import sys

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table,
    UniqueConstraint, create_engine, delete, inspect, select,
)


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

UNIQUE_USER = 'uq_password_reset_tokens_user_id'
# What Column(index=True) named the plain index in the first release.
PREVIOUS_USER_INDEX = 'ix_password_reset_tokens_user_id'


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
               nullable=False),
        Column('created_by_user_id', Integer, ForeignKey('users.id', ondelete='SET NULL'),
               nullable=True),
        Column('token_hash', String(64), unique=True, nullable=False),
        Column('created_at', DateTime, nullable=False),
        Column('expires_at', DateTime, nullable=False),
        Column('used_at', DateTime, nullable=True),
        UniqueConstraint('user_id', name=UNIQUE_USER),
    )


def _user_id_is_unique(inspector):
    unique = [i['column_names'] for i in inspector.get_indexes('password_reset_tokens')
              if i['unique']]
    unique += [u['column_names']
               for u in inspector.get_unique_constraints('password_reset_tokens')]
    return ['user_id'] in unique


def _delete_all_but_newest(conn, table):
    """Keep each user's newest row (latest created_at, then highest id): the
    newer link is the one meant to retire the others. Returns the count."""
    rows = conn.execute(
        select(table.c.id, table.c.user_id)
        .order_by(table.c.user_id, table.c.created_at.desc(), table.c.id.desc())
    ).all()
    kept_users = set()
    stale = []
    for row_id, user_id in rows:
        if user_id in kept_users:
            stale.append(row_id)
        kept_users.add(user_id)
    if stale:
        conn.execute(delete(table).where(table.c.id.in_(stale)))
    return len(stale)


def run_migration(database_url=None):
    """Create password_reset_tokens, or make user_id unique on an existing one."""
    engine = create_engine(database_url or db_url)
    table = _tables()

    print("Starting migration: password_reset_tokens with one link per user...")

    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table('password_reset_tokens'):
            print("  → Creating 'password_reset_tokens'...")
            table.create(conn)
            print("\n✓ Migration completed successfully!")
            print("  - Added: password_reset_tokens table")
            return

        if _user_id_is_unique(inspector):
            print("  ✓ password_reset_tokens.user_id is already unique")
            print("\n✓ Database schema is up to date. No migrations needed.")
            return

        removed = _delete_all_but_newest(conn, table)
        print(f"  → Removed {removed} superseded link(s), keeping each user's newest")
        print(f"  → Adding unique index '{UNIQUE_USER}'...")
        # Before dropping the plain index: MySQL refuses to drop the only
        # index its user_id foreign key can use.
        Index(UNIQUE_USER, table.c.user_id, unique=True).create(conn)
        if any(i['name'] == PREVIOUS_USER_INDEX and not i['unique']
               for i in inspector.get_indexes('password_reset_tokens')):
            print(f"  → Dropping redundant index '{PREVIOUS_USER_INDEX}'...")
            Index(PREVIOUS_USER_INDEX, table.c.user_id).drop(conn)

    print("\n✓ Migration completed successfully!")
    print("  - password_reset_tokens.user_id is now unique")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
