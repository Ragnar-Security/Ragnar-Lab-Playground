#!/usr/bin/env python3
"""Check an operator .env against the app's production rules on the Docker
host, before `docker compose -f docker-compose.prod.yml up`.

    python3 scripts/preflight_env.py [path/to/.env]     # default: ./.env

Prints one line per problem as `KEY: problem`, never the value, and exits
1 when there is any; exits 2 when the file cannot be read; 0 otherwise.
Before that it prints, on stdout, which VPN mode is active and, per enabled
backend, whether a router is used and which client networks the labs admit
(network ranges are not secrets).

The rules are the ones the app enforces at startup (app/production_policy.py
and the backend checks of app/config.py:validate_config(), mirrored here),
plus MYSQL_ROOT_PASSWORD, which only the MySQL container reads and the app
therefore never sees. Needs only the Python standard library: the policy
and network-slot modules are loaded by path so the host needs neither Flask
nor the app's other dependencies.
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
    'aws': ('AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID'),
    'gcp': ('GCP_PROJECT', 'GCP_REGION', 'GCP_ZONE', 'GCP_NETWORK'),
}
ROUTER_KEYS = {'aws': 'AWS_ROUTER_INSTANCE_ID', 'gcp': 'GCP_ROUTER_INSTANCE'}
DEFAULT_LAB_CIDRS = {'aws': '172.20.0.0/16', 'gcp': '172.21.0.0/16'}
LAB_CIDR_RESERVED = ('10.0.0.0/8', '172.17.0.0/16')
# Slot 500's /26 sits at base + 500 * 64, so a base holds 501 * 64 addresses.
LAB_CIDR_MIN_ADDRESSES = 501 * 64
LAB_SITE_ID = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?')


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


policy = _load('production_policy', 'app/production_policy.py')
slots = _load('_preflight_network_slots', 'app/backends/network_slots.py')


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


def _lab_cidr_problem(cidr, others, tailnet, clients):
    """What app/config.py refuses in a cloud lab base, without the value.
    `others` maps another enabled cloud's CIDR key to its base; `tailnet`
    reserves the tailnet range (VPN_MODE=headscale); `clients` are the
    backend's parsed client networks."""
    try:
        network = ipaddress.IPv4Network(cidr)
    except ValueError:
        return 'must be an IPv4 network address/prefix'
    reserved = LAB_CIDR_RESERVED + ((slots.TAILNET_CIDR,) if tailnet else ())
    for blocked in sorted(reserved, key=ipaddress.IPv4Network):
        if network.overlaps(ipaddress.IPv4Network(blocked)):
            return f'overlaps reserved range {blocked}'
    if any(network.overlaps(ipaddress.IPv4Network(client)) for client in clients):
        return 'overlaps a network in the client CIDRs'
    if network.num_addresses < LAB_CIDR_MIN_ADDRESSES:
        return 'is too small for slot 500; needs a /17 or larger'
    for key, other in others.items():
        try:
            if network.overlaps(ipaddress.IPv4Network(other)):
                return f'overlaps {key}'
        except ValueError:
            pass  # reported against that key itself
    return None


def _aws_client_cidrs(env, mode):
    """(networks, problem) for AWS_CLIENT_CIDRS as app/config.py reads it:
    unset is the tailnet, under VPN_MODE=headscale only."""
    raw = (env.get('AWS_CLIENT_CIDRS') or '').strip()
    if raw:
        try:
            return slots.parse_client_cidrs(raw), None
        except ValueError as error:
            return (), str(error)
    if mode != 'headscale':
        return (), f'required when VPN_MODE={mode}'
    return (slots.TAILNET_CIDR,), None


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
    mode = policy.vpn_mode(env.get('VPN_MODE'))
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
        clients = ()
        if name == 'aws':
            clients, problem = _aws_client_cidrs(env, mode)
            check('AWS_CLIENT_CIDRS', problem)
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
        check(key, _lab_cidr_problem(bases[key], others, mode == 'headscale', clients))


_MODE_SUMMARY = {
    'headscale': 'students reach labs over the Headscale tailnet; the dashboard '
                 'shows Tailscale setup and each student\'s Headscale key',
    'external': 'students reach labs over a VPN the portal does not run; the '
                'dashboard shows VPN_NAME and the VPN instructions, never a Headscale key',
    'none': 'students are already on an internal network routed to the labs; '
            'the dashboard shows no VPN instructions. Labs stay private',
}


def summary(env):
    """What the file selects, one line each: the VPN mode and, per enabled
    backend, its router and the client networks its labs admit."""
    mode = policy.vpn_mode(env.get('VPN_MODE'))
    lines = [f'VPN_MODE={mode}: {_MODE_SUMMARY[mode]}' if mode in _MODE_SUMMARY
             else 'VPN_MODE: not a known mode (see the problems)']
    for name in _enabled_backends(env.get('ENABLED_BACKENDS')):
        if name == 'proxmox':
            lines.append('proxmox: no router; labs are reached through the jumpbox\'s '
                         'routed lab VLANs (10.0.0.0/8), whatever the VPN')
            continue
        if name not in ROUTER_KEYS:
            continue
        key = ROUTER_KEYS[name]
        router = (f'router {key} (started for launches, stopped when idle)'
                  if (env.get(key) or '').strip() else
                  f'no router ({key} empty): the VPN or internal network must '
                  f'route to {name.upper()}_LAB_CIDR itself')
        if name == 'aws':
            clients, problem = _aws_client_cidrs(env, mode)
            admits = ('lab security groups admit ' + ', '.join(clients)
                      if not problem else 'AWS_CLIENT_CIDRS is invalid (see the problems)')
        else:
            admits = ('client networks are the operator\'s <site-id>-lab-tailnet '
                      'firewall rule, not portal settings')
        lines.append(f'{name}: {router}; {admits}')
    return lines


def problems(env):
    found = []

    def check(key, problem):
        if problem:
            found.append(f'{key}: {problem}')

    for key, problem in policy.vpn_problems(
            policy.vpn_mode(env.get('VPN_MODE')), env.get('VPN_INSTRUCTIONS_URL'),
            env.get('VPN_INSTRUCTIONS')):
        check(key, problem)
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
    env = parse_env(text)
    for line in summary(env):
        print(line)
    found = problems(env)
    for line in found:
        print(line, file=sys.stderr)
    if found:
        return 1
    print(f'{path}: passes the production preflight')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
