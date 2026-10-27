#!/usr/bin/env python3
"""
Migration: add KOTH tables and vm_records.koth_game_id column. Idempotent.
"""

import os
from sqlalchemy import create_engine, text

db_user = os.environ.get('MYSQL_USER', 'proxmox_user')
db_password = os.environ.get('MYSQL_PASSWORD', 'proxmox_password')
db_host = os.environ.get('MYSQL_HOST', 'mysql')
db_name = os.environ.get('MYSQL_DATABASE', 'proxmox_automation')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def check_table_exists(conn, t):
    r = conn.execute(text("""SELECT COUNT(*) FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t"""), {"d": db_name, "t": t})
    return r.fetchone()[0] > 0


def check_column_exists(conn, t, c):
    r = conn.execute(text("""SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=:d AND TABLE_NAME=:t AND COLUMN_NAME=:c"""),
        {"d": db_name, "t": t, "c": c})
    return r.fetchone()[0] > 0


def run_migration():
    engine = create_engine(db_url)
    print("Starting KOTH migration...")
    with engine.connect() as conn:
        if not check_table_exists(conn, 'koth_games'):
            print("  -> Creating koth_games")
            conn.execute(text("""
                CREATE TABLE koth_games (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    deployment_group_name VARCHAR(255) NOT NULL,
                    entry_vlan_tag INT NOT NULL,
                    internal_vlan_tag INT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    is_active TINYINT(1) NOT NULL DEFAULT 0,
                    pause_other_deployments TINYINT(1) NOT NULL DEFAULT 0,
                    show_scoreboard TINYINT(1) NOT NULL DEFAULT 1,
                    show_network_map TINYINT(1) NOT NULL DEFAULT 1,
                    scan_interval_sec INT NOT NULL DEFAULT 12,
                    claim_cooldown_sec INT NOT NULL DEFAULT 30,
                    box_ownership_score DOUBLE NOT NULL DEFAULT 1,
                    port_open_score DOUBLE NOT NULL DEFAULT 3,
                    exp_scoring_enabled TINYINT(1) NOT NULL DEFAULT 1,
                    exp_val INT NOT NULL DEFAULT 60,
                    scoring_iteration INT NOT NULL DEFAULT 0,
                    started_at DATETIME NULL,
                    ended_at DATETIME NULL,
                    duration_minutes INT NULL,
                    ends_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by INT NULL,
                    INDEX idx_koth_active (is_active),
                    CONSTRAINT ck_koth_games_status CHECK (status IN ('draft','active','ended'))
                ) ENGINE=InnoDB
            """))
            # `active_singleton` is a regular nullable column kept in sync
            # with `is_active` by a SQLAlchemy @validates hook on KothGame.
            # MySQL doesn't support partial unique indexes (`WHERE is_active=1`)
            # and writes to STORED GENERATED columns are rejected, so we use
            # an app-managed sentinel: TRUE when active, NULL otherwise. The
            # UNIQUE index allows multiple NULLs but only one TRUE — exactly
            # the "one active game" invariant we want.
            conn.execute(text("""
                ALTER TABLE koth_games
                ADD COLUMN active_singleton TINYINT(1) NULL,
                ADD UNIQUE KEY uq_koth_active_singleton (active_singleton)
            """))
        if not check_table_exists(conn, 'koth_teams'):
            print("  -> Creating koth_teams")
            conn.execute(text("""
                CREATE TABLE koth_teams (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    student_group_id INT NOT NULL,
                    color VARCHAR(20) NOT NULL,
                    submission_token VARCHAR(64) NOT NULL,
                    display_name VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_koth_team_token (submission_token),
                    UNIQUE KEY uq_koth_team_group_per_game (game_id, student_group_id),
                    UNIQUE KEY uq_koth_team_color_per_game (game_id, color),
                    INDEX idx_koth_team_game (game_id),
                    INDEX idx_koth_team_group (student_group_id),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (student_group_id) REFERENCES student_groups(id)
                ) ENGINE=InnoDB
            """))
        if not check_table_exists(conn, 'koth_boxes'):
            print("  -> Creating koth_boxes")
            conn.execute(text("""
                CREATE TABLE koth_boxes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    vm_record_id INT NULL,
                    name VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255) NULL,
                    ips JSON NOT NULL,
                    services JSON NOT NULL,
                    owner_team_id INT NULL,
                    claimed_at DATETIME NULL,
                    position JSON NULL,
                    claim_file_path VARCHAR(255) NULL,
                    INDEX idx_koth_box_game (game_id),
                    INDEX idx_koth_box_vm (vm_record_id),
                    INDEX idx_koth_box_owner (owner_team_id),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (vm_record_id) REFERENCES vm_records(id),
                    FOREIGN KEY (owner_team_id) REFERENCES koth_teams(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """))
        if not check_table_exists(conn, 'koth_flags'):
            print("  -> Creating koth_flags")
            conn.execute(text("""
                CREATE TABLE koth_flags (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    box_id INT NULL,
                    name VARCHAR(255) NOT NULL,
                    value VARCHAR(512) NOT NULL,
                    points DOUBLE NOT NULL DEFAULT 0,
                    submission_count_limit INT NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_koth_flag_game (game_id),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (box_id) REFERENCES koth_boxes(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """))
        if not check_table_exists(conn, 'koth_flag_submissions'):
            print("  -> Creating koth_flag_submissions")
            conn.execute(text("""
                CREATE TABLE koth_flag_submissions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    flag_id INT NOT NULL,
                    team_id INT NOT NULL,
                    submitted_by_user_id INT NULL,
                    accepted TINYINT(1) NOT NULL DEFAULT 0,
                    accepted_singleton TINYINT(1) AS (CASE WHEN accepted=1 THEN 1 ELSE NULL END) STORED,
                    submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_koth_sub_game (game_id),
                    INDEX idx_koth_sub_flag (flag_id),
                    INDEX idx_koth_sub_team (team_id),
                    UNIQUE KEY uq_koth_flag_sub_accepted (flag_id, team_id, accepted_singleton),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (flag_id) REFERENCES koth_flags(id) ON DELETE CASCADE,
                    FOREIGN KEY (team_id) REFERENCES koth_teams(id) ON DELETE CASCADE
                ) ENGINE=InnoDB
            """))
        if not check_table_exists(conn, 'koth_claim_events'):
            print("  -> Creating koth_claim_events")
            conn.execute(text("""
                CREATE TABLE koth_claim_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    box_id INT NULL,
                    team_id INT NULL,
                    event_type VARCHAR(20) NOT NULL,
                    points_delta DOUBLE NOT NULL DEFAULT 0,
                    meta JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_koth_event_game (game_id),
                    INDEX idx_koth_event_created (created_at),
                    CONSTRAINT ck_koth_claim_event_type CHECK (event_type IN ('claim','reclaim','flag','score_recompute')),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (box_id) REFERENCES koth_boxes(id) ON DELETE SET NULL,
                    FOREIGN KEY (team_id) REFERENCES koth_teams(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """))
        if not check_table_exists(conn, 'koth_score_snapshots'):
            print("  -> Creating koth_score_snapshots")
            conn.execute(text("""
                CREATE TABLE koth_score_snapshots (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    game_id INT NOT NULL,
                    team_id INT NOT NULL,
                    score DOUBLE NOT NULL DEFAULT 0,
                    iteration INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX ix_koth_snap_game_iter (game_id, iteration),
                    FOREIGN KEY (game_id) REFERENCES koth_games(id) ON DELETE CASCADE,
                    FOREIGN KEY (team_id) REFERENCES koth_teams(id) ON DELETE CASCADE
                ) ENGINE=InnoDB
            """))
        if not check_column_exists(conn, 'vm_records', 'koth_game_id'):
            print("  -> Adding vm_records.koth_game_id")
            conn.execute(text("""
                ALTER TABLE vm_records
                ADD COLUMN koth_game_id INT NULL,
                ADD INDEX idx_vm_records_koth_game (koth_game_id),
                ADD CONSTRAINT fk_vm_records_koth_game FOREIGN KEY (koth_game_id)
                  REFERENCES koth_games(id)
            """))
        conn.commit()
    print("KOTH migration complete.")


if __name__ == '__main__':
    run_migration()
