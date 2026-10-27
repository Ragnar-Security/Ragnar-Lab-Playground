"""The portal shows each student the key stored in its own database.

Key rotation only reaches students if both key columns change together:
`student_whitelist.headscale_key` seeds future registrations and
`users.headscale_key` is what a registered student sees on the dashboard.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, exc, text

from app.models import db

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update_headscale_keys.py"

from scripts import update_headscale_keys  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'portal.db'}")
    db.Model.metadata.create_all(engine)
    # ISO text, not a datetime: Python 3.12 deprecates sqlite3's implicit
    # datetime adapter, and a raw text() INSERT bypasses SQLAlchemy's type.
    now = datetime.now().isoformat(sep=" ")
    with engine.begin() as conn:
        for login, key in [("alice", "old-alice"), ("bob", "old-bob"), ("carol", "old-carol")]:
            conn.execute(
                text("INSERT INTO student_whitelist (login_id, headscale_key, added_at) VALUES (:l, :k, :t)"),
                {"l": login, "k": key, "t": now},
            )
        # Only alice has registered; bob's key reaches him through the whitelist.
        conn.execute(
            text(
                "INSERT INTO users (login_id, password_hash, is_admin, headscale_key, created_at, disabled, session_version) "
                "VALUES ('alice', 'x', 0, 'old-alice', :t, 0, 0)"
            ),
            {"t": now},
        )
    yield engine
    engine.dispose()


def _keys(engine, table):
    with engine.connect() as conn:
        return dict(conn.execute(text(f"SELECT login_id, headscale_key FROM {table}")).all())


def test_registered_and_unregistered_students_both_get_the_new_key(engine):
    result = update_headscale_keys.update_keys(engine, {"alice": "new-alice", "bob": "new-bob"})

    assert _keys(engine, "users") == {"alice": "new-alice"}
    assert _keys(engine, "student_whitelist") == {
        "alice": "new-alice",
        "bob": "new-bob",
        "carol": "old-carol",
    }
    assert result["updated"] == ["alice", "bob"]
    assert result["unknown"] == []


def test_second_run_with_the_same_keys_changes_nothing(engine):
    keys = {"alice": "new-alice", "bob": "new-bob"}
    update_headscale_keys.update_keys(engine, keys)

    again = update_headscale_keys.update_keys(engine, keys)

    assert again["updated"] == []
    assert again["unchanged"] == ["alice", "bob"]
    assert _keys(engine, "users") == {"alice": "new-alice"}


def test_unknown_login_is_reported_and_creates_no_row(engine):
    result = update_headscale_keys.update_keys(engine, {"alice": "new-alice", "mallory": "new-m"})

    assert result["unknown"] == ["mallory"]
    assert "mallory" not in _keys(engine, "student_whitelist")
    assert "mallory" not in _keys(engine, "users")
    assert _keys(engine, "student_whitelist")["alice"] == "new-alice"


def test_failure_on_the_users_table_leaves_the_whitelist_unchanged(engine):
    # A half-applied rotation would show a registered student one key while
    # the whitelist (the source for any re-registration) holds another.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER block_users BEFORE UPDATE ON users "
                "BEGIN SELECT RAISE(ABORT, 'users is locked'); END"
            )
        )

    with pytest.raises(exc.DBAPIError, match="users is locked"):
        update_headscale_keys.update_keys(engine, {"alice": "new-alice", "bob": "new-bob"})

    assert _keys(engine, "student_whitelist") == {
        "alice": "old-alice",
        "bob": "old-bob",
        "carol": "old-carol",
    }


def _run_cli(tmp_path, stdin):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'portal.db'}")
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_reads_keys_from_stdin_and_reports_without_echoing_them(engine, tmp_path):
    result = _run_cli(tmp_path, "Alice,new-alice-secret\nmallory,new-m-secret\n")

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["updated"] == ["alice"]
    assert summary["unknown"] == ["mallory"]
    assert _keys(engine, "users")["alice"] == "new-alice-secret"
    assert "secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "stdin",
    [
        "alice,\n",  # empty key would blank the student's dashboard
        "alice,new-1\nalice,new-2\n",  # ambiguous: which key is real?
        "alice\n",  # malformed row
        "",  # nothing to do is an operator mistake, not success
    ],
)
def test_cli_rejects_bad_input_without_touching_the_database(engine, tmp_path, stdin):
    result = _run_cli(tmp_path, stdin)

    assert result.returncode != 0
    assert _keys(engine, "student_whitelist")["alice"] == "old-alice"
    assert _keys(engine, "users")["alice"] == "old-alice"
