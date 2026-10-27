"""Startup and transport security settings, observed where an operator or a
browser would notice them: startup refusing to run, the cookie attributes a
login response carries, and the TLS verification Proxmox traffic uses."""

import importlib
import os
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from unittest.mock import patch

import pytest

import app as app_package
from app import create_app
from app.models import User, db

# `app.config` the attribute is the selected Config class; the module lives
# only in sys.modules under that name.
cfg = importlib.import_module('app.config')

STRONG_KEY = 'a3f1' * 16  # 64 hex chars, the shape secrets.token_hex(32) emits


@pytest.fixture
def startable(monkeypatch):
    """Satisfy every unrelated startup check so a test sees only its own."""
    monkeypatch.setattr(cfg, 'PROXMOX_HOST', 'proxmox.example.test')
    monkeypatch.setattr(cfg, 'PROXMOX_TOKEN_ID', 'svc@pve!portal')
    monkeypatch.setattr(cfg, 'PROXMOX_TOKEN_SECRET', 'not-a-real-token')
    monkeypatch.setattr(cfg, 'DATABASE_URL', 'sqlite:///:memory:')
    monkeypatch.delenv('MYSQL_PASSWORD', raising=False)
    monkeypatch.setenv('SECRET_KEY', STRONG_KEY)


# ---------------------------------------------------------------- SECRET_KEY

@pytest.mark.parametrize('secret_key', [
    None,
    '',
    '   ',
    # The value .env.example shipped before it became an empty placeholder.
    'change-this-to-a-random-secret-key-in-production',
    'test-secret-key-for-integration',
    'dev',
    # Random-looking but too short to resist offline guessing of the HMAC key.
    'a3f1' * 7 + 'abc',
])
def test_production_refuses_to_start_with_weak_secret_key(startable, monkeypatch, secret_key):
    monkeypatch.setenv('FLASK_ENV', 'production')
    if secret_key is None:
        monkeypatch.delenv('SECRET_KEY', raising=False)
    else:
        monkeypatch.setenv('SECRET_KEY', secret_key)

    with pytest.raises(ValueError, match='SECRET_KEY'):
        cfg.validate_config()


@pytest.mark.parametrize('flask_env', ['', 'staging', 'Production'])
def test_unrecognised_environment_is_treated_as_production(startable, monkeypatch, flask_env):
    monkeypatch.setenv('FLASK_ENV', flask_env)
    monkeypatch.setenv('SECRET_KEY', 'change-this-to-a-random-secret-key-in-production')

    with pytest.raises(ValueError, match='SECRET_KEY'):
        cfg.validate_config()


@pytest.mark.parametrize('flask_env, expected', [
    ('Development', 'DevelopmentConfig'),
    (' testing ', 'TestingConfig'),
    ('development', 'DevelopmentConfig'),
    ('Production', 'ProductionConfig'),
    ('', 'ProductionConfig'),
    ('staging', 'ProductionConfig'),
])
def test_config_class_agrees_with_production_check(monkeypatch, flask_env, expected):
    """FLASK_ENV=Development once selected ProductionConfig while the security
    checks treated the same process as development."""
    monkeypatch.setenv('FLASK_ENV', flask_env)
    try:
        importlib.reload(cfg)
        assert cfg.config.__name__ == expected
        assert cfg.is_production() is (expected == 'ProductionConfig')
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_production_starts_with_strong_secret_key(startable, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    cfg.validate_config()


def _start_real_app(monkeypatch):
    """create_app() along the non-test path, but against throwaway SQLite."""
    monkeypatch.setattr(app_package.config, 'SQLALCHEMY_DATABASE_URI', 'sqlite://')
    monkeypatch.setattr(app_package.config, 'SQLALCHEMY_ENGINE_OPTIONS', {}, raising=False)
    application = create_app()
    application.config['TESTING'] = True
    return application


def _login(application, login_id='cookie-user', password='not-a-real-password'):
    with application.app_context():
        db.create_all()
        user = User(login_id=login_id, is_admin=False)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
    client = application.test_client()
    response = client.post('/auth/login', json={'login_id': login_id, 'password': password})
    assert response.get_json()['success'] is True
    return client, response


def test_development_without_secret_key_still_signs_sessions(startable, monkeypatch):
    """Compose passes `SECRET_KEY=` (empty) when .env leaves it blank; a
    developer stack must still get a working, ephemeral signing key."""
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SECRET_KEY', '')

    application = _start_real_app(monkeypatch)
    client, _ = _login(application)

    assert client.get('/auth/me').status_code == 200


# --------------------------------------------------------- database password

@pytest.mark.parametrize('database_url', [
    'mysql+pymysql://vmuser:vmpassword@db:3306/vmdatabase',
    'mysql+pymysql://vmuser:strong-user-password-change-this@db:3306/vmdatabase',
    # Compose renders an unset ${MYSQL_PASSWORD} as an empty password.
    'mysql+pymysql://vmuser:@db:3306/vmdatabase',
])
def test_production_refuses_known_database_passwords(startable, monkeypatch, database_url):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'DATABASE_URL', database_url)

    with pytest.raises(ValueError, match='database password'):
        cfg.validate_config()


def test_production_refuses_placeholder_mysql_password_env(startable, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'DATABASE_URL', 'mysql+pymysql://vmuser:s3cure-Db-pass-91x@db:3306/vmdatabase')
    monkeypatch.setenv('MYSQL_PASSWORD', 'strong-user-password-change-this')

    with pytest.raises(ValueError, match='MYSQL_PASSWORD'):
        cfg.validate_config()


def test_production_accepts_operator_database_password(startable, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'DATABASE_URL', 'mysql+pymysql://vmuser:s3cure-Db-pass-91x@db:3306/vmdatabase')
    monkeypatch.setenv('MYSQL_PASSWORD', 's3cure-Db-pass-91x')

    cfg.validate_config()


def test_development_compose_defaults_still_start(startable, monkeypatch):
    """docker-compose.yml hardcodes vmuser/vmpassword under FLASK_ENV=development."""
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setattr(cfg, 'DATABASE_URL', 'mysql+pymysql://vmuser:vmpassword@db:3306/vmdatabase')
    monkeypatch.setenv('MYSQL_PASSWORD', 'vmpassword')
    monkeypatch.setenv('SECRET_KEY', '')

    cfg.validate_config()


# ----------------------------------------------------------- session cookie

def _session_cookie_header(response):
    headers = [h for h in response.headers.getlist('Set-Cookie') if h.startswith('session=')]
    assert len(headers) == 1, headers
    return headers[0]


@pytest.fixture
def clean_session_env(monkeypatch):
    for name in ('SESSION_COOKIE_SECURE', 'SESSION_COOKIE_HTTPONLY',
                 'SESSION_COOKIE_SAMESITE', 'PERMANENT_SESSION_LIFETIME'):
        monkeypatch.delenv(name, raising=False)


def test_production_login_cookie_is_secure_httponly_lax(clean_session_env, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'production')
    application = create_app(testing=True)

    _, response = _login(application)
    header = _session_cookie_header(response)

    assert '; Secure' in header
    assert '; HttpOnly' in header
    assert '; SameSite=Lax' in header


def test_development_login_cookie_works_over_plain_http(clean_session_env, monkeypatch):
    """The integration stack (docker-compose.test.yml) serves plain HTTP on
    5099 with FLASK_ENV=development; a Secure cookie would never come back."""
    monkeypatch.setenv('FLASK_ENV', 'development')
    application = create_app(testing=True)

    _, response = _login(application)
    header = _session_cookie_header(response)

    assert '; Secure' not in header
    assert '; HttpOnly' in header
    assert '; SameSite=Lax' in header


def test_session_lifetime_comes_from_environment(clean_session_env, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('PERMANENT_SESSION_LIFETIME', '600')
    application = create_app(testing=True)

    _, response = _login(application)
    expires = _session_cookie_header(response).split('Expires=')[1].split(';')[0]
    remaining = (parsedate_to_datetime(expires) - datetime.now(timezone.utc)).total_seconds()

    assert 540 < remaining <= 601


def test_session_lifetime_defaults_to_two_hours(clean_session_env, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    application = create_app(testing=True)

    _, response = _login(application)
    expires = _session_cookie_header(response).split('Expires=')[1].split(';')[0]
    remaining = (parsedate_to_datetime(expires) - datetime.now(timezone.utc)).total_seconds()

    assert 7140 < remaining <= 7201


def test_invalid_samesite_fails_at_startup_not_at_login(clean_session_env, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SESSION_COOKIE_SAMESITE', 'Laxx')

    with pytest.raises(ValueError, match='SESSION_COOKIE_SAMESITE'):
        create_app(testing=True)


# --------------------------------------------------------- Proxmox TLS

class _ReloadedConfig:
    """Re-read app.config under a given environment, then restore it."""

    def __init__(self, monkeypatch, **env):
        self.monkeypatch = monkeypatch
        self.env = env

    def __enter__(self):
        for name in ('PROXMOX_VERIFY_SSL', 'PROXMOX_CA_BUNDLE'):
            self.monkeypatch.delenv(name, raising=False)
        for name, value in self.env.items():
            self.monkeypatch.setenv(name, value)
        importlib.reload(cfg)
        return cfg

    def __exit__(self, *exc):
        self.monkeypatch.undo()
        importlib.reload(cfg)


def _constructor_verify(monkeypatch, verify_ssl, ca_bundle, token=True):
    from app import proxmox_service
    monkeypatch.setattr(cfg, 'PROXMOX_VERIFY_SSL', verify_ssl)
    monkeypatch.setattr(cfg, 'PROXMOX_CA_BUNDLE', ca_bundle)
    monkeypatch.setattr(proxmox_service, 'PROXMOX_TOKEN_ID', 'portal' if token else '')
    monkeypatch.setattr(proxmox_service, 'PROXMOX_TOKEN_SECRET', 'not-a-real-token' if token else '')
    with patch('app.proxmox_service.ProxmoxAPI') as proxmox_api:
        proxmox_service.get_proxmox_connection()
    return proxmox_api.call_args.kwargs['verify_ssl']


@pytest.mark.parametrize('token', [True, False])
def test_proxmox_verification_on_by_default(monkeypatch, token):
    with _ReloadedConfig(monkeypatch) as reloaded:
        verify, bundle = reloaded.PROXMOX_VERIFY_SSL, reloaded.PROXMOX_CA_BUNDLE
    assert _constructor_verify(monkeypatch, verify, bundle, token=token) is True


@pytest.mark.parametrize('token', [True, False])
def test_proxmox_verification_can_be_disabled(monkeypatch, token):
    with _ReloadedConfig(monkeypatch, PROXMOX_VERIFY_SSL='false') as reloaded:
        verify, bundle = reloaded.PROXMOX_VERIFY_SSL, reloaded.PROXMOX_CA_BUNDLE
    assert _constructor_verify(monkeypatch, verify, bundle, token=token) is False


def test_proxmox_ca_bundle_is_passed_as_verify_value(monkeypatch, tmp_path):
    bundle_path = tmp_path / 'proxmox-ca.pem'
    bundle_path.write_text('placeholder')
    with _ReloadedConfig(monkeypatch, PROXMOX_CA_BUNDLE=str(bundle_path)) as reloaded:
        verify, bundle = reloaded.PROXMOX_VERIFY_SSL, reloaded.PROXMOX_CA_BUNDLE
    assert _constructor_verify(monkeypatch, verify, bundle) == str(bundle_path)


def test_disabled_verification_warns_once_at_startup(startable, monkeypatch, caplog):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'PROXMOX_VERIFY_SSL', False)

    with caplog.at_level('WARNING', logger='app.config'):
        cfg.validate_config()

    warnings = [r for r in caplog.records if 'PROXMOX_VERIFY_SSL' in r.getMessage()]
    assert len(warnings) == 1


def test_missing_ca_bundle_fails_at_startup(startable, monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'PROXMOX_CA_BUNDLE', str(tmp_path / 'absent.pem'))

    with pytest.raises(ValueError, match='PROXMOX_CA_BUNDLE'):
        cfg.validate_config()


@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0,
                    reason='root can read a mode-000 file')
def test_unreadable_ca_bundle_fails_at_startup(startable, monkeypatch, tmp_path):
    bundle = tmp_path / 'proxmox-ca.pem'
    bundle.write_text('placeholder')
    bundle.chmod(0)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(cfg, 'PROXMOX_CA_BUNDLE', str(bundle))
    try:
        with pytest.raises(ValueError, match='PROXMOX_CA_BUNDLE'):
            cfg.validate_config()
    finally:
        bundle.chmod(0o600)


class _FakeResponse:
    status_code = 200
    content = b'{"data": {"version": "8.2"}}'


def test_token_auth_traffic_actually_verifies_tls(monkeypatch):
    """Below the ProxmoxAPI boundary on purpose: proxmoxer 2.0.0 accepted
    verify_ssl=True for token auth and still sent every request with
    verify=False. Only the HTTP layer shows what the wire really does."""
    from app import proxmox_service
    monkeypatch.setattr(cfg, 'PROXMOX_VERIFY_SSL', True)
    monkeypatch.setattr(cfg, 'PROXMOX_CA_BUNDLE', '')
    monkeypatch.setattr(proxmox_service, 'PROXMOX_HOST', 'proxmox.example.test')
    monkeypatch.setattr(proxmox_service, 'PROXMOX_TOKEN_ID', 'portal')
    monkeypatch.setattr(proxmox_service, 'PROXMOX_TOKEN_SECRET', 'not-a-real-token')
    seen = []

    def fake_request(self, method, url, params=None, data=None, headers=None,
                     cookies=None, files=None, auth=None, timeout=None,
                     allow_redirects=True, proxies=None, hooks=None,
                     stream=None, verify=None, cert=None, json=None):
        seen.append(verify)
        return _FakeResponse()

    with patch('requests.Session.request', fake_request):
        proxmox_service.get_proxmox_connection().version.get()

    assert seen == [True]
