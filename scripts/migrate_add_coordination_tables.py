#!/usr/bin/env python3
"""Add the tables that hold cross-worker coordination state.

Node-memory reservations, VMID reservations with the VMID high-water mark,
and self-service password-reset failures move out of process memory into
these tables (see app/coordination.py). The script creates whichever tables
are missing and seeds the coordination_state lock rows. It never changes a
table or row that already exists, so a re-run, or a run after the app's
db.create_all() made the tables at startup, is a no-op.

The old code never reads these tables, so run it before restarting into
the new code; docs/PRODUCTION_DEPLOYMENT.md step 3 runs every migration.
"""

import os
import sys

from sqlalchemy import (
    BigInteger, Column, Double, Index, Integer, MetaData, String, Table,
    create_engine, insert, inspect, select,
)
from sqlalchemy.exc import IntegrityError


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

STATE_ROWS = ('node_admission', 'vmid_allocator', 'password_reset_limiter')


def _tables():
    """The tables as app.models declares them. Declared here rather than
    imported so the script runs without the app's configuration;
    tests/test_cross_worker_coordination.py checks the two stay identical."""
    metadata = MetaData()
    state = Table(
        'coordination_state', metadata,
        Column('name', String(64), primary_key=True),
        Column('value', BigInteger, nullable=False),
    )
    Table(
        'node_memory_reservations', metadata,
        Column('reservation_id', String(64), primary_key=True),
        Column('node', String(255), nullable=False),
        Column('memory_bytes', BigInteger, nullable=False),
        Column('expires_at', Double, nullable=False),
        Index('ix_node_memory_reservations_expires_at', 'expires_at'),
    )
    Table(
        'vmid_reservations', metadata,
        Column('vmid', Integer, primary_key=True, autoincrement=False),
        Column('reservation_id', String(64), nullable=False),
        Column('expires_at', Double, nullable=False),
        Index('ix_vmid_reservations_reservation_id', 'reservation_id'),
        Index('ix_vmid_reservations_expires_at', 'expires_at'),
    )
    Table(
        'password_reset_failures', metadata,
        Column('id', Integer, primary_key=True),
        Column('scope', String(16), nullable=False),
        Column('key_hash', String(64), nullable=False),
        Column('failed_at', Double, nullable=False),
        Index('ix_password_reset_failures_key', 'scope', 'key_hash', 'failed_at'),
        Index('ix_password_reset_failures_failed_at', 'failed_at'),
    )
    return metadata, state


def run_migration(database_url=None):
    """Create missing coordination tables and seed their lock rows."""
    engine = create_engine(database_url or db_url)
    metadata, state = _tables()

    print("Starting migration: cross-worker coordination tables...")

    existing = set(inspect(engine).get_table_names())
    for table in metadata.sorted_tables:
        if table.name in existing:
            print(f"  ✓ {table.name} already exists")
        else:
            print(f"  → Creating '{table.name}'...")
            table.create(engine)

    for name in STATE_ROWS:
        try:
            with engine.begin() as conn:
                if conn.execute(
                        select(state.c.name).where(state.c.name == name)
                ).first() is None:
                    print(f"  → Seeding coordination_state row '{name}'")
                    conn.execute(insert(state).values(name=name, value=0))
        except IntegrityError:
            # The running app created the row between the check and insert.
            pass

    print("\n✓ Migration completed successfully!")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
