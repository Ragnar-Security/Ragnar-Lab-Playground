"""Regression tests for the users.session_version schema migration."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def test_session_version_migration_is_idempotent_on_sqlite(tmp_path):
    migration_path = (
        Path(__file__).parents[1] / 'scripts' / 'migrate_add_session_version.py'
    )
    assert migration_path.exists(), 'session-version migration script is missing'

    spec = importlib.util.spec_from_file_location(
        'migrate_add_session_version', migration_path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text(
            'CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(100))'
        ))
        conn.execute(text(
            "INSERT INTO users (id, login_id) VALUES (1, 'student')"
        ))

    migration.run_migration(database_url)
    migration.run_migration(database_url)

    columns = {column['name']: column for column in inspect(engine).get_columns('users')}
    assert 'session_version' in columns
    assert columns['session_version']['nullable'] is False
    with engine.connect() as conn:
        value = conn.execute(text(
            "SELECT session_version FROM users WHERE login_id = 'student'"
        )).scalar_one_or_none()
    assert value == 0
