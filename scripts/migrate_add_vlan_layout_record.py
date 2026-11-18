#!/usr/bin/env python3
"""Add the vlan_layout_record table and seed it with the VLAN layout the
stored student pairs were numbered under.

    python scripts/migrate_add_vlan_layout_record.py --bands '<pre-upgrade layout>'
    python scripts/migrate_add_vlan_layout_record.py --site-env <pre-upgrade site.env>
    python scripts/migrate_add_vlan_layout_record.py            # only with no stored pair

The portal refuses to start while student pairs are stored and no layout is
recorded, because only the operator knows which layout numbered them; on an
upgrade that is the layout the site ran before it: JUMPBOX_VLAN_BANDS in the
pre-upgrade .env or site.env, or, where that was unset, the pre-upgrade
default `entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4`.
--site-env reads VLAN_BANDS, else JUMPBOX_VLAN_BANDS, from that file, else
that default. The layout is validated against every stored pair (each tag in
a band; a layout without an `internal` band, from before per-student
internal VLANs were routed, is not checked for internal tags) and recorded
in canonical form. With no stored pair a layout is optional: without one the
portal records its own VLAN_BANDS on its first start.

Idempotent: creates the table when missing; an existing record is kept, and
a different --bands is refused (scripts/accept_vlan_layout.py changes it).
"""

import argparse
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, create_engine,
                        inspect, select, text)

REPO = Path(__file__).resolve().parent.parent
# The layout every release before VLAN_BANDS shipped as its default.
PRE_VLAN_BANDS_DEFAULT = "entry:100-200:0:0 group-entry:300-399:200:2 group-internal:400-500:300:4"

# Match the deployment defaults used by the other standalone migrations.
db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def _load(name, relative):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


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


def bands_from_site_env(path):
    """The pre-upgrade layout a site.env sets: VLAN_BANDS, else its old name
    JUMPBOX_VLAN_BANDS, else the pre-upgrade default."""
    merge = _load('_build_students_merged', 'scripts/build_students_merged.py')
    return (merge.site_env_value(path, 'VLAN_BANDS')
            or merge.site_env_value(path, 'JUMPBOX_VLAN_BANDS')
            or PRE_VLAN_BANDS_DEFAULT)


def _stored_pairs(conn):
    """(owner, entry, internal) for every user and whitelist row with a tag."""
    inspector = inspect(conn)
    pairs = []
    for table, kind in (('student_whitelist', 'whitelisted student'), ('users', 'user')):
        if not inspector.has_table(table):
            continue
        columns = {c['name'] for c in inspector.get_columns(table)}
        if not {'login_id', 'entry_vlan_tag', 'internal_vlan_tag'} <= columns:
            continue
        rows = conn.execute(text(
            f"SELECT login_id, entry_vlan_tag, internal_vlan_tag FROM {table} "
            "WHERE entry_vlan_tag IS NOT NULL OR internal_vlan_tag IS NOT NULL "
            "ORDER BY login_id"))
        pairs += [(f"{kind} {login}", entry, internal) for login, entry, internal in rows]
    return pairs


def _pair_problems(pairs, bands):
    """Stored tags the layout `bands` does not route."""
    names = {band.name for band in bands}
    routed = lambda tag: any(band.first <= tag <= band.last for band in bands)  # noqa: E731
    problems = []
    for owner, entry, internal in pairs:
        if entry is not None and not routed(entry):
            problems.append(f"{owner}: entry VLAN {entry} is in no band")
        if 'internal' in names and internal is not None and not routed(internal):
            problems.append(f"{owner}: internal VLAN {internal} is in no band")
    return problems


def run_migration(database_url=None, bands=None):
    """Create the table and seed it; SystemExit with the reason on refusal."""
    slots = _load('_lab_network_slots', 'app/backends/network_slots.py')
    canonical = None
    if bands is not None:
        try:
            canonical = slots.canonical_vlan_bands(slots.parse_vlan_bands(bands))
        except ValueError as error:
            raise SystemExit(f"the layout is not valid VLAN_BANDS: {error}") from None
    engine = create_engine(database_url or db_url)
    table = _table()
    print("Starting migration: vlan_layout_record...")
    with engine.begin() as conn:
        if not inspect(conn).has_table('vlan_layout_record'):
            print("  → Creating 'vlan_layout_record'...")
            table.create(conn)
        recorded = conn.execute(select(table.c.bands).where(table.c.id == 1)).scalar()
        if recorded is not None:
            if canonical is not None and canonical != recorded:
                raise SystemExit(
                    f"the database already records {recorded}; it is not replaced here. "
                    "Change it between terms with scripts/accept_vlan_layout.py.")
            print(f"  ✓ layout already recorded: {recorded}")
            return
        pairs = _stored_pairs(conn)
        if canonical is None:
            if pairs:
                raise SystemExit(
                    f"{len(pairs)} student VLAN pair(s) are stored, so the layout they were "
                    "numbered under must be given: --bands '<pre-upgrade layout>' or "
                    "--site-env <pre-upgrade site.env>")
            print("  ✓ no student pair stored; the portal records its layout on first start")
            return
        problems = _pair_problems(pairs, slots.parse_vlan_bands(canonical))
        if problems:
            raise SystemExit("the layout does not route these stored pairs, so it is not the "
                             "one they were numbered under:\n  - " + "\n  - ".join(problems))
        conn.execute(table.insert().values(id=1, bands=canonical, recorded_at=datetime.now()))
        print(f"  → recorded {canonical} for {len(pairs)} stored pair(s)")
    print("\n✓ Migration completed successfully!")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument('--bands', help='the pre-upgrade VLAN layout')
    layout.add_argument('--site-env', help='a pre-upgrade site.env holding it')
    args = parser.parse_args(argv)
    bands = args.bands if args.site_env is None else bands_from_site_env(args.site_env)
    try:
        run_migration(bands=bands)
    except SystemExit as refusal:
        print(f"❌ Migration refused: {refusal}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"❌ Migration failed: {str(e)}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
