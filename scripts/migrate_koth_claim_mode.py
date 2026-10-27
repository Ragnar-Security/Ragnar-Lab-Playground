#!/usr/bin/env python3
"""
Migration: KOTH classroom-scale changes. Idempotent.

Adds:
  koth_games.claim_mode  VARCHAR(10) NOT NULL DEFAULT 'file'
      How the scorer captures box ownership: 'file' (Proxmox guest-agent
      read, one API call per box per tick), 'beacon' (TCP read off the
      service probe we already make, zero API calls), or 'both'.
      Defaults to 'file' so games that predate this column keep behaving
      exactly as they did. New games get KOTH_DEFAULT_CLAIM_MODE.
  koth_games.claim_port  INT NOT NULL DEFAULT 31337
      Port the claim beacon listens on.
  koth_teams.score       FLOAT NOT NULL DEFAULT 0
      Authoritative live score. Backfilled from each team's latest
      koth_score_snapshots row so an in-flight game does not reset to
      zero when this is applied.

Also adds ix_koth_snap_game_team_iter on koth_score_snapshots so the
history table can be queried per team without a filesort.
"""

import os
from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def check_column_exists(conn, table, column):
    r = conn.execute(text("""SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"d": db_name, "t": table, "c": column})
    return r.fetchone()[0] > 0


def check_index_exists(conn, table, index):
    r = conn.execute(text("""SELECT COUNT(*) FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND INDEX_NAME=:i"""),
        {"d": db_name, "t": table, "i": index})
    return r.fetchone()[0] > 0


def run_migration():
    engine = create_engine(db_url)
    print("Starting KOTH claim-mode migration...")
    with engine.connect() as conn:
        if not check_column_exists(conn, 'koth_games', 'claim_mode'):
            print("  -> Adding koth_games.claim_mode")
            conn.execute(text(
                "ALTER TABLE koth_games ADD COLUMN claim_mode "
                "VARCHAR(10) NOT NULL DEFAULT 'file'"))
        else:
            print("  -- koth_games.claim_mode already present")

        if not check_column_exists(conn, 'koth_games', 'claim_port'):
            print("  -> Adding koth_games.claim_port")
            conn.execute(text(
                "ALTER TABLE koth_games ADD COLUMN claim_port "
                "INT NOT NULL DEFAULT 31337"))
        else:
            print("  -- koth_games.claim_port already present")

        if not check_column_exists(conn, 'koth_teams', 'score'):
            print("  -> Adding koth_teams.score")
            conn.execute(text(
                "ALTER TABLE koth_teams ADD COLUMN score "
                "FLOAT NOT NULL DEFAULT 0"))
            # Backfill from the latest snapshot per team so a game that is
            # live when this runs keeps its scoreboard. Without this every
            # team silently drops to zero on the next state fetch.
            print("  -> Backfilling koth_teams.score from latest snapshots")
            result = conn.execute(text("""
                UPDATE koth_teams t
                JOIN (
                    SELECT s.team_id, s.score
                    FROM koth_score_snapshots s
                    JOIN (
                        SELECT team_id, MAX(iteration) AS max_iter
                        FROM koth_score_snapshots GROUP BY team_id
                    ) m ON m.team_id = s.team_id AND m.max_iter = s.iteration
                ) latest ON latest.team_id = t.id
                SET t.score = latest.score
            """))
            print(f"     backfilled {result.rowcount} team(s)")
        else:
            print("  -- koth_teams.score already present")

        if not check_index_exists(conn, 'koth_score_snapshots',
                                  'ix_koth_snap_game_team_iter'):
            print("  -> Adding ix_koth_snap_game_team_iter")
            conn.execute(text(
                "CREATE INDEX ix_koth_snap_game_team_iter "
                "ON koth_score_snapshots (game_id, team_id, iteration)"))
        else:
            print("  -- ix_koth_snap_game_team_iter already present")

        conn.commit()
    print("KOTH claim-mode migration complete.")


if __name__ == '__main__':
    run_migration()
