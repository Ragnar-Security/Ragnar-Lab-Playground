#!/usr/bin/env python3
"""
Follow-up KOTH migration: add `koth_boxes.claim_file_path` for the new
file-poll claim mechanism.

The KOTH server now reads each box's claim file via the QEMU guest agent
on every scoring tick (TryHackMe-style). The path is per-box, sourced
from `templates.yaml` at materialize time. Default `/koth/claim` for
Linux templates; Windows templates override to `C:\\koth\\claim`.

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
    print("Adding koth_boxes.claim_file_path...")
    with engine.connect() as conn:
        if not has_column(conn, 'koth_boxes', 'claim_file_path'):
            conn.execute(text(
                "ALTER TABLE koth_boxes ADD COLUMN claim_file_path VARCHAR(255) NULL"))
            print("  -> added claim_file_path")
        else:
            print("  -> claim_file_path already exists")
        # Backfill existing rows so the new poller has a path to read.
        # Linux default; admins can edit per-box from the admin UI later.
        n = conn.execute(text(
            "UPDATE koth_boxes SET claim_file_path = '/koth/claim' "
            "WHERE claim_file_path IS NULL")).rowcount
        print(f"  -> backfilled {n} row(s) with '/koth/claim'")
        conn.commit()
    print("Migration complete.")


if __name__ == '__main__':
    run_migration()
