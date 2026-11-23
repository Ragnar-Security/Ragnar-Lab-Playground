#!/usr/bin/env python3
"""Add the protected_vlans table: the group and KOTH internal VLANs the
jumpbox and the Headscale policy were verified to keep from the VPN.

Idempotent: creates the table when missing and otherwise changes nothing.
Run it before the new code starts; the code reads the table on every VLAN
allocation and on every Proxmox group or KOTH deployment.
"""

import os
import sys

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, inspect


# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def _table():
    """The table as app.models.ProtectedVlan declares it. Declared here so the
    script runs without the app's configuration; tests/test_network_protection.py
    checks the two stay identical."""
    return Table(
        'protected_vlans', MetaData(),
        Column('tag', Integer, primary_key=True, autoincrement=False),
        Column('verified_at', DateTime, nullable=False),
    )


def run_migration(database_url=None):
    engine = create_engine(database_url or db_url)
    print("Starting migration: protected_vlans...")
    with engine.begin() as conn:
        if inspect(conn).has_table('protected_vlans'):
            print("  ✓ protected_vlans already exists")
            print("\n✓ Database schema is up to date. No migrations needed.")
            return
        print("  → Creating 'protected_vlans'...")
        _table().create(conn)
    print("\n✓ Migration completed successfully!")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
