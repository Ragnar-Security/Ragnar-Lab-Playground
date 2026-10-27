#!/usr/bin/env python3
"""
Follow-up KOTH migration: add `duration_minutes` and `ends_at` columns to
`koth_games` for time-boxed games.

  duration_minutes INT NULL    -- NULL = unlimited
  ends_at          DATETIME NULL  -- computed as started_at + duration_minutes
                                  -- when the game becomes active; recomputed
                                  -- on PATCH if duration changes mid-game.
                                  -- The scoring tick auto-ends the game when
                                  -- now() >= ends_at.

Idempotent. Safe to re-run.
"""

import os
from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def has_column(conn, table, column):
    r = conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"d": db_name, "t": table, "c": column})
    return r.fetchone()[0] > 0


def run_migration():
    engine = create_engine(db_url)
    print("Adding duration_minutes / ends_at to koth_games...")
    with engine.connect() as conn:
        if not has_column(conn, 'koth_games', 'duration_minutes'):
            conn.execute(text(
                "ALTER TABLE koth_games ADD COLUMN duration_minutes INT NULL"))
            print("  -> added duration_minutes")
        else:
            print("  -> duration_minutes already exists")
        if not has_column(conn, 'koth_games', 'ends_at'):
            conn.execute(text(
                "ALTER TABLE koth_games ADD COLUMN ends_at DATETIME NULL"))
            print("  -> added ends_at")
        else:
            print("  -> ends_at already exists")
        conn.commit()
    print("Migration complete.")


if __name__ == '__main__':
    run_migration()
