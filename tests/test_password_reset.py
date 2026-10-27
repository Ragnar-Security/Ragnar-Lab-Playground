"""Self-service password reset: what an attacker guessing SIS IDs, and a
student who forgot a password, each observe."""

import pytest

from app import create_app
from app.auth import PasswordResetLimiter, _SlidingWindow
from app.models import StudentWhitelist, User, db

OLD_PASSWORD = 'original-pass-1'
NEW_PASSWORD = 'replacement-pass-2'
SIS = '100000001'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def _make_app(clock, monkeypatch, proxy_hops=None):
    if proxy_hops is None:
        monkeypatch.delenv('TRUSTED_PROXY_HOPS', raising=False)
    else:
        monkeypatch.setenv('TRUSTED_PROXY_HOPS', str(proxy_hops))
    application = create_app(testing=True)
    # Off by default for new installs; these tests exercise the enabled flow.
    application.config['SELF_SERVICE_PASSWORD_RESET'] = True
    application.extensions['password_reset_limiters'] = PasswordResetLimiter(clock=clock)
    return application


@pytest.fixture
def app(clock, monkeypatch):
    application = _make_app(clock, monkeypatch)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _student(login_id, sis=SIS, with_account=True):
    db.session.add(StudentWhitelist(login_id=login_id, sis_user_id=sis, full_name='Example Student'))
    if with_account:
        user = User(login_id=login_id, full_name='Example Student')
        user.set_password(OLD_PASSWORD)
        db.session.add(user)
    db.session.commit()


def _reset(client, login_id, sis=SIS, password=NEW_PASSWORD, **kwargs):
    return client.post('/auth/reset-password', json={
        'login_id': login_id, 'sis_user_id': sis, 'password': password,
    }, **kwargs)


def _password_is(login_id, password):
    db.session.expire_all()
    return User.query.filter_by(login_id=login_id).one().check_password(password)


def test_correct_sis_resets_password(app):
    _student('student-a')

    response = _reset(app.test_client(), 'student-a')

    assert response.get_json()['success'] is True
    assert _password_is('student-a', NEW_PASSWORD)


def test_wrong_sis_is_rejected(app):
    _student('student-a')

    response = _reset(app.test_client(), 'student-a', sis='100000002')

    assert response.get_json()['success'] is False
    assert _password_is('student-a', OLD_PASSWORD)


@pytest.mark.parametrize('stored_sis', [None, ''])
@pytest.mark.parametrize('submitted_sis', [SIS, '000000000', 'anything'])
def test_missing_stored_sis_rejects_every_submission(app, stored_sis, submitted_sis):
    _student('student-a', sis=stored_sis)

    response = _reset(app.test_client(), 'student-a', sis=submitted_sis)

    assert response.get_json()['success'] is False
    assert _password_is('student-a', OLD_PASSWORD)


def test_failure_modes_are_indistinguishable(app):
    """Distinct messages let anyone test which login IDs exist on the roster
    and which have no SIS ID on file."""
    _student('has-sis')
    _student('no-sis', sis=None)
    _student('no-account', with_account=False)
    client = app.test_client()

    responses = [
        _reset(client, 'not-on-roster'),
        _reset(client, 'has-sis', sis='100000002'),
        _reset(client, 'no-sis'),
        _reset(client, 'no-account'),
    ]

    assert {r.status_code for r in responses} == {responses[0].status_code}
    assert {r.get_data(as_text=True) for r in responses} == {responses[0].get_data(as_text=True)}
    assert responses[0].get_json()['success'] is False


def test_per_login_limit_trips_and_resets_after_window(app, clock):
    _student('student-a')
    client = app.test_client()

    for attempt in range(5):
        response = _reset(client, 'student-a', sis=f'90000000{attempt}')
        assert response.status_code == 200, attempt

    # Even the right SIS ID is refused once the window is exhausted, or the
    # limit would only slow an attacker who has not guessed yet.
    blocked = _reset(client, 'student-a')
    assert blocked.status_code == 429
    assert blocked.get_json()['success'] is False
    assert _password_is('student-a', OLD_PASSWORD)

    clock.now += 15 * 60 - 1
    assert _reset(client, 'student-a').status_code == 429

    clock.now += 2
    allowed = _reset(client, 'student-a')
    assert allowed.get_json()['success'] is True
    assert _password_is('student-a', NEW_PASSWORD)


def test_per_login_limit_holds_across_client_addresses(app):
    _student('student-a')
    client = app.test_client()

    for attempt in range(5):
        _reset(client, 'student-a', sis='100000002',
               environ_base={'REMOTE_ADDR': f'198.51.100.{attempt + 1}'})

    blocked = _reset(client, 'student-a', environ_base={'REMOTE_ADDR': '198.51.100.99'})
    assert blocked.status_code == 429


def test_per_ip_limit_spans_login_ids(app):
    _student('target')
    client = app.test_client()
    attacker = {'REMOTE_ADDR': '203.0.113.7'}

    for attempt in range(20):
        _reset(client, f'guess-{attempt}', environ_base=attacker)

    assert _reset(client, 'target', environ_base=attacker).status_code == 429
    elsewhere = _reset(client, 'target', environ_base={'REMOTE_ADDR': '203.0.113.8'})
    assert elsewhere.get_json()['success'] is True


def test_spoofed_forwarded_for_does_not_escape_ip_limit(app):
    """With no trusted proxy configured, X-Forwarded-For is client-controlled."""
    _student('target')
    client = app.test_client()
    attacker = {'REMOTE_ADDR': '203.0.113.7'}

    for attempt in range(20):
        _reset(client, f'guess-{attempt}', environ_base=attacker,
               headers={'X-Forwarded-For': f'192.0.2.{attempt}'})

    response = _reset(client, 'target', environ_base=attacker,
                      headers={'X-Forwarded-For': '192.0.2.200'})
    assert response.status_code == 429


def test_behind_trusted_proxy_limit_keys_on_forwarded_client(clock, monkeypatch):
    """In production every request arrives from the Caddy container; keyed on
    that address, one attacker would lock the whole class out of resets."""
    application = _make_app(clock, monkeypatch, proxy_hops=1)
    with application.app_context():
        db.create_all()
        _student('target')
        client = application.test_client()
        proxy = {'REMOTE_ADDR': '172.18.0.5'}

        for attempt in range(20):
            _reset(client, f'guess-{attempt}', environ_base=proxy,
                   headers={'X-Forwarded-For': '203.0.113.7'})

        assert _reset(client, 'target', environ_base=proxy,
                      headers={'X-Forwarded-For': '203.0.113.7'}).status_code == 429
        other_student = _reset(client, 'target', environ_base=proxy,
                               headers={'X-Forwarded-For': '203.0.113.8'})
        assert other_student.get_json()['success'] is True
        db.session.remove()
        db.drop_all()


def test_success_revokes_other_sessions(app):
    _student('student-a')
    victim_browser = app.test_client()
    login = victim_browser.post('/auth/login', json={'login_id': 'student-a', 'password': OLD_PASSWORD})
    assert login.get_json()['success'] is True

    assert _reset(app.test_client(), 'student-a').get_json()['success'] is True

    assert victim_browser.get('/auth/me').status_code == 401


def test_disabled_reset_endpoint_is_gone(app):
    _student('student-a')
    app.config['SELF_SERVICE_PASSWORD_RESET'] = False

    response = _reset(app.test_client(), 'student-a')

    assert response.status_code == 404
    assert _password_is('student-a', OLD_PASSWORD)


def test_disabled_reset_hides_link_and_points_to_admin(app):
    app.config['SELF_SERVICE_PASSWORD_RESET'] = False
    client = app.test_client()

    login_page = client.get('/').get_data(as_text=True)
    reset_page = client.get('/reset-password').get_data(as_text=True)

    assert 'href="/reset-password"' not in login_page
    assert 'id="resetForm"' not in reset_page
    assert 'administrator' in reset_page.lower()


def test_enabled_reset_offers_link_and_form(app):
    client = app.test_client()

    assert 'href="/reset-password"' in client.get('/').get_data(as_text=True)
    assert 'id="resetForm"' in client.get('/reset-password').get_data(as_text=True)


# Off unless the operator opts in: the flow trusts a login ID plus a roster
# SIS ID, which a new site may consider guessable or widely known.
@pytest.mark.parametrize('raw, expected', [
    (None, False), ('', False), ('true', True), ('false', False), ('bogus', False),
])
def test_reset_flag_reads_environment(monkeypatch, raw, expected):
    import importlib
    cfg = importlib.import_module('app.config')
    if raw is None:
        monkeypatch.delenv('SELF_SERVICE_PASSWORD_RESET', raising=False)
    else:
        monkeypatch.setenv('SELF_SERVICE_PASSWORD_RESET', raw)
    try:
        importlib.reload(cfg)
        assert cfg.config.SELF_SERVICE_PASSWORD_RESET is expected
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_limiter_memory_stays_bounded_and_newest_key_is_limited(clock):
    window = _SlidingWindow(limit=2, window=900, clock=clock, max_keys=3)

    for i in range(10):
        window.record(f'spray-{i}')
        assert len(window) <= 3
    window.record('newest')
    window.record('newest')

    assert len(window) <= 3
    assert window.blocked('newest')


def test_limiter_drops_expired_keys_before_evicting_live_ones(clock):
    window = _SlidingWindow(limit=2, window=900, clock=clock, max_keys=2)
    window.record('stale')
    clock.now += 901

    window.record('live')
    window.record('live')
    window.record('other')

    assert window.blocked('live')


def test_reset_limiter_never_tracks_more_than_its_cap(clock):
    """A spray of distinct login IDs from rotating addresses must not grow
    the in-process ledger past its cap, and must not blind it to a target."""
    limiter = PasswordResetLimiter(clock=clock)
    cap = _SlidingWindow.MAX_KEYS

    for i in range(cap + 50):
        limiter.record_failure(f'addr-{i}', f'spray-{i}')
    for _ in range(PasswordResetLimiter.PER_LOGIN):
        limiter.record_failure('addr-attacker', 'victim')

    assert len(limiter._by_login) <= cap
    assert len(limiter._by_address) <= cap
    assert limiter.blocked('addr-elsewhere', 'victim')
