"""
Configuration module for Lab Playground
Handles environment variables and Flask configuration
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

from . import production_policy as policy

load_dotenv()

logger = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parent.parent

# Site identity is deployment data. Committed defaults stay neutral so a
# newly cloned playground cannot accidentally present itself as another site.
_templates_path = Path(os.getenv('TEMPLATES_FILE') or 'templates.yaml')
if not _templates_path.is_absolute():
    _templates_path = APP_ROOT / _templates_path
TEMPLATES_FILE = str(_templates_path)
SITE_NAME = os.getenv('SITE_NAME') or 'Lab Playground'
SITE_BRAND_TAG = os.getenv('SITE_BRAND_TAG') or ''
SITE_ORG = os.getenv('SITE_ORG') or ''
VPN_LOGIN_SERVER = os.getenv('VPN_LOGIN_SERVER') or ''



def _env_number(name, default, cast):
    """Read a numeric env var, treating an explicit empty string the same as
    unset. `os.getenv(name, default)` returns the default only when the var
    is *missing*, not when it's set to ''; docker-compose env templates often
    pass '' for unset upstream values, which would otherwise crash
    float('') / int('') at import time."""
    raw = os.getenv(name)
    if raw is None or raw == '':
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r in environment; falling back to default %r",
            name, raw, default)
        return cast(default)


def _env_bool(name, default):
    """Read a boolean env var; unset or empty means `default`.

    Unrecognised values fall back to the default with a warning rather than
    to False, so a typo in a security switch cannot silently weaken it."""
    raw = os.getenv(name)
    if not (raw or '').strip():
        return default
    value = policy.parse_bool(raw)
    if value is None:
        logger.warning("Invalid %s=%r in environment; falling back to default %r",
                       name, raw, default)
        return default
    return value


def is_production():
    """Anything but an explicit development or testing FLASK_ENV is production.

    The one production check: config-class selection below and
    scripts/init_db.py's bootstrap admin guard use it too, so an unset or
    misspelled FLASK_ENV gets the strict checks everywhere."""
    return policy.is_production_env(os.getenv('FLASK_ENV'))


# ============ PROXMOX API CONFIGURATION ============
PROXMOX_HOST = os.getenv('PROXMOX_HOST', '')
PROXMOX_USER = os.getenv('PROXMOX_USER', 'root@pam')
PROXMOX_PASSWORD = os.getenv('PROXMOX_PASSWORD', '')
PROXMOX_TOKEN_ID = os.getenv('PROXMOX_TOKEN_ID', '')
PROXMOX_TOKEN_SECRET = os.getenv('PROXMOX_TOKEN_SECRET', '')
# Credentials travel on every API call, so verification is on unless the
# operator turns it off. A self-signed cluster should prefer
# PROXMOX_CA_BUNDLE (a PEM path) over disabling verification.
PROXMOX_VERIFY_SSL = _env_bool('PROXMOX_VERIFY_SSL', True)
PROXMOX_CA_BUNDLE = (os.getenv('PROXMOX_CA_BUNDLE') or '').strip()


def proxmox_tls_verify():
    """The `verify_ssl` value for ProxmoxAPI: False, True, or a CA bundle path
    (proxmoxer hands it to requests' `verify`, which accepts all three)."""
    if not PROXMOX_VERIFY_SSL:
        return False
    return PROXMOX_CA_BUNDLE or True


# ============ NODE SELECTION CONFIGURATION ============
ENABLE_LOAD_BALANCING = os.getenv('ENABLE_LOAD_BALANCING', 'false').lower() == 'true'
FIXED_NODE = os.getenv('FIXED_NODE', '')
CPU_THRESHOLD = _env_number('CPU_THRESHOLD', '80.0', float)
MEMORY_THRESHOLD = _env_number('MEMORY_THRESHOLD', '85.0', float)

# ============ ORPHAN REAPER CONFIGURATION ============
# A fallback for clones whose VMRecord was never committed because the app
# stopped mid-deploy. The grace window is deliberately long so an in-flight
# deployment can recover before this independent sweeper may act.
ORPHAN_REAPER_ENABLED = (os.environ.get('ORPHAN_REAPER_ENABLED') or 'true').lower() == 'true'
ORPHAN_REAPER_DRY_RUN = (os.environ.get('ORPHAN_REAPER_DRY_RUN') or 'false').lower() == 'true'
ORPHAN_REAPER_GRACE_MINUTES = _env_number('ORPHAN_REAPER_GRACE_MINUTES', 60, int)
# A sweep runs inline in the expiry loop, after that loop's expiry pass. One
# hung guest can cost 60 s (stop) + 120 s (delete), so a backlog would hold
# up the next expiry pass. A sweep starts no new guest once it has run
# MAX_PASS_SECONDS or acted on MAX_PASS_GUESTS; the rest wait for the next
# sweep. Worst-case sweep length: MAX_PASS_SECONDS + 180 s.
ORPHAN_REAPER_MAX_PASS_SECONDS = _env_number('ORPHAN_REAPER_MAX_PASS_SECONDS', 120, int)
ORPHAN_REAPER_MAX_PASS_GUESTS = _env_number('ORPHAN_REAPER_MAX_PASS_GUESTS', 10, int)

# ============ VM LIFECYCLE LOCKS ============
# Off (default): vm_lifecycle_lock is a process lock, which excludes every
# other holder while gunicorn runs one worker. On: each holder also takes
# MySQL GET_LOCK on a second pool connection, so workers exclude each other.
# MUST be true before running more than one worker; otherwise two workers
# can stop, delete or retire the same VMID at once.
DISTRIBUTED_VM_LOCKS = (os.environ.get('DISTRIBUTED_VM_LOCKS') or 'false').lower() == 'true'


def untracked_survivors_file():
    """Append-only JSONL of failed-deploy survivors whose VMRecord could not be
    committed. It must live on persistent storage; the default is the
    bind-mounted data/ directory. Read per call so tests can redirect it."""
    path = Path(os.getenv('UNTRACKED_SURVIVORS_FILE')
                or 'data/untracked-survivors.jsonl')
    if not path.is_absolute():
        path = APP_ROOT / path
    return str(path)


# ============ DATABASE CONFIGURATION ============
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///instance/app.db')

# ============ GROUP VLAN POOL ============
GROUP_ENTRY_VLAN_MIN = _env_number('GROUP_ENTRY_VLAN_MIN', '300', int)
GROUP_ENTRY_VLAN_MAX = _env_number('GROUP_ENTRY_VLAN_MAX', '400', int)
GROUP_INTERNAL_VLAN_MIN = _env_number('GROUP_INTERNAL_VLAN_MIN', '400', int)
GROUP_INTERNAL_VLAN_MAX = _env_number('GROUP_INTERNAL_VLAN_MAX', '500', int)


def get_group_vlan_ranges():
    """Return ((entry_min, entry_max), (internal_min, internal_max)) for student groups."""
    return (
        (GROUP_ENTRY_VLAN_MIN, GROUP_ENTRY_VLAN_MAX),
        (GROUP_INTERNAL_VLAN_MIN, GROUP_INTERNAL_VLAN_MAX),
    )


# --- King of the Hill defaults ---------------------------------------------
KOTH_DEFAULT_SCAN_INTERVAL_SEC = _env_number('KOTH_DEFAULT_SCAN_INTERVAL_SEC', 12, int)
KOTH_DEFAULT_CLAIM_COOLDOWN_SEC = _env_number('KOTH_DEFAULT_CLAIM_COOLDOWN_SEC', 30, int)
KOTH_DEFAULT_BOX_OWNERSHIP_SCORE = _env_number('KOTH_DEFAULT_BOX_OWNERSHIP_SCORE', 1, float)
KOTH_DEFAULT_PORT_OPEN_SCORE = _env_number('KOTH_DEFAULT_PORT_OPEN_SCORE', 3, float)
KOTH_DEFAULT_EXP_SCORING = (os.environ.get('KOTH_DEFAULT_EXP_SCORING') or 'true').lower() == 'true'
KOTH_DEFAULT_EXP_VAL = _env_number('KOTH_DEFAULT_EXP_VAL', 60, int)
# How new games capture box ownership. 'file' = Proxmox guest-agent read
# of the claim file: one HTTPS API call per box per tick. 'beacon' = read
# the token off a TCP listener the team runs: one extra local connect per
# box per tick, in the same bounded fan-out as the service probes, and
# zero Proxmox API calls. 'both' = beacon first, file as fallback. 'both'
# is the default for NEW games so Linux boxes get the cheap path while
# the Windows templates keep the file. Existing rows keep whatever is in
# the column — see KothGame.claim_mode.
KOTH_DEFAULT_CLAIM_MODE = (os.environ.get('KOTH_DEFAULT_CLAIM_MODE') or 'both').lower()
KOTH_DEFAULT_CLAIM_PORT = _env_number('KOTH_DEFAULT_CLAIM_PORT', 31337, int)
KOTH_CLAIM_MODES = ('file', 'beacon', 'both')
# Wall-clock seconds between KothScoreSnapshot history rows. The live
# scoreboard reads KothTeam.score, so nothing on a request path depends on
# this; it only bounds how fast the history table grows (a full class on a
# 12s tick writes one row per team per tick, tens of thousands per game).
KOTH_SNAPSHOT_INTERVAL_SEC = _env_number('KOTH_SNAPSHOT_INTERVAL_SEC', 60, int)
# Per-team color palette. Index = team selection order. Limits the
# maximum number of teams per game (UNIQUE(game_id, color) on koth_teams
# enforces no reuse within a game). 24 entries cover a large classroom
# with spare slots. All chosen to be visually distinct on the brutalist
# dark theme (#0a0a0c bg) and to avoid the amber UI accent (#ffb000).
# First 8 names are unchanged from the original palette so existing data
# and tests stay valid.
KOTH_TEAM_COLORS = [
    "red", "blue", "green", "orange", "purple", "yellow", "cyan", "magenta",
    "deeppink", "lime", "mediumslateblue", "coral",
    "teal", "tomato", "skyblue", "violet",
    "springgreen", "crimson", "turquoise", "salmon",
    "plum", "seagreen", "dodgerblue", "orchid",
]


def get_koth_defaults():
    return {
        "scan_interval_sec": KOTH_DEFAULT_SCAN_INTERVAL_SEC,
        "claim_cooldown_sec": KOTH_DEFAULT_CLAIM_COOLDOWN_SEC,
        "claim_mode": (KOTH_DEFAULT_CLAIM_MODE
                       if KOTH_DEFAULT_CLAIM_MODE in KOTH_CLAIM_MODES else 'both'),
        "claim_port": KOTH_DEFAULT_CLAIM_PORT,
        "box_ownership_score": KOTH_DEFAULT_BOX_OWNERSHIP_SCORE,
        "port_open_score": KOTH_DEFAULT_PORT_OPEN_SCORE,
        "exp_scoring": KOTH_DEFAULT_EXP_SCORING,
        "exp_val": KOTH_DEFAULT_EXP_VAL,
    }


def _reset_token_ttl_minutes():
    """Lifetime of an admin-issued reset link. A zero or negative value would
    mint links that are dead on arrival, so it falls back to the default."""
    default = 60
    minutes = _env_number('PASSWORD_RESET_TOKEN_TTL_MINUTES', default, int)
    if minutes < 1:
        logger.warning("PASSWORD_RESET_TOKEN_TTL_MINUTES=%r must be at least 1; using %r",
                       minutes, default)
        return default
    return minutes


# ============ FLASK CONFIGURATION ============
class Config:
    """Base Flask configuration"""
    DEBUG = False
    TESTING = False
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JSON_SORT_KEYS = False
    SITE_NAME = SITE_NAME
    SITE_BRAND_TAG = SITE_BRAND_TAG
    SITE_ORG = SITE_ORG
    VPN_LOGIN_SERVER = VPN_LOGIN_SERVER
    # The roster SIS ID is the only proof of identity a self-service reset
    # asks for, on top of the login ID. Off unless the operator opts in:
    # sites that consider that ID guessable or widely known leave it off and
    # reset passwords through an administrator instead.
    SELF_SERVICE_PASSWORD_RESET = _env_bool('SELF_SERVICE_PASSWORD_RESET', False)
    PASSWORD_RESET_TOKEN_TTL_MINUTES = _reset_token_ttl_minutes()

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    # Sized for a full classroom with everyone connected at once: every
    # team, multiple tabs each, plus admin/SSE/normal HTTP. The default pool
    # (5 + 10) once caused a burst of 503/500s: every KOTH SSE client
    # parked a connection. Pair with the explicit db.session.remove() in
    # the SSE handler so this headroom isn't burned by the same bug.
    #
    # MySQL 8's default max_connections is 151; docker-compose.prod.yml
    # raises it to 300. Each lifecycle-lock holder keeps its session
    # connection checked out through the Proxmox stop and delete (up to
    # ~180 s), and with DISTRIBUTED_VM_LOCKS a second one for GET_LOCK. A
    # class-wide destroy wave of 45 students plus the expiry thread is then
    # 2 * 46 = 92 connections, plus about 45 /list renders that also hold
    # one across Proxmox calls: ~137. 50 + 100 = 150 covers it; one worker
    # at a full pool leaves 150 of MySQL's 300 for migrations, mysqldump and
    # a shell, and two workers at a full pool still fit (300). With the flag
    # off the same wave needs ~92. tests/test_cross_worker_coordination.py
    # holds this budget. pool_pre_ping recycles connections the server
    # closed (wait_timeout).
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 50,
        'max_overflow': 100,
        'pool_pre_ping': True,
        'pool_recycle': 3600,
        'pool_timeout': 10,
    }

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'

# Select config based on environment, by the same rule as is_production().
FLASK_ENV = policy.normalized_flask_env(os.getenv('FLASK_ENV'))
if FLASK_ENV == 'development':
    config = DevelopmentConfig
elif FLASK_ENV == 'testing':
    config = TestingConfig
else:
    config = ProductionConfig


def session_cookie_settings():
    """Session cookie attributes, read when the app is created.

    Production sits behind TLS (Caddy), so its cookie is Secure by default.
    Development and the integration stack serve plain HTTP, where a Secure
    cookie would never be sent back and nobody could stay logged in."""
    raw_samesite = (os.getenv('SESSION_COOKIE_SAMESITE') or 'Lax').strip()
    samesite = policy.samesite_value(raw_samesite)
    if samesite is None:
        # Werkzeug would raise on every login instead; fail at startup.
        raise ValueError(
            f"SESSION_COOKIE_SAMESITE must be Lax, Strict or None, not {raw_samesite!r}")
    return {
        'SESSION_COOKIE_SECURE': _env_bool('SESSION_COOKIE_SECURE', is_production()),
        'SESSION_COOKIE_HTTPONLY': _env_bool('SESSION_COOKIE_HTTPONLY', True),
        'SESSION_COOKIE_SAMESITE': samesite,
        'PERMANENT_SESSION_LIFETIME': _env_number('PERMANENT_SESSION_LIFETIME', 7200, int),
    }


def trusted_proxy_hops():
    """How many reverse proxies in front of the app append X-Forwarded-For.

    Zero (the default) ignores the header, which any client can forge.
    docker-compose.prod.yml sets 1 because every request there arrives
    through Caddy; without it each client looks like the Caddy container."""
    return max(_env_number('TRUSTED_PROXY_HOPS', 0, int), 0)


def _database_url_password():
    """Password embedded in DATABASE_URL, or None for passwordless backends."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    try:
        url = make_url(DATABASE_URL)
    except ArgumentError:
        return None  # SQLAlchemy reports the malformed URL itself at first use.
    if url.get_backend_name() == 'sqlite':
        return None
    return url.password or ''


def _production_secret_errors():
    errors = []
    problem = policy.secret_key_problem(os.getenv('SECRET_KEY'))
    if problem:
        errors.append(f"SECRET_KEY {problem}")
    db_password = _database_url_password()
    if db_password is not None and policy.db_password_problem(db_password):
        errors.append("DATABASE_URL uses an empty or published example database password")
    mysql_password = os.getenv('MYSQL_PASSWORD')
    if mysql_password is not None:
        problem = policy.db_password_problem(mysql_password)
        if problem:
            errors.append(f"MYSQL_PASSWORD {problem}")
    return errors


def validate_config():
    """
    Validate critical configuration on startup.
    Raises ValueError for fatal misconfigurations, logs warnings for non-fatal issues.
    """
    errors = []

    if not PROXMOX_HOST:
        errors.append("PROXMOX_HOST is not set")

    has_token = PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET
    has_password = PROXMOX_PASSWORD
    if not has_token and not has_password:
        errors.append("No Proxmox authentication configured. Set either PROXMOX_TOKEN_ID/PROXMOX_TOKEN_SECRET or PROXMOX_PASSWORD")

    if errors:
        raise ValueError("Configuration errors:\n  - " + "\n  - ".join(errors))

    # Validate that group VLAN pools don't overlap user VLAN pools (100-200 entry, 200-300 internal).
    # Treat MAX as exclusive (MIN == MAX is invalid, so [MIN, MAX) is the effective range).
    group_ranges_errors = []
    if GROUP_ENTRY_VLAN_MIN >= GROUP_ENTRY_VLAN_MAX:
        group_ranges_errors.append("GROUP_ENTRY_VLAN_MAX must be > GROUP_ENTRY_VLAN_MIN")
    if GROUP_INTERNAL_VLAN_MIN >= GROUP_INTERNAL_VLAN_MAX:
        group_ranges_errors.append("GROUP_INTERNAL_VLAN_MAX must be > GROUP_INTERNAL_VLAN_MIN")
    # Overlap with user entry pool [100, 200): non-overlap requires MIN >= 200 or MAX <= 100.
    if not (GROUP_ENTRY_VLAN_MIN >= 200 or GROUP_ENTRY_VLAN_MAX <= 100):
        logger.warning("GROUP_ENTRY_VLAN range overlaps the user entry VLAN pool (100-200). "
                       "Collisions will be rejected at allocation time.")
    # Overlap with user internal pool [200, 300): non-overlap requires MIN >= 300 or MAX <= 200.
    if not (GROUP_INTERNAL_VLAN_MIN >= 300 or GROUP_INTERNAL_VLAN_MAX <= 200):
        logger.warning("GROUP_INTERNAL_VLAN range overlaps the user internal VLAN pool (200-300). "
                       "Collisions will be rejected at allocation time.")
    if group_ranges_errors:
        raise ValueError("Group VLAN config errors:\n  - " + "\n  - ".join(group_ranges_errors))

    if PROXMOX_VERIFY_SSL and PROXMOX_CA_BUNDLE:
        problem = policy.ca_bundle_problem(PROXMOX_CA_BUNDLE)
        if problem:
            raise ValueError(f"PROXMOX_CA_BUNDLE={PROXMOX_CA_BUNDLE!r} {problem}")
    if not PROXMOX_VERIFY_SSL:
        logger.warning(
            "PROXMOX_VERIFY_SSL=false: the Proxmox API certificate is not verified, so a "
            "network attacker can capture the API credentials. Prefer PROXMOX_CA_BUNDLE.")

    if is_production():
        secret_errors = _production_secret_errors()
        if secret_errors:
            raise ValueError("Refusing to start in production:\n  - " + "\n  - ".join(secret_errors))
        return

    if not (os.getenv('SECRET_KEY') or '').strip():
        logger.warning("SECRET_KEY is not set. A random key will be generated (sessions won't persist across restarts)")
    db_password = _database_url_password()
    if db_password is not None and policy.db_password_problem(db_password):
        logger.warning("DATABASE_URL uses a published example password; production refuses to start with it")
