#!/usr/bin/env python3
"""
Follow-up KOTH migration: fix the `uq_koth_flag_sub_accepted` index on MySQL.

Background
----------
`scripts/migrate_add_koth.py` creates `koth_flag_submissions` with a
generated `accepted_singleton` column (1 when accepted, NULL otherwise) and
`UNIQUE(flag_id, team_id, accepted_singleton)`. But `db.create_all()` runs on
every container start (`scripts/init_db.py`), and until the model declared
that column it built the table first, as a plain `UNIQUE(flag_id, team_id)`:
MySQL has no partial indexes, so the model's `sqlite_where` was dropped.
On such a table a team's second wrong guess fails with
`IntegrityError (1062, "Duplicate entry ... for key 'uq_koth_flag_sub_accepted'")`
(a 500), because wrong guesses are recorded against the game's first flag,
and its first wrong guess blocks that flag for the team.

This migration:
  1. Adds the generated `accepted_singleton` column if it is missing.
  2. Rebuilds `uq_koth_flag_sub_accepted` on
     (flag_id, team_id, accepted_singleton) if it has any other shape.
     The old index allowed at most one row per (flag, team), so the new
     one cannot conflict with existing rows.

Idempotent. Safe to re-run. MySQL only.
"""

import os
import sys

from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

TABLE = 'koth_flag_submissions'
INDEX = 'uq_koth_flag_sub_accepted'
WANTED = ['flag_id', 'team_id', 'accepted_singleton']


def has_column(conn, column):
    return conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"t": TABLE, "c": column}).scalar() > 0


def index_columns(conn):
    rows = conn.execute(text(
        """SELECT COLUMN_NAME FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND INDEX_NAME=:i
           ORDER BY SEQ_IN_INDEX"""),
        {"t": TABLE, "i": INDEX}).fetchall()
    return [row[0] for row in rows]


def run_migration(database_url=None):
    engine = create_engine(database_url or db_url)
    print(f"Fixing {TABLE}.{INDEX} (one accepted submission per flag and team)...")
    with engine.begin() as conn:
        if has_column(conn, 'accepted_singleton'):
            print("  -> accepted_singleton column already present")
        else:
            conn.execute(text(
                f"ALTER TABLE {TABLE} ADD COLUMN accepted_singleton TINYINT(1) "
                "AS (CASE WHEN accepted = 1 THEN 1 ELSE NULL END) STORED"))
            print("  -> added generated accepted_singleton column")

        cols = index_columns(conn)
        if cols == WANTED:
            print(f"  -> {INDEX} already on {', '.join(WANTED)}")
        else:
            drop = f"DROP INDEX {INDEX}, " if cols else ""
            conn.execute(text(
                f"ALTER TABLE {TABLE} {drop}"
                f"ADD UNIQUE INDEX {INDEX} ({', '.join(WANTED)})"))
            print(f"  -> rebuilt {INDEX}: {cols or 'missing'} -> {WANTED}")
    print("Migration complete.")


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
