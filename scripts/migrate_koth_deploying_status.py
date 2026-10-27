#!/usr/bin/env python3
"""
Follow-up KOTH migration: relax the koth_games.status CHECK constraint to
allow the new 'deploying' value (was draft|active|ended; now also deploying).

Idempotent. Safe to re-run.
"""

import os
from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def has_constraint(conn, table, name):
    r = conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
           WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND CONSTRAINT_NAME=:n"""),
        {"d": db_name, "t": table, "n": name})
    return r.fetchone()[0] > 0


def run_migration():
    engine = create_engine(db_url)
    print("Relaxing koth_games.status CHECK constraint to allow 'deploying'...")
    with engine.connect() as conn:
        if has_constraint(conn, 'koth_games', 'ck_koth_games_status'):
            conn.execute(text("ALTER TABLE koth_games DROP CHECK ck_koth_games_status"))
            print("  -> dropped old CHECK constraint")
        conn.execute(text(
            "ALTER TABLE koth_games ADD CONSTRAINT ck_koth_games_status "
            "CHECK (status IN ('draft','deploying','active','ended'))"))
        print("  -> added new CHECK constraint (draft|deploying|active|ended)")
        conn.commit()
    print("Migration complete.")


if __name__ == '__main__':
    run_migration()
