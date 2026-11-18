#!/usr/bin/env python3
"""Add the vlan_layout_record table that holds the VLAN_BANDS layout the
stored student pairs were numbered under.
Idempotent: creates the table when missing and otherwise changes nothing.
The old code never reads it. The new code creates it at startup as well
(db.create_all) and records the layout on its first start; running this
first only makes the schema complete before that start.
"""

import os
import sys

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect

# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def _table():
    """The table as app.models.VlanLayoutRecord declares it. Declared here so
    the script runs without the app's configuration; tests/test_vlan_bands.py
    checks the two stay identical."""
    return Table(
        'vlan_layout_record', MetaData(),
        Column('id', Integer, primary_key=True),
        Column('bands', String(1024), nullable=False),
        Column('recorded_at', DateTime, nullable=False),
    )


def run_migration(database_url=None):
    engine = create_engine(database_url or db_url)
    print("Starting migration: vlan_layout_record...")
    with engine.begin() as conn:
        if not inspect(conn).has_table('vlan_layout_record'):
            print("  → Creating 'vlan_layout_record'...")
            _table().create(conn)
            print("\n✓ Migration completed successfully!")
            return
    print("  ✓ vlan_layout_record already exists")
    print("\n✓ Database schema is up to date. No migrations needed.")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
