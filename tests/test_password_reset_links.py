"""Admin-issued password reset links: what an admin, the student holding the
link, and someone guessing or replaying links each observe."""

import hashlib
import importlib
import logging
import runpy
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from flask import Flask
from gunicorn.config import Config as GunicornConfig
from gunicorn.glogging import Logger as GunicornLogger
from sqlalchemy import create_engine, inspect, select, text

from app import create_app
from app import password_reset_links
from app.auth import PasswordResetLimiter, auth
from app.models import PasswordResetToken, User, db

OLD_PASSWORD = 'original-pass-1'
NEW_PASSWORD = 'replacement-pass-2'
ADMIN_PASSWORD = 'admin-pass-123'
REPO = Path(__file__).parents[1]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class WallClock:
    """Stands in for datetime.now() so expiry can be tested without sleeping."""

    def __init__(self):
        self.now = datetime(2026, 1, 15, 9, 0, 0)

    def __call__(self):
        return self.now


@pytest.fixture
def wall_clock(monkeypatch):
    clock = WallClock()
    monkeypatch.setattr(password_reset_links, '_now', clock)
    return clock


@pytest.fixture
def app(monkeypatch, wall_clock):
    monkeypatch.delenv('TRUSTED_PROXY_HOPS', raising=False)
    application = create_app(testing=True)
    application.extensions['password_reset_limiters'] = PasswordResetLimiter(clock=FakeClock())
    with application.app_context():
        db.create_all()
        _user('admin', is_admin=True, password=ADMIN_PASSWORD)
        _user('student-a')
        _user('student-b')
        yield application
        db.session.remove()
        db.drop_all()


def _user(login_id, is_admin=False, password=OLD_PASSWORD):
    user = User(login_id=login_id, full_name='Example Person', is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _user_id(login_id):
    return User.query.filter_by(login_id=login_id).one().id


def _logged_in(app, login_id, password=OLD_PASSWORD):
    client = app.test_client()
    response = client.post('/auth/login', json={'login_id': login_id, 'password': password})
    assert response.get_json()['success'] is True
    return client


def _admin(app):
    return _logged_in(app, 'admin', ADMIN_PASSWORD)


def _create_link(client, login_id='student-a'):
    return client.post(f'/admin/students/{_user_id(login_id)}/reset-link', json={})


def _token_from(response):
    body = response.get_json()
    assert body['success'] is True, body
    url = urlsplit(body['reset_url'])
    assert url.path == '/reset-password'
    assert url.fragment.startswith('token=')
    return url.fragment[len('token='):]


def _issue(app, login_id='student-a'):
    return _token_from(_create_link(_admin(app), login_id))


def _redeem(client, token, password=NEW_PASSWORD, **kwargs):
    return client.post('/auth/reset-password/link',
                       json={'token': token, 'password': password}, **kwargs)


def _password_is(login_id, password):
    db.session.expire_all()
    return User.query.filter_by(login_id=login_id).one().check_password(password)


def _token_rows():
    db.session.expire_all()
    return PasswordResetToken.query.all()


# ---- issuing ----

def test_anonymous_cannot_create_link(app):
    response = _create_link(app.test_client())

    assert response.status_code == 401
    assert _token_rows() == []


def test_student_cannot_create_link(app):
    student = _logged_in(app, 'student-b')

    response = _create_link(student, 'student-a')

    assert response.status_code == 403
    assert _token_rows() == []


def test_cross_site_form_post_cannot_create_link(app):
    """A cross-site HTML form can only send form encodings; requiring JSON
    keeps this endpoint safe even where SESSION_COOKIE_SAMESITE=None."""
    admin = _admin(app)

    response = admin.post(f'/admin/students/{_user_id("student-a")}/reset-link',
                          data={'x': '1'})

    assert response.status_code == 415
    assert _token_rows() == []


def test_unknown_user_gets_404(app):
    response = _admin(app).post('/admin/students/9999/reset-link', json={})

    assert response.status_code == 404
    assert _token_rows() == []


def test_link_response_is_not_cached_and_reports_expiry(app, wall_clock):
    response = _create_link(_admin(app))

    assert 'no-store' in response.headers.get('Cache-Control', '')
    expires = datetime.fromisoformat(response.get_json()['expires_at'])
    assert expires.replace(tzinfo=None) == wall_clock.now + timedelta(minutes=60)


def test_database_never_holds_the_raw_token(app):
    token = _issue(app)

    dumped = []
    for table in db.metadata.sorted_tables:
        for row in db.session.execute(select(table)).all():
            dumped.extend(str(value) for value in row)
    everything = '\n'.join(dumped)

    assert token not in everything
    [row] = _token_rows()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.user_id == _user_id('student-a')
    assert row.created_by_user_id == _user_id('admin')
    assert row.used_at is None


def test_token_carries_32_random_bytes(app):
    tokens = {_issue(app, 'student-a'), _issue(app, 'student-b')}

    assert len(tokens) == 2
    for token in tokens:
        # urlsafe base64 of 32 bytes is 43 characters without padding.
        assert len(token) >= 43


# ---- redeeming ----

def test_link_resets_password_and_revokes_other_sessions(app):
    victim_browser = _logged_in(app, 'student-a')
    token = _issue(app)

    response = _redeem(app.test_client(), token)

    assert response.get_json()['success'] is True
    assert _password_is('student-a', NEW_PASSWORD)
    assert victim_browser.get('/auth/me').status_code == 401
    _logged_in(app, 'student-a', NEW_PASSWORD)


def test_link_works_only_once(app):
    token = _issue(app)
    assert _redeem(app.test_client(), token).get_json()['success'] is True

    replay = _redeem(app.test_client(), token, password='attacker-pass-3')

    assert replay.get_json()['success'] is False
    assert _password_is('student-a', NEW_PASSWORD)


@pytest.mark.parametrize('minutes_later, works', [(4, True), (6, False)])
def test_link_expires_after_configured_ttl(app, wall_clock, minutes_later, works):
    app.config['PASSWORD_RESET_TOKEN_TTL_MINUTES'] = 5
    token = _issue(app)

    wall_clock.now += timedelta(minutes=minutes_later)
    response = _redeem(app.test_client(), token)

    assert response.get_json()['success'] is works
    assert _password_is('student-a', NEW_PASSWORD if works else OLD_PASSWORD)


def test_new_link_kills_the_previous_one(app):
    first = _issue(app)
    second = _issue(app)

    assert _redeem(app.test_client(), first).get_json()['success'] is False
    assert _password_is('student-a', OLD_PASSWORD)
    assert _redeem(app.test_client(), second).get_json()['success'] is True


def test_new_link_for_one_student_leaves_anothers_alive(app):
    token_a = _issue(app, 'student-a')
    _issue(app, 'student-b')

    assert _redeem(app.test_client(), token_a).get_json()['success'] is True


def test_weak_password_is_refused_and_link_stays_usable(app):
    token = _issue(app)

    weak = _redeem(app.test_client(), token, password='short')

    assert weak.get_json()['success'] is False
    assert 'at least 8 characters' in weak.get_json()['error']
    assert _redeem(app.test_client(), token).get_json()['success'] is True


def test_invalid_expired_and_used_links_are_indistinguishable(app, wall_clock):
    used = _issue(app, 'student-a')
    assert _redeem(app.test_client(), used).get_json()['success'] is True
    expired = _issue(app, 'student-b')
    wall_clock.now += timedelta(minutes=61)

    responses = [
        _redeem(app.test_client(), 'not-a-real-token'),
        _redeem(app.test_client(), expired),
        _redeem(app.test_client(), used),
        _redeem(app.test_client(), ''),
    ]

    assert len({(r.status_code, r.get_data(as_text=True)) for r in responses}) == 1
    assert responses[0].get_json()['success'] is False


def test_wrong_tokens_count_against_the_client_limit(app):
    token = _issue(app)
    attacker = {'REMOTE_ADDR': '203.0.113.7'}

    for attempt in range(PasswordResetLimiter.PER_ADDRESS):
        _redeem(app.test_client(), f'guess-{attempt}', environ_base=attacker)

    blocked = _redeem(app.test_client(), token, environ_base=attacker)
    assert blocked.status_code == 429
    assert _password_is('student-a', OLD_PASSWORD)

    elsewhere = _redeem(app.test_client(), token, environ_base={'REMOTE_ADDR': '203.0.113.8'})
    assert elsewhere.get_json()['success'] is True


def test_concurrent_double_use_succeeds_exactly_once(tmp_path, wall_clock):
    """Two tabs, or an attacker racing the student, submit one link at the
    same moment. A file database gives each thread its own connection, as
    production's pool does; the in-memory test database would share one."""
    application = Flask(__name__)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f'sqlite:///{tmp_path / "race.db"}',
        SECRET_KEY='race-test',
        PASSWORD_RESET_TOKEN_TTL_MINUTES=60,
    )
    db.init_app(application)
    application.register_blueprint(auth, url_prefix='/auth')

    with application.app_context():
        db.create_all()
        student = _user('student-a')
        token, _expires = password_reset_links.issue(student, created_by=None)

    barrier = threading.Barrier(2)
    results = []

    def submit(password):
        client = application.test_client()
        barrier.wait()
        results.append(_redeem(client, token, password=password).get_json()['success'])

    threads = [threading.Thread(target=submit, args=(f'racer-pass-{n}',)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [False, True]
    with application.app_context():
        db.drop_all()


# ---- the page ----

def test_reset_page_sends_no_referrer(app):
    response = app.test_client().get('/reset-password')

    assert response.headers.get('Referrer-Policy') == 'no-referrer'
    # Caddy overwrites Referrer-Policy on every response; the meta element
    # keeps the page's own policy in force behind it.
    assert '<meta name="referrer" content="no-referrer">' in response.get_data(as_text=True)


def test_page_offers_link_form_and_tells_students_to_ask_for_a_link(app):
    app.config['SELF_SERVICE_PASSWORD_RESET'] = False

    page = app.test_client().get('/reset-password').get_data(as_text=True)

    assert 'id="tokenResetForm"' in page
    assert 'id="resetForm"' not in page
    assert 'ask an administrator for a password reset link' in page.lower()


# ---- logging ----

def test_token_never_reaches_application_logs(app, caplog):
    caplog.set_level(logging.DEBUG)
    token = _issue(app)
    _redeem(app.test_client(), token, password='short')
    _redeem(app.test_client(), token)
    _redeem(app.test_client(), token)

    assert caplog.records, 'issuing and using a link should be audited'
    assert token not in caplog.text
    assert token not in repr([record.args for record in caplog.records])


def _gunicorn_access_line(response):
    """The line production's gunicorn writes for this request, formatted with
    the access_log_format from gunicorn.conf.py."""
    settings = runpy.run_path(str(REPO / 'gunicorn.conf.py'))
    cfg = GunicornConfig()
    cfg.set('access_log_format', settings['access_log_format'])
    environ = dict(response.request.environ)
    query = environ.get('QUERY_STRING')
    environ['RAW_URI'] = environ['PATH_INFO'] + (f'?{query}' if query else '')
    atoms = GunicornLogger(cfg).atoms(
        SimpleNamespace(status=response.status, sent=len(response.data),
                        headers=list(response.headers.items())),
        SimpleNamespace(headers=list(response.request.headers.items())),
        environ, timedelta(milliseconds=5))
    return settings['access_log_format'] % atoms


def test_token_never_reaches_gunicorn_access_log(app):
    """A browser never sends a URL fragment, so the page request and the
    form POST that follows carry the token only in the POST body."""
    reset_url = _create_link(_admin(app)).get_json()['reset_url']
    token = urlsplit(reset_url).fragment[len('token='):]
    browser = app.test_client()
    on_the_wire = urlsplit(reset_url)._replace(fragment='').geturl()

    page = browser.get(on_the_wire)
    post = _redeem(browser, token, headers={'Referer': f'http://localhost{on_the_wire}'})

    assert post.get_json()['success'] is True
    for response in (page, post):
        line = _gunicorn_access_line(response)
        assert '/reset-password' in line
        assert token not in line


# ---- configuration and schema ----

@pytest.mark.parametrize('raw, expected', [
    (None, 60), ('', 60), ('15', 15), ('0', 60), ('-5', 60), ('bogus', 60),
])
def test_ttl_reads_environment(monkeypatch, raw, expected):
    # `app.config` the attribute is the Flask config object; import the module.
    cfg = importlib.import_module('app.config')
    if raw is None:
        monkeypatch.delenv('PASSWORD_RESET_TOKEN_TTL_MINUTES', raising=False)
    else:
        monkeypatch.setenv('PASSWORD_RESET_TOKEN_TTL_MINUTES', raw)
    try:
        importlib.reload(cfg)
        assert cfg.config.PASSWORD_RESET_TOKEN_TTL_MINUTES == expected
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def _load_migration():
    path = REPO / 'scripts' / 'migrate_add_password_reset_tokens.py'
    assert path.exists(), 'password reset token migration script is missing'
    spec = importlib.util.spec_from_file_location('migrate_add_password_reset_tokens', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_creates_table_matching_model_and_is_idempotent(tmp_path):
    migration = _load_migration()
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(100))'))
        conn.execute(text("INSERT INTO users (id, login_id) VALUES (1, 'student')"))

    migration.run_migration(database_url)
    migration.run_migration(database_url)

    inspector = inspect(engine)
    migrated = {c['name']: c for c in inspector.get_columns('password_reset_tokens')}
    model = PasswordResetToken.__table__.columns
    assert set(migrated) == {c.name for c in model}
    for column in model:
        assert migrated[column.name]['nullable'] == column.nullable, column.name
    unique = {tuple(i['column_names']) for i in inspector.get_indexes('password_reset_tokens')
              if i['unique']}
    unique |= {tuple(u['column_names'])
               for u in inspector.get_unique_constraints('password_reset_tokens')}
    assert ('token_hash',) in unique
    with engine.connect() as conn:
        assert conn.execute(text('SELECT login_id FROM users')).scalar_one() == 'student'


def test_purging_students_takes_their_links_with_them(tmp_path):
    """scripts/purge_roster.sql runs DELETE FROM users WHERE is_admin = 0 at
    term changeover. A link must not stop that DELETE, and an admin's issued
    links must not stop the admin from being removed later."""
    migration = _load_migration()
    database_url = f"sqlite:///{tmp_path / 'purge.db'}"
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN)'))
        conn.execute(text('INSERT INTO users (id, is_admin) VALUES (1, 1), (2, 0)'))
    migration.run_migration(database_url)

    with engine.begin() as conn:
        conn.execute(text('PRAGMA foreign_keys = ON'))
        conn.execute(text(
            "INSERT INTO password_reset_tokens (user_id, created_by_user_id, token_hash,"
            " created_at, expires_at) VALUES (2, 1, 'h1', '2026-01-01', '2026-01-02'),"
            " (1, 2, 'h2', '2026-01-01', '2026-01-02')"))
        conn.execute(text('DELETE FROM users WHERE is_admin = 0'))
        rows = conn.execute(text(
            'SELECT user_id, created_by_user_id FROM password_reset_tokens')).all()

    assert rows == [(1, None)]
