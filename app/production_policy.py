"""Production rules for secrets and security settings.

Shared by app.config (startup), scripts/init_db.py (admin seeding) and
scripts/preflight_env.py (the operator's .env, checked on the Docker host
before `docker compose up`). Stdlib only, with no package-relative imports:
the preflight loads this file by path, so it runs without Flask and the
app's other dependencies and without executing app/__init__.py.

Every check takes a value and returns a problem message or None. Messages
never include the value, because the values are secrets.
"""

import os

NON_PRODUCTION_ENVS = frozenset({'development', 'testing'})

MIN_SECRET_KEY_LENGTH = 32
# Values published in this repository's examples, docs and test stacks.
# Anyone can read them, so a key or password equal to one is no secret.
KNOWN_SECRET_KEYS = frozenset({
    'change-this-to-a-random-secret-key-in-production',
    'your-random-secret-key-here',
    'your-secret-key-here-use-secrets.token-hex',
    'generate-with-python-secrets-token-hex-32',
    'test-secret-key-for-integration',
    'test-secret-key',
    'test-key',
    'test',
    'dev',
    'secret',
    'changeme',
})
KNOWN_DB_PASSWORDS = frozenset({
    '',
    'vmpassword',
    'rootpassword',
    'password',
    'changeme',
    'strong-user-password-change-this',
    'strong-root-password-change-this',
})
KNOWN_ADMIN_PASSWORDS = frozenset({
    'admin',
    'admin123',
    'changethispassword123',
    'change-this-password-immediately',
})

SAMESITE_VALUES = {'lax': 'Lax', 'strict': 'Strict', 'none': 'None'}
_TRUE = frozenset({'1', 'true', 'yes', 'on'})
_FALSE = frozenset({'0', 'false', 'no', 'off'})


def normalized_flask_env(raw):
    """FLASK_ENV as every production decision reads it."""
    return (raw or '').strip().lower()


def is_production_env(raw):
    """Anything but an explicit development or testing FLASK_ENV is
    production, so an unset or misspelled value gets the strict checks."""
    return normalized_flask_env(raw) not in NON_PRODUCTION_ENVS


def parse_bool(raw):
    """True/False for a recognised boolean string, None for empty or unknown."""
    value = (raw or '').strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def secret_key_problem(value):
    key = (value or '').strip()
    if not key or key.lower() in KNOWN_SECRET_KEYS or len(key) < MIN_SECRET_KEY_LENGTH:
        # Anyone who knows the signing key can forge a session cookie for
        # any user_id, including the first admin.
        return (f"must be a unique random value of at least {MIN_SECRET_KEY_LENGTH} "
                f"characters. Generate one with: "
                f"python -c 'import secrets; print(secrets.token_hex(32))'")
    return None


def db_password_problem(value):
    if (value or '').strip().lower() in KNOWN_DB_PASSWORDS:
        return "is empty or a published example database password"
    return None


def admin_password_problem(value):
    password = (value or '').strip()
    if not password or password.lower() in KNOWN_ADMIN_PASSWORDS:
        return "must be set to an operator-supplied password that is not a known default"
    return None


def samesite_value(raw):
    """The canonical SameSite attribute, or None when `raw` is not valid.
    Unset or empty means Lax."""
    return SAMESITE_VALUES.get((raw or 'Lax').strip().lower())


def ca_bundle_problem(path):
    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        # Otherwise every Proxmox call fails later with an opaque TLS error.
        return "is not a readable file"
    return None
