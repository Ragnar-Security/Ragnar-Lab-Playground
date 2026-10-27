"""Consumer-visible tests for operator-supplied site configuration."""

import pytest

from app import create_app
from app.models import User, db
from scripts import init_db, preflight_env


@pytest.fixture
def app():
    application = create_app(testing=True)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _authenticated_client(app):
    with app.app_context():
        user = User(login_id="site-config-user", is_admin=False, session_version=1)
        user.set_password("not-a-real-password")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 1
    return client


def test_operator_branding_preserves_current_login_and_index_text(app):
    app.config.update(
        SITE_NAME="Example Lab",
        SITE_BRAND_TAG="Acme",
        SITE_ORG="Example University",
        VPN_LOGIN_SERVER="https://vpn.example.edu",
    )

    login_html = app.test_client().get("/").get_data(as_text=True)
    index_html = _authenticated_client(app).get("/").get_data(as_text=True)

    assert "<title>Login - Acme Example Lab</title>" in login_html
    assert '<span class="course-label">Acme</span>' in login_html
    assert "<h3>Example Lab</h3>" in login_html
    assert "<title>Acme Example Lab</title>" in index_html
    assert "sudo tailscale up --login-server=https://vpn.example.edu --auth-key=" in index_html
    assert "&copy; 2026 Acme Example Lab - Example University. All rights reserved." in index_html


def test_neutral_defaults_omit_deployment_identity_and_login_server(app):
    app.config.update(
        SITE_NAME="Lab Playground",
        SITE_BRAND_TAG="",
        SITE_ORG="",
        VPN_LOGIN_SERVER="",
    )

    login_html = app.test_client().get("/").get_data(as_text=True)
    index_html = _authenticated_client(app).get("/").get_data(as_text=True)
    rendered = login_html + index_html

    assert "<title>Login - Lab Playground</title>" in login_html
    assert "<h3>Lab Playground</h3>" in login_html
    assert "sudo tailscale up --auth-key=" in index_html
    assert "--login-server" not in index_html
    assert "Acme" not in rendered
    assert "Example University" not in rendered
    assert "vpn.example.edu" not in rendered


@pytest.mark.parametrize(
    "password",
    [
        None,
        "admin",
        "admin123",
        "ChangeThisPassword123",
        "change-this-password-immediately",
    ],
)
def test_production_refuses_to_seed_admin_with_missing_or_known_password(app, monkeypatch, password):
    monkeypatch.setenv("FLASK_ENV", "production")
    if password is None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASSWORD", password)

    with app.app_context(), pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        init_db.create_admin_account()

    with app.app_context():
        assert User.query.filter_by(login_id="admin").first() is None



@pytest.mark.parametrize("flask_env", ["", "staging"])
def test_non_development_modes_refuse_known_admin_password(app, monkeypatch, flask_env):
    monkeypatch.setenv("FLASK_ENV", flask_env)
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")

    with app.app_context(), pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        init_db.create_admin_account()


def test_admin_configuration_error_is_not_retried(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    create_calls = []

    def create_test_app():
        create_calls.append(True)
        return app

    monkeypatch.setattr(init_db, "create_app", create_test_app)
    monkeypatch.setattr(init_db, "verify_schema", lambda: None)
    monkeypatch.setattr(init_db, "import_student_whitelist", lambda: None)
    monkeypatch.setattr(init_db.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        init_db.init_database()

    assert len(create_calls) == 1


def test_development_keeps_default_admin_password(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with app.app_context():
        init_db.create_admin_account()
        admin = User.query.filter_by(login_id="admin").one()
        assert admin.check_password("admin123")


def test_production_seeds_admin_with_operator_password(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", "operator-supplied-strong-password")

    with app.app_context():
        init_db.create_admin_account()
        admin = User.query.filter_by(login_id="admin").one()
        assert admin.check_password("operator-supplied-strong-password")


# Generated, not literal: only the length is under test.
@pytest.mark.parametrize("length, seeds", [(15, False), (16, True), (32, True)])
def test_production_admin_password_needs_sixteen_characters(app, monkeypatch, length, seeds):
    password = "k" * length
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("ADMIN_PASSWORD", password)

    with app.app_context():
        if seeds:
            init_db.create_admin_account()
            assert User.query.filter_by(login_id="admin").one().check_password(password)
        else:
            with pytest.raises(RuntimeError, match="ADMIN_PASSWORD") as refused:
                init_db.create_admin_account()
            assert "16" in str(refused.value)
            assert password not in str(refused.value)
            assert User.query.filter_by(login_id="admin").first() is None


@pytest.mark.parametrize("password", [
    None, "", "admin123", "change-this-password-immediately", "k" * 15, "k" * 16,
    " " * 4 + "k" * 15 + " " * 4,
])
def test_preflight_and_startup_agree_on_admin_password(app, monkeypatch, password):
    """An .env that passes the host preflight must not then stop the
    container at startup, and the reverse."""
    monkeypatch.setenv("FLASK_ENV", "production")
    if password is None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASSWORD", password)
    preflight_refuses = any(line.startswith("ADMIN_PASSWORD:")
                            for line in preflight_env.problems({"ADMIN_PASSWORD": password}))

    with app.app_context():
        try:
            init_db.create_admin_account()
            startup_refuses = False
        except init_db.AdminConfigurationError:
            startup_refuses = True

    assert preflight_refuses == startup_refuses


def _raise_validate_config_error():
    raise ValueError("Configuration errors:\n  - PROXMOX_HOST is not set")


@pytest.mark.parametrize("failure", ["validate_config", "samesite"])
def test_configuration_error_fails_at_once_instead_of_waiting_for_database(
        monkeypatch, capsys, failure):
    """A bad .env never fixes itself; retrying it 30 times as 'Database not
    ready' hides the real cause behind a misleading message."""
    create_calls = []

    def create_misconfigured_app():
        create_calls.append(True)
        if failure == "samesite":
            return create_app(testing=True)
        return _raise_validate_config_error()

    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "sideways" if failure == "samesite" else "Lax")
    monkeypatch.setattr(init_db, "create_app", create_misconfigured_app)
    monkeypatch.setattr(init_db.time, "sleep", lambda _seconds: None)

    with pytest.raises(ValueError):
        init_db.init_database()

    assert len(create_calls) == 1
    assert "Database not ready" not in capsys.readouterr().out


def test_admin_guard_uses_the_shared_production_check(app, monkeypatch):
    """init_db and app.config must agree on what counts as production."""
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(init_db, "is_production", lambda: True)

    with app.app_context(), pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        init_db.create_admin_account()


def test_mixed_case_development_keeps_default_admin_password(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "Development")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with app.app_context():
        init_db.create_admin_account()
        assert User.query.filter_by(login_id="admin").one().check_password("admin123")


def _seed_admin(login_id="admin", password="existing-password"):
    admin = User(login_id=login_id, is_admin=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return admin


def test_seed_disabled_creates_no_admin_account(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "false")

    with app.app_context():
        init_db.create_admin_account()
        assert User.query.filter_by(login_id="admin").first() is None


def test_seed_disabled_leaves_existing_admin_untouched(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "false")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-different-password")

    with app.app_context():
        _seed_admin()
        init_db.create_admin_account()
        admin = User.query.filter_by(login_id="admin").one()
        assert admin.is_admin
        assert not admin.disabled
        assert admin.check_password("existing-password")


@pytest.mark.parametrize("raw", [None, "", "true", "TRUE", "1"])
def test_seed_enabled_or_unset_keeps_current_behavior(app, monkeypatch, raw):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("ADMIN_PASSWORD", "operator-supplied-strong-password")
    if raw is None:
        monkeypatch.delenv("SEED_ADMIN_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("SEED_ADMIN_ACCOUNT", raw)

    with app.app_context():
        init_db.create_admin_account()
        admin = User.query.filter_by(login_id="admin").one()
        assert admin.is_admin
        assert admin.check_password("operator-supplied-strong-password")


def test_production_without_seeding_needs_no_admin_password(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "false")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(init_db, "create_app", lambda: app)
    monkeypatch.setattr(init_db, "verify_schema", lambda: None)
    monkeypatch.setattr(init_db, "import_student_whitelist", lambda: None)

    init_db.init_database()

    with app.app_context():
        assert User.query.filter_by(login_id="admin").first() is None


def test_production_with_seeding_still_refuses_default_password(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "true")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")

    with app.app_context(), pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        init_db.create_admin_account()

    with app.app_context():
        assert User.query.filter_by(login_id="admin").first() is None


def test_seed_flag_typo_is_refused_rather_than_seeding(app, monkeypatch):
    # "flase" must not silently fall back to seeding a generic admin.
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "flase")

    with app.app_context(), pytest.raises(RuntimeError, match="SEED_ADMIN_ACCOUNT"):
        init_db.create_admin_account()

    with app.app_context():
        assert User.query.filter_by(login_id="admin").first() is None


def test_seed_disabled_still_promotes_listed_admin_users(app, monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("SEED_ADMIN_ACCOUNT", "false")
    monkeypatch.setenv("ADMIN_USERS", "ta1")

    with app.app_context():
        ta = User(login_id="ta1", is_admin=False)
        ta.set_password("x-not-real")
        db.session.add(ta)
        db.session.commit()
        init_db.create_admin_account()
        assert User.query.filter_by(login_id="ta1").one().is_admin
