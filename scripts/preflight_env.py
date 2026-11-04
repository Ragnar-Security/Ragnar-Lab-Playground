#!/usr/bin/env python3
"""Check an operator .env against the app's production rules on the Docker
host, before `docker compose -f docker-compose.prod.yml up`.

    python3 scripts/preflight_env.py [path/to/.env]     # default: ./.env

Prints one line per problem as `KEY: problem`, never the value, and exits
1 when there is any; exits 2 when the file cannot be read; 0 otherwise.

The rules are the ones the app enforces at startup (app/production_policy.py
and the backend checks of app/config.py:validate_config(), mirrored here),
plus MYSQL_ROOT_PASSWORD, which only the MySQL container reads and the app
therefore never sees. Needs only the Python standard library: the policy
module is loaded by path so the host needs neither Flask nor the app's
other dependencies.
"""

import importlib.util
import ipaddress
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# An exact release version, optionally pinned to its image digest.
RELEASE_VERSION = re.compile(
    r'\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:@sha256:[0-9a-f]{64})?')

# Mirrors app/config.py: which backends exist, what each enabled cloud
# needs, and where its lab networks may live.
KNOWN_BACKENDS = ('proxmox', 'aws', 'gcp')
CLOUD_REQUIRED = {
    'aws': ('AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID',
            'AWS_ROUTER_INSTANCE_ID'),
    'gcp': ('GCP_PROJECT', 'GCP_REGION', 'GCP_ZONE', 'GCP_NETWORK',
            'GCP_ROUTER_INSTANCE'),
}
DEFAULT_LAB_CIDRS = {'aws': '172.20.0.0/16', 'gcp': '172.21.0.0/16'}
LAB_CIDR_RESERVED = ('10.0.0.0/8', '100.64.0.0/10', '172.17.0.0/16')
# Slot 500's /26 sits at base + 500 * 64, so a base holds 501 * 64 addresses.
LAB_CIDR_MIN_ADDRESSES = 501 * 64
LAB_SITE_ID = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?')


def _load_policy():
    spec = importlib.util.spec_from_file_location(
        'production_policy', REPO / 'app' / 'production_policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _load_policy()


def _unquote(raw):
    """Value of one assignment the way Compose reads an .env file: quotes
    delimit the value, and an unquoted value ends at a ` #` comment."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            inner = value[1:end]
            return inner.replace('\\"', '"') if quote == '"' else inner
    for marker in (' #', '\t#'):
        cut = value.find(marker)
        if cut != -1:
            value = value[:cut]
    return value.strip()


def parse_env(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):]
        key, raw = line.split('=', 1)
        values[key.strip()] = _unquote(raw)  # later assignments win, as in Compose
    return values


def _enabled_backends(raw):
    """ENABLED_BACKENDS as app/config.py reads it: unset or empty is
    ('proxmox',); otherwise lower-case unique names in order, '' included."""
    if not (raw or '').strip():
        return ('proxmox',)
    names = []
    for part in raw.split(','):
        name = part.strip().lower()
        if name not in names:
            names.append(name)
    return tuple(names)


def _lab_cidr_problem(cidr, others):
    """What app/config.py refuses in a cloud lab base, without the value.
    `others` maps another enabled cloud's CIDR key to its base."""
    try:
        network = ipaddress.IPv4Network(cidr)
    except ValueError:
        return 'must be an IPv4 network address/prefix'
    for reserved in LAB_CIDR_RESERVED:
        if network.overlaps(ipaddress.IPv4Network(reserved)):
            return f'overlaps reserved range {reserved}'
    if network.num_addresses < LAB_CIDR_MIN_ADDRESSES:
        return 'is too small for slot 500; needs a /17 or larger'
    for key, other in others.items():
        try:
            if network.overlaps(ipaddress.IPv4Network(other)):
                return f'overlaps {key}'
        except ValueError:
            pass  # reported against that key itself
    return None


def _backend_problems(env, check):
    backends = _enabled_backends(env.get('ENABLED_BACKENDS'))
    if any(name not in KNOWN_BACKENDS for name in backends):
        check('ENABLED_BACKENDS', 'must list only proxmox, aws or gcp, comma-separated')
    if 'proxmox' in backends:
        verify = policy.parse_bool(env.get('PROXMOX_VERIFY_SSL'))
        bundle = (env.get('PROXMOX_CA_BUNDLE') or '').strip()
        if verify is not False and bundle:
            # Compose mounts the PEM into the container; this checks the same
            # path on the host, so mount it at an identical path.
            problem = policy.ca_bundle_problem(bundle)
            check('PROXMOX_CA_BUNDLE', problem and f'{bundle!r} {problem} on this host')
        # Cloud admission reserves capacity under node 'aws'/'gcp'.
        if (env.get('FIXED_NODE') or '').strip().lower() in CLOUD_REQUIRED:
            check('FIXED_NODE', 'must not be named aws or gcp (cloud backend names)')

    clouds = [name for name in backends if name in CLOUD_REQUIRED]
    if not clouds:
        return
    site = (env.get('LAB_SITE_ID') or '').strip()
    if not site:
        check('LAB_SITE_ID', 'required when a cloud backend is enabled')
    elif not LAB_SITE_ID.fullmatch(site):
        check('LAB_SITE_ID', "must be 1-20 characters of a-z, 0-9 and '-', "
              "not starting or ending with '-'")
    bases = {f'{name.upper()}_LAB_CIDR':
             (env.get(f'{name.upper()}_LAB_CIDR') or '').strip() or DEFAULT_LAB_CIDRS[name]
             for name in clouds}
    for name in clouds:
        for key in CLOUD_REQUIRED[name]:
            if not (env.get(key) or '').strip():
                check(key, f'required when {name} is enabled')
        cap_key = f'{name.upper()}_MAX_VMS'
        try:
            cap = int((env.get(cap_key) or '').strip())
        except ValueError:
            cap = None
        if cap is None or cap < 1:
            check(cap_key, f'must be a positive integer when {name} is enabled')
        key = f'{name.upper()}_LAB_CIDR'
        others = {other: cidr for other, cidr in bases.items() if other != key}
        check(key, _lab_cidr_problem(bases[key], others))


def problems(env):
    found = []

    def check(key, problem):
        if problem:
            found.append(f'{key}: {problem}')

    check('SECRET_KEY', policy.secret_key_problem(env.get('SECRET_KEY')))
    # docker-compose.prod.yml builds DATABASE_URL from MYSQL_USER and
    # MYSQL_PASSWORD, so the passwords below are the ones that matter.
    for key in ('MYSQL_ROOT_PASSWORD', 'MYSQL_PASSWORD'):
        check(key, policy.db_password_problem(env.get(key)))
    # init_db only needs it to seed the first admin, but a deployment that
    # loses its database volume seeds again with whatever is here. With
    # seeding off nothing reads it, so it is not required.
    seed = policy.seed_admin_account(env.get('SEED_ADMIN_ACCOUNT'))
    if seed is None:
        check('SEED_ADMIN_ACCOUNT', 'must be true or false')
    elif seed:
        check('ADMIN_PASSWORD', policy.admin_password_problem(env.get('ADMIN_PASSWORD')))
    if policy.samesite_value(env.get('SESSION_COOKIE_SAMESITE')) is None:
        check('SESSION_COOKIE_SAMESITE', 'must be Lax, Strict or None')

    _backend_problems(env, check)

    release = env.get('LAB_PLAYGROUND_VERSION') or ''
    if release and not RELEASE_VERSION.fullmatch(release):
        # docker-compose.release.yml pulls this tag. A moving one such as
        # `latest` would let any restart change what production runs.
        check('LAB_PLAYGROUND_VERSION', 'must be an exact release such as '
              '1.2.3 or 1.2.3@sha256:<digest>, not a moving tag')
    return found


def main(argv):
    path = Path(argv[1] if len(argv) > 1 else '.env')
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        print(f'{path}: cannot read ({exc.strerror})', file=sys.stderr)
        return 2
    found = problems(parse_env(text))
    for line in found:
        print(line, file=sys.stderr)
    if found:
        return 1
    print(f'{path}: passes the production preflight')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
