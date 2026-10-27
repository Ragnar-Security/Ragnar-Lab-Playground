#!/usr/bin/env python3
"""Check an operator .env against the app's production rules on the Docker
host, before `docker compose -f docker-compose.prod.yml up`.

    python3 scripts/preflight_env.py [path/to/.env]     # default: ./.env

Prints one line per problem as `KEY: problem`, never the value, and exits
1 when there is any; exits 2 when the file cannot be read; 0 otherwise.

The rules are the ones the app enforces at startup (app/production_policy.py),
plus MYSQL_ROOT_PASSWORD, which only the MySQL container reads and the app
therefore never sees. Needs only the Python standard library: the policy
module is loaded by path so the host needs neither Flask nor the app's
other dependencies.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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

    verify = policy.parse_bool(env.get('PROXMOX_VERIFY_SSL'))
    bundle = (env.get('PROXMOX_CA_BUNDLE') or '').strip()
    if verify is not False and bundle:
        # Compose mounts the PEM into the container; this checks the same
        # path on the host, so mount it at an identical path.
        problem = policy.ca_bundle_problem(bundle)
        check('PROXMOX_CA_BUNDLE', problem and f'{bundle!r} {problem} on this host')
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
