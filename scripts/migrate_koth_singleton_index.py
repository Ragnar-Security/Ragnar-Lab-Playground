#!/usr/bin/env python3
"""
Follow-up KOTH migration: fix the broken `uq_koth_active_singleton` index.

Background
----------
The original migration created `uq_koth_active_singleton` as a plain
`UNIQUE(is_active)` because MySQL does not support partial indexes
(`... WHERE is_active = 1`, which is what SQLAlchemy's `sqlite_where=` only
honors for SQLite). On MySQL that meant **at most two** koth_games rows
total — one with is_active=1 and one with is_active=0 — which surfaces
as `pymysql.err.IntegrityError: (1062, "Duplicate entry '0' ...")` the
moment you try to create a second draft after any prior game has been
ended (its row holds the is_active=0 slot forever).

This migration:
  1. Drops the bogus UNIQUE(is_active) index.
  2. Adds an `active_singleton` BOOLEAN NULL column.
  3. Backfills it: TRUE when is_active=1, NULL otherwise.
  4. Adds a real UNIQUE(active_singleton) index. Both MySQL and SQLite
     allow multiple NULL values in a UNIQUE index but only one TRUE.

The application now keeps `active_singleton` in lockstep with `is_active`
via a SQLAlchemy `@validates` hook on the model.

Idempotent. Safe to re-run.
"""

import os
from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def has_index(conn, table, index):
    r = conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND INDEX_NAME=:i"""),
        {"d": db_name, "t": table, "i": index})
    return r.fetchone()[0] > 0


def has_column(conn, table, column):
    r = conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"d": db_name, "t": table, "c": column})
    return r.fetchone()[0] > 0


def column_is_generated(conn, table, column):
    r = conn.execute(text(
        """SELECT EXTRA FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"d": db_name, "t": table, "c": column})
    row = r.fetchone()
    if not row:
        return False
    return 'GENERATED' in (row[0] or '').upper()


def index_columns(conn, table, index):
    r = conn.execute(text(
        """SELECT COLUMN_NAME FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND INDEX_NAME=:i
           ORDER BY SEQ_IN_INDEX"""),
        {"d": db_name, "t": table, "i": index})
    return [row[0] for row in r.fetchall()]


def run_migration():
    engine = create_engine(db_url)
    print("Fixing koth_games.uq_koth_active_singleton (single-active-game index)...")
    with engine.connect() as conn:
        # 1. Drop bogus UNIQUE(is_active) if it's still there with that shape.
        if has_index(conn, 'koth_games', 'uq_koth_active_singleton'):
            cols = index_columns(conn, 'koth_games', 'uq_koth_active_singleton')
            if cols == ['is_active']:
                conn.execute(text(
                    "ALTER TABLE koth_games DROP INDEX uq_koth_active_singleton"))
                print("  -> dropped old UNIQUE(is_active) index")
            elif cols == ['active_singleton']:
                print("  -> index already on active_singleton; nothing to drop")
            else:
                print(f"  !! unexpected index columns {cols} — leaving alone")

        # 2. Ensure active_singleton exists as a *regular* nullable column.
        # If a previous run of migrate_add_koth.py created it as a STORED
        # GENERATED column, drop and recreate — the app now writes the value
        # directly via a SQLAlchemy @validates hook, and writes to generated
        # columns are rejected by MySQL.
        if has_column(conn, 'koth_games', 'active_singleton'):
            if column_is_generated(conn, 'koth_games', 'active_singleton'):
                # Drop any UNIQUE index that depends on it first.
                if has_index(conn, 'koth_games', 'uq_koth_active_singleton'):
                    conn.execute(text(
                        "ALTER TABLE koth_games DROP INDEX uq_koth_active_singleton"))
                    print("  -> dropped UNIQUE index that referenced generated column")
                conn.execute(text(
                    "ALTER TABLE koth_games DROP COLUMN active_singleton"))
                print("  -> dropped generated active_singleton column")
                conn.execute(text(
                    "ALTER TABLE koth_games ADD COLUMN active_singleton TINYINT(1) NULL"))
                print("  -> added regular active_singleton column")
            else:
                print("  -> active_singleton column already a regular column")
        else:
            conn.execute(text(
                "ALTER TABLE koth_games ADD COLUMN active_singleton TINYINT(1) NULL"))
            print("  -> added active_singleton column")

        # 3. Backfill: 1 where is_active=1, NULL otherwise.
        conn.execute(text(
            "UPDATE koth_games "
            "SET active_singleton = CASE WHEN is_active = 1 THEN 1 ELSE NULL END"))
        print("  -> backfilled active_singleton from is_active")

        # 4. (Re)create the proper UNIQUE index on active_singleton.
        if not has_index(conn, 'koth_games', 'uq_koth_active_singleton'):
            conn.execute(text(
                "ALTER TABLE koth_games "
                "ADD UNIQUE INDEX uq_koth_active_singleton (active_singleton)"))
            print("  -> added UNIQUE(active_singleton) index")
        else:
            cols = index_columns(conn, 'koth_games', 'uq_koth_active_singleton')
            if cols != ['active_singleton']:
                conn.execute(text(
                    "ALTER TABLE koth_games DROP INDEX uq_koth_active_singleton"))
                conn.execute(text(
                    "ALTER TABLE koth_games "
                    "ADD UNIQUE INDEX uq_koth_active_singleton (active_singleton)"))
                print("  -> rebuilt UNIQUE(active_singleton) index")
            else:
                print("  -> UNIQUE(active_singleton) index already correct")

        conn.commit()
    print("Migration complete.")


if __name__ == '__main__':
    run_migration()
