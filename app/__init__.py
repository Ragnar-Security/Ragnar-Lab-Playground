"""
Lab Playground application package

This package contains all application modules for managing Proxmox VMs.

Modules:
    - config: Configuration management
    - models: Database models
    - proxmox_service: Proxmox API integration
    - template_manager: VM template management
    - tasks: Background tasks
    - routes: Flask API endpoints
"""

import secrets

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import urllib3
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import (
    PROXMOX_VERIFY_SSL,
    config,
    session_cookie_settings,
    trusted_proxy_hops,
    validate_config,
)
from .models import db

# With verification off, urllib3 would warn on every Proxmox request;
# validate_config() says it once at startup instead. With verification on,
# the warning stays live so any other unverified request is still visible.
if not PROXMOX_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__version__ = "1.0.0"
__all__ = ["create_app", "db"]


def create_app(testing=False):
    """
    Application factory function

    Creates and configures the Flask application with all blueprints and settings.

    Args:
        testing: If True, use SQLite in-memory and skip config validation

    Returns:
        Flask: Configured Flask application
    """
    app = Flask(__name__, template_folder='../templates', static_folder='../static')

    # Load configuration
    app.config.from_object(config)

    if testing:
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['SECRET_KEY'] = 'test-secret-key'
        # Clear MySQL-only pool options inherited from ProductionConfig.
        # SQLite uses StaticPool which rejects pool_size/max_overflow/etc.
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
    else:
        # Validate configuration in non-test mode
        if not config.TESTING:
            try:
                validate_config()
            except ValueError as e:
                import logging
                logging.getLogger(__name__).error(f"Configuration error: {e}")
                raise
        # A missing bind mount must fail the container immediately instead of
        # presenting an apparently healthy portal with no deployment groups.
        from .template_manager import load_template_config
        load_template_config()

    # Sessions are signed cookies; server-side revocation uses User.session_version.
    # `or`, not a getenv default: Compose passes an unset variable as an empty
    # string, and an empty key would leave the app unable to sign sessions.
    # In production validate_config() has already refused a missing key.
    import os
    if not testing:
        app.config['SECRET_KEY'] = os.getenv('SECRET_KEY') or secrets.token_hex(32)
    app.config.update(session_cookie_settings())

    proxy_hops = trusted_proxy_hops()
    if proxy_hops:
        # request.remote_addr then names the real client, which the password
        # reset rate limit keys on.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops)

    # Initialize database
    db.init_app(app)
    
    # Register blueprints
    from .routes import api
    from .auth import auth
    app.register_blueprint(api)
    app.register_blueprint(auth, url_prefix='/auth')

    @app.context_processor
    def inject_site_configuration():
        return {
            'site_name': app.config['SITE_NAME'],
            'site_brand_tag': app.config['SITE_BRAND_TAG'],
            'site_org': app.config['SITE_ORG'],
            'vpn_login_server': app.config['VPN_LOGIN_SERVER'],
            'self_service_password_reset': app.config['SELF_SERVICE_PASSWORD_RESET'],
        }

    
    # Database tables are managed externally (e.g., with migrations)

    # KOTH boot recovery + scheduler resume.
    if not app.config.get('TESTING'):
        with app.app_context():
            try:
                from .models import db as _db, KothGame
                # Any game stuck in 'deploying' (Flask crashed mid-deploy)
                # gets reverted to 'draft' so the admin can retry.
                stuck = KothGame.query.filter_by(status='deploying').all()
                if stuck:
                    for g in stuck:
                        app.logger.warning(
                            "KOTH game %d (%r) was stuck in 'deploying' on boot; "
                            "reverting to 'draft'", g.id, g.name)
                        g.status = 'draft'
                    _db.session.commit()
                # Resume the scoring loop if a game is currently active.
                if KothGame.query.filter_by(is_active=True).first():
                    from . import koth_scorer
                    koth_scorer.start_scheduler(app)
            except Exception as e:
                app.logger.warning(
                    "Could not run KOTH boot recovery: %s", e)

    return app
