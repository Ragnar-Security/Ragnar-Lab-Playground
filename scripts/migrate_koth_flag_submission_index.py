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

The repair is one ALTER TABLE: add the STORED generated column and rebuild
the index on (flag_id, team_id, accepted_singleton). The old index allowed at
most one row per (flag, team), so the new one cannot conflict with existing
rows.

Lock and duration
-----------------
Adding a STORED generated column copies the table. The statement asks for
ALGORITHM=COPY, LOCK=SHARED explicitly, so MySQL refuses rather than taking
a stronger lock: reads continue, and every flag submission waits until the
copy finishes. Measured on MySQL 8.0 (docs/PRODUCTION_DEPLOYMENT.md has the
numbers): well under a second for a classroom game's table. Run it between
games anyway; it refuses while a game is active unless --during-game.

Usage:
    python scripts/migrate_koth_flag_submission_index.py --check   # report only
    python scripts/migrate_koth_flag_submission_index.py           # repair

Idempotent. Safe to re-run. MySQL only.
"""

import argparse
import os
import sys
import time

from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

TABLE = 'koth_flag_submissions'
INDEX = 'uq_koth_flag_sub_accepted'
WANTED = ['flag_id', 'team_id', 'accepted_singleton']
ADD_COLUMN = ("ADD COLUMN accepted_singleton TINYINT(1) "
              "AS (CASE WHEN accepted = 1 THEN 1 ELSE NULL END) STORED")


class GameActive(RuntimeError):
    """A KOTH game is running; the repair blocks its flag submissions."""


def has_table(conn, table):
    return conn.execute(text(
        """SELECT COUNT(*) FROM information_schema.TABLES
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"""),
        {"t": table}).scalar() > 0


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


def repair_clauses(conn):
    """The ALTER TABLE clauses still needed; empty when the table is right."""
    clauses = []
    if not has_column(conn, 'accepted_singleton'):
        clauses.append(ADD_COLUMN)
    cols = index_columns(conn)
    if cols != WANTED:
        if cols:
            clauses.append(f"DROP INDEX {INDEX}")
        clauses.append(f"ADD UNIQUE INDEX {INDEX} ({', '.join(WANTED)})")
    return clauses


def active_games(conn):
    if not has_table(conn, 'koth_games'):
        return 0
    return conn.execute(text(
        "SELECT COUNT(*) FROM koth_games WHERE is_active = 1")).scalar()


def run_migration(database_url=None, *, check=False, during_game=False):
    """Repair the table. Returns True when it is correct afterwards.

    `check` reports what would change and changes nothing. Raises
    GameActive while a game is active unless `during_game`.
    """
    engine = create_engine(database_url or db_url)
    print(f"Fixing {TABLE}.{INDEX} (one accepted submission per flag and team)...")
    try:
        with engine.begin() as conn:
            if not has_table(conn, TABLE):
                print(f"  -> {TABLE} does not exist; nothing to do")
                return True
            clauses = repair_clauses(conn)
            if not clauses:
                print(f"  -> already correct: {INDEX} on {', '.join(WANTED)}")
                return True
            rows = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
            print(f"  {TABLE}: {rows} rows; current {INDEX}: "
                  f"{index_columns(conn) or 'missing'}")
            print("  The repair copies the table (ALGORITHM=COPY, LOCK=SHARED): reads "
                  "continue, flag submissions wait until it finishes.")
            if check:
                print("  --check: nothing changed. Needed: " + "; ".join(clauses))
                return False
            games = active_games(conn)
            if games and not during_game:
                raise GameActive(
                    f"{games} KOTH game(s) active: run this between games, or pass "
                    "--during-game to accept that submissions wait for the copy")
            started = time.monotonic()
            conn.execute(text(f"ALTER TABLE {TABLE} {', '.join(clauses)}, "
                              "ALGORITHM=COPY, LOCK=SHARED"))
            print(f"  -> repaired in {time.monotonic() - started:.2f}s: "
                  f"{INDEX} on {', '.join(WANTED)}")
            return True
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--check', action='store_true',
                        help='report what would change; change nothing')
    parser.add_argument('--during-game', action='store_true',
                        help='run even while a KOTH game is active')
    args = parser.parse_args(argv)
    try:
        run_migration(check=args.check, during_game=args.during_game)
    except GameActive as e:
        print(f"Refusing: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
