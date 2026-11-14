#!/usr/bin/env python3
"""Add VM backend identity columns (backend, provider_id, deployment_id,
network_slot) and the deployment_id index to vm_records. Idempotent.

Existing rows become backend='proxmox' through the column default; node and
vm_type keep their Proxmox meaning.
"""

import os

from sqlalchemy import create_engine, text


db_user = os.environ.get("MYSQL_USER", "proxmox_user")
db_password = os.environ.get("MYSQL_PASSWORD", "proxmox_password")
db_host = os.environ.get("MYSQL_HOST", "mysql")
db_name = os.environ.get("MYSQL_DATABASE", "proxmox_automation")
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

TABLE = "vm_records"
COLUMNS = (
    ("backend", "VARCHAR(32) NOT NULL DEFAULT 'proxmox'"),
    ("provider_id", "VARCHAR(255) NULL"),
    ("deployment_id", "VARCHAR(64) NULL"),
    ("network_slot", "INT NULL"),
)
DEPLOYMENT_INDEX = "ix_vm_records_deployment_id"


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


def check_index_exists(conn, table_name, index_name):
    result = conn.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = :db_name
          AND TABLE_NAME = :table_name
          AND INDEX_NAME = :index_name
    """), {
        "db_name": db_name,
        "table_name": table_name,
        "index_name": index_name,
    })
    return result.fetchone()[0] > 0


def _add_column_if_missing(conn, column_name, ddl):
    if check_column_exists(conn, TABLE, column_name):
        print(f"{TABLE}.{column_name} already exists")
        return
    conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column_name} {ddl}"))
    print(f"Added {TABLE}.{column_name}")


def _add_index_if_missing(conn, index_name, column_name):
    if check_index_exists(conn, TABLE, index_name):
        print(f"{TABLE}.{index_name} already exists")
        return
    conn.execute(text(f"CREATE INDEX {index_name} ON {TABLE} ({column_name})"))
    print(f"Added index {TABLE}.{index_name}")


def run_migration(engine=None):
    engine = engine or create_engine(db_url)
    with engine.begin() as conn:
        for column_name, ddl in COLUMNS:
            _add_column_if_missing(conn, column_name, ddl)
        _add_index_if_missing(conn, DEPLOYMENT_INDEX, "deployment_id")


if __name__ == "__main__":
    run_migration()
