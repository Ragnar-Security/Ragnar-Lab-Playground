#!/usr/bin/env python3
"""Add persistent VM deletion retry state. Idempotent."""

import os

from sqlalchemy import create_engine, text


db_user = os.environ.get("MYSQL_USER", "proxmox_user")
db_password = os.environ.get("MYSQL_PASSWORD", "proxmox_password")
db_host = os.environ.get("MYSQL_HOST", "mysql")
db_name = os.environ.get("MYSQL_DATABASE", "proxmox_automation")
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def check_column_exists(conn, table_name, column_name):
    result = conn.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :db_name
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
    """), {
        "db_name": db_name,
        "table_name": table_name,
        "column_name": column_name,
    })
    return result.fetchone()[0] > 0


def _add_column_if_missing(conn, column_name, ddl):
    if check_column_exists(conn, "vm_records", column_name):
        print(f"vm_records.{column_name} already exists")
        return
    conn.execute(text(f"ALTER TABLE vm_records ADD COLUMN {column_name} {ddl}"))
    print(f"Added vm_records.{column_name}")


def run_migration(engine=None):
    engine = engine or create_engine(db_url)
    with engine.begin() as conn:
        _add_column_if_missing(
            conn,
            "deletion_last_attempt_at",
            "DATETIME NULL",
        )
        _add_column_if_missing(
            conn,
            "deletion_alert_sent",
            "BOOLEAN NOT NULL DEFAULT FALSE",
        )


if __name__ == "__main__":
    run_migration()
