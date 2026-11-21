"""The one-accept-per-team flag index, on a live MySQL 8 server.

SQLite honours the model's old partial index, so the defect these cover
(a plain UNIQUE(flag_id, team_id) on MySQL) cannot appear in the default
suite. Skipped unless KOTH_TEST_MYSQL_URL names a disposable MySQL 8
server account allowed to create and drop databases, e.g.

    KOTH_TEST_MYSQL_URL=mysql+pymysql://root:<pw>@127.0.0.1:3407/ \
        python -m pytest tests/test_koth_flag_index_mysql.py

Each test works in its own throwaway database.
"""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

SERVER_URL = os.getenv('KOTH_TEST_MYSQL_URL')
pytestmark = pytest.mark.skipif(
    not SERVER_URL, reason='KOTH_TEST_MYSQL_URL not set (needs a disposable MySQL 8 server)')

# koth_flag_submissions exactly as db.create_all() built it on MySQL before
# the model declared accepted_singleton (SHOW CREATE TABLE on a portal
# started from ba3dd12), minus the foreign keys this test does not need.
PRE_FIX_TABLE = """
CREATE TABLE koth_flag_submissions (
  id int NOT NULL AUTO_INCREMENT,
  game_id int NOT NULL,
  flag_id int NOT NULL,
  team_id int NOT NULL,
  submitted_by_user_id int DEFAULT NULL,
  accepted tinyint(1) NOT NULL,
  submitted_at datetime NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_koth_flag_sub_accepted (flag_id, team_id),
  KEY ix_koth_flag_submissions_game_id (game_id),
  KEY ix_koth_flag_submissions_team_id (team_id),
  KEY ix_koth_flag_submissions_flag_id (flag_id)
) ENGINE=InnoDB
"""


@pytest.fixture
def mysql_url():
    name = 'koth_test_' + uuid.uuid4().hex[:12]
    server = create_engine(SERVER_URL)
    with server.begin() as conn:
        conn.execute(text(f'CREATE DATABASE {name}'))
    try:
        yield make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with server.begin() as conn:
            conn.execute(text(f'DROP DATABASE {name}'))
        server.dispose()


def _migration():
    path = Path(__file__).parents[1] / 'scripts' / 'migrate_koth_flag_submission_index.py'
    spec = importlib.util.spec_from_file_location('migrate_koth_flag_submission_index', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _submit(conn, flag_id, accepted, team_id=1):
    conn.execute(text(
        "INSERT INTO koth_flag_submissions (game_id, flag_id, team_id, accepted, submitted_at) "
        "VALUES (1, :f, :t, :a, NOW())"), {'f': flag_id, 't': team_id, 'a': accepted})


def test_a_team_can_guess_wrong_twice_then_score_on_a_create_all_schema(mysql_url):
    from werkzeug.security import generate_password_hash
    from app import create_app
    from app.models import (db, User, StudentGroup, StudentGroupMember,
                            KothGame, KothTeam, KothFlag)
    app = create_app(testing=True, test_config={'SQLALCHEMY_DATABASE_URI': mysql_url})
    with app.app_context():
        db.create_all()
        try:
            student = User(login_id='stud', password_hash=generate_password_hash('p'))
            group = StudentGroup(name='team-a', display_name='Team A')
            db.session.add_all([student, group])
            db.session.commit()
            db.session.add(StudentGroupMember(group_id=group.id, user_id=student.id))
            game = KothGame(name='g', deployment_group_name='x', entry_vlan_tag=550,
                            internal_vlan_tag=551, status='active', is_active=True)
            db.session.add(game)
            db.session.commit()
            db.session.add_all([
                KothTeam(game_id=game.id, student_group_id=group.id, color='red',
                         submission_token='tok', display_name='Team A'),
                KothFlag(game_id=game.id, name='f1', value='right', points=10),
            ])
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as s:
                s['user_id'] = student.id
                s['session_version'] = student.session_version

            def submit(value):
                r = client.post('/koth/api/flags/submit', json={'value': value})
                assert r.status_code == 200, r.data
                return r.get_json()

            assert submit('wrong-1') == {'accepted': False}
            assert submit('wrong-2') == {'accepted': False}
            assert submit('right')['accepted'] is True
            assert submit('right') == {'accepted': False, 'reason': 'duplicate'}
        finally:
            db.session.remove()
            db.engine.dispose()


def test_repair_migration_fixes_a_pre_fix_table_and_keeps_its_rows(mysql_url, capsys):
    engine = create_engine(mysql_url)
    with engine.begin() as conn:
        conn.execute(text(PRE_FIX_TABLE))
        _submit(conn, flag_id=1, accepted=0)   # a wrong guess, recorded on flag 1
        _submit(conn, flag_id=2, accepted=1)   # an accepted flag
    # The defect: the team's next wrong guess collides with the first.
    with pytest.raises(IntegrityError) as err, engine.begin() as conn:
        _submit(conn, flag_id=1, accepted=0)
    assert err.value.orig.args[0] == 1062

    migration = _migration()
    assert migration.run_migration(mysql_url) is True
    assert 'repaired in' in capsys.readouterr().out
    assert migration.run_migration(mysql_url) is True       # idempotent
    assert 'already correct' in capsys.readouterr().out

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT flag_id, accepted, accepted_singleton FROM koth_flag_submissions "
            "ORDER BY id")).fetchall()
        assert [tuple(r) for r in rows] == [(1, 0, None), (2, 1, 1)]
        _submit(conn, flag_id=1, accepted=0)
        _submit(conn, flag_id=1, accepted=1)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _submit(conn, flag_id=2, accepted=1)                  # still one accept per team
    engine.dispose()


def test_repair_migration_refuses_while_a_game_is_active(mysql_url, capsys):
    engine = create_engine(mysql_url)
    with engine.begin() as conn:
        conn.execute(text(PRE_FIX_TABLE))
        conn.execute(text("CREATE TABLE koth_games (id int PRIMARY KEY, is_active tinyint(1))"))
        conn.execute(text("INSERT INTO koth_games VALUES (1, 1)"))
    migration = _migration()

    assert migration.run_migration(mysql_url, check=True) is False
    with pytest.raises(migration.GameActive):
        migration.run_migration(mysql_url)
    with engine.connect() as conn:
        assert migration.repair_clauses(conn), 'nothing may change before the repair runs'

    assert migration.run_migration(mysql_url, during_game=True) is True
    with engine.connect() as conn:
        assert migration.repair_clauses(conn) == []
    engine.dispose()
