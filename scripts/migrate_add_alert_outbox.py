#!/usr/bin/env python3
"""Add the alert_outbox table that carries operator alerts to delivery.

Idempotent: creates the table (with its dispatch index) when missing, adds
the index to a table that lacks it, and otherwise changes nothing.

The old code never reads the table, so run this before the new code starts;
docs/PRODUCTION_DEPLOYMENT.md step 3 runs every migration first.
"""

import os
import sys

from sqlalchemy import (
    Column, DateTime, Index, Integer, MetaData, String, Table, Text,
    create_engine, inspect,
)


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

DISPATCH_INDEX = 'ix_alert_outbox_status_next_attempt'


def _table():
    """The table as app.models.AlertOutbox declares it. Declared here so the
    script runs without the app's configuration; tests/test_alert_outbox.py
    checks the two stay identical."""
    return Table(
        'alert_outbox', MetaData(),
        Column('id', Integer, primary_key=True),
        Column('alert_type', String(100), nullable=False),
        Column('payload', Text, nullable=False),
        Column('pending_channels', String(255), nullable=False),
        Column('status', String(20), nullable=False),
        Column('attempts', Integer, nullable=False),
        Column('next_attempt_at', DateTime, nullable=False),
        Column('last_error', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('sent_at', DateTime, nullable=True),
        Index(DISPATCH_INDEX, 'status', 'next_attempt_at'),
    )


def run_migration(database_url=None):
    engine = create_engine(database_url or db_url)
    table = _table()

    print("Starting migration: alert_outbox...")
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table('alert_outbox'):
            print("  → Creating 'alert_outbox'...")
            table.create(conn)
            print("\n✓ Migration completed successfully!")
            return
        if not any(index['name'] == DISPATCH_INDEX
                   for index in inspector.get_indexes('alert_outbox')):
            print(f"  → Adding index '{DISPATCH_INDEX}'...")
            next(index for index in table.indexes
                 if index.name == DISPATCH_INDEX).create(conn)
            print("\n✓ Migration completed successfully!")
            return
    print("  ✓ alert_outbox already exists")
    print("\n✓ Database schema is up to date. No migrations needed.")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
