"""scripts/preflight_env.py as an operator runs it on the Docker host: a
bare interpreter, a .env file, and one line per problem."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO / 'scripts' / 'preflight_env.py'

GOOD = {
    'SECRET_KEY': 'a3f1' * 16,
    'MYSQL_ROOT_PASSWORD': 'Rt-9vKq2xW7mPz4LsN8d',
    'MYSQL_PASSWORD': 'Us-4hJd8cQ1nYb6TeR3w',
    'ADMIN_PASSWORD': 'Ad-7gFs2kL9pXv5MuC1q',
    'SESSION_COOKIE_SAMESITE': 'Lax',
    'PROXMOX_VERIFY_SSL': 'true',
    'PROXMOX_CA_BUNDLE': '',
    'ENABLED_BACKENDS': 'proxmox',
}

# Neutral placeholders: only key names are ever asserted on.
AWS_ENV = {
    'LAB_SITE_ID': 'site-a', 'AWS_REGION': 'us-test-1',
    'AWS_AVAILABILITY_ZONE': 'us-test-1a', 'AWS_LAB_VPC_ID': 'vpc-placeholder',
    'AWS_ROUTER_INSTANCE_ID': 'i-placeholder', 'AWS_MAX_VMS': '3',
}
GCP_ENV = {
    'GCP_PROJECT': 'project-placeholder', 'GCP_REGION': 'region-test1',
    'GCP_ZONE': 'region-test1-a', 'GCP_NETWORK': 'network-placeholder',
    'GCP_ROUTER_INSTANCE': 'router-placeholder', 'GCP_MAX_VMS': '2',
}


def valid_aws_env():
    return {**GOOD, **AWS_ENV, 'ENABLED_BACKENDS': 'aws'}


def valid_cloud_env():
    return {**GOOD, **AWS_ENV, **GCP_ENV, 'ENABLED_BACKENDS': 'aws,gcp'}


def _write_env(path, values):
    lines = ['# operator .env', 'MYSQL_DATABASE=vmdatabase', 'MYSQL_USER=vmuser']
    lines += [f'{key}={value}' for key, value in values.items() if value is not None]
    path.write_text('\n'.join(lines) + '\n')
    return path


def _run(*args, cwd=None):
    # -I -S: no site-packages, so the host needs nothing beyond the stdlib.
    return subprocess.run(
        [sys.executable, '-I', '-S', str(PREFLIGHT), *args],
        capture_output=True, text=True, cwd=cwd, check=False)


def test_good_file_passes(tmp_path):
    result = _run(str(_write_env(tmp_path / '.env', GOOD)))

    assert result.returncode == 0, result.stderr
    assert result.stderr == ''


def test_defaults_to_dot_env_in_working_directory(tmp_path):
    _write_env(tmp_path / '.env', GOOD)

    assert _run(cwd=tmp_path).returncode == 0


@pytest.mark.parametrize('key, value', [
    ('SECRET_KEY', None),
    ('SECRET_KEY', ''),
    ('SECRET_KEY', 'change-this-to-a-random-secret-key-in-production'),
    ('SECRET_KEY', 'a3f1' * 7 + 'abc'),
    ('MYSQL_ROOT_PASSWORD', None),
    ('MYSQL_ROOT_PASSWORD', ''),
    ('MYSQL_ROOT_PASSWORD', 'strong-root-password-change-this'),
    ('MYSQL_ROOT_PASSWORD', 'rootpassword'),
    ('MYSQL_PASSWORD', None),
    ('MYSQL_PASSWORD', ''),
    ('MYSQL_PASSWORD', 'strong-user-password-change-this'),
    ('ADMIN_PASSWORD', None),
    ('ADMIN_PASSWORD', ''),
    ('ADMIN_PASSWORD', 'change-this-password-immediately'),
    ('ADMIN_PASSWORD', 'admin123'),
    ('SESSION_COOKIE_SAMESITE', 'Sideways'),
    ('PROXMOX_CA_BUNDLE', '/nonexistent/proxmox-ca.pem'),
])
def test_each_bad_value_fails_naming_only_its_key(tmp_path, key, value):
    result = _run(str(_write_env(tmp_path / '.env', {**GOOD, key: value})))

    problems = result.stderr.splitlines()
    assert result.returncode == 1
    assert len(problems) == 1, problems
    assert problems[0].startswith(f'{key}:')
    if key not in ('SESSION_COOKIE_SAMESITE', 'PROXMOX_CA_BUNDLE') and value:
        assert value not in result.stdout + result.stderr


def test_secret_values_are_never_printed(tmp_path):
    weak = {**GOOD, 'SECRET_KEY': 'short-but-secret-ish',
            'MYSQL_ROOT_PASSWORD': 'rootpassword', 'ADMIN_PASSWORD': 'admin123'}
    result = _run(str(_write_env(tmp_path / '.env', weak)))

    assert result.returncode == 1
    for secret in ('short-but-secret-ish', 'rootpassword', 'admin123'):
        assert secret not in result.stdout + result.stderr


def test_unreadable_ca_bundle_fails(tmp_path):
    bundle = tmp_path / 'proxmox-ca.pem'
    bundle.write_text('placeholder')
    env = _write_env(tmp_path / '.env', {**GOOD, 'PROXMOX_CA_BUNDLE': str(bundle)})
    assert _run(str(env)).returncode == 0

    bundle.chmod(0)
    try:
        if _run(str(env)).returncode == 0:
            pytest.skip('this user can read a mode-000 file (root)')
        assert _run(str(env)).stderr.startswith('PROXMOX_CA_BUNDLE:')
    finally:
        bundle.chmod(0o600)


def test_ca_bundle_is_ignored_when_verification_is_off(tmp_path):
    env = _write_env(tmp_path / '.env', {
        **GOOD, 'PROXMOX_VERIFY_SSL': 'false', 'PROXMOX_CA_BUNDLE': '/nonexistent/ca.pem'})

    assert _run(str(env)).returncode == 0


def test_the_committed_example_is_refused(tmp_path):
    """Copying .env.example without filling it in must not pass."""
    result = _run(str(REPO / '.env.example'))

    keys = {line.split(':', 1)[0] for line in result.stderr.splitlines()}
    assert result.returncode == 1
    assert {'SECRET_KEY', 'MYSQL_ROOT_PASSWORD', 'MYSQL_PASSWORD', 'ADMIN_PASSWORD'} <= keys


def test_quotes_export_and_inline_comments_are_read_like_compose(tmp_path):
    env = tmp_path / '.env'
    env.write_text(
        f'export SECRET_KEY="{GOOD["SECRET_KEY"]}"\n'
        f"MYSQL_ROOT_PASSWORD='{GOOD['MYSQL_ROOT_PASSWORD']}'  # root\n"
        f'MYSQL_PASSWORD={GOOD["MYSQL_PASSWORD"]}  # app user\n'
        f'ADMIN_PASSWORD={GOOD["ADMIN_PASSWORD"]}\n'
        'SESSION_COOKIE_SAMESITE=strict  # case-insensitive\n')

    result = _run(str(env))

    assert result.returncode == 0, result.stderr


def test_later_assignment_wins(tmp_path):
    env = _write_env(tmp_path / '.env', GOOD)
    with env.open('a') as handle:
        handle.write('MYSQL_ROOT_PASSWORD=\n')

    result = _run(str(env))

    assert result.stderr.startswith('MYSQL_ROOT_PASSWORD:')


def test_missing_file_is_reported(tmp_path):
    result = _run(str(tmp_path / 'absent.env'))

    assert result.returncode == 2
    assert 'absent.env' in result.stderr


@pytest.mark.parametrize('admin_password', [None, '', 'admin123'])
def test_admin_password_not_required_when_admin_seeding_is_off(tmp_path, admin_password):
    values = {**GOOD, 'ADMIN_PASSWORD': admin_password, 'SEED_ADMIN_ACCOUNT': 'false'}
    result = _run(str(_write_env(tmp_path / '.env', values)))

    assert result.returncode == 0, result.stderr


def test_admin_password_still_checked_when_seeding_is_explicitly_on(tmp_path):
    values = {**GOOD, 'ADMIN_PASSWORD': 'admin123', 'SEED_ADMIN_ACCOUNT': 'true'}
    result = _run(str(_write_env(tmp_path / '.env', values)))

    assert result.returncode == 1
    assert result.stderr.startswith('ADMIN_PASSWORD:')


# Generated, not literal: only the length is under test, and a literal
# password-shaped fixture is one more synthetic hit for secret scanners.
@pytest.mark.parametrize('length, passes', [(15, False), (16, True), (32, True)])
def test_admin_password_needs_sixteen_characters(tmp_path, length, passes):
    password = 'k' * length
    result = _run(str(_write_env(tmp_path / '.env', {**GOOD, 'ADMIN_PASSWORD': password})))

    if passes:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert result.stderr.startswith('ADMIN_PASSWORD:')
        assert '16' in result.stderr
        assert password not in result.stdout + result.stderr


def test_unrecognised_seed_flag_is_reported(tmp_path):
    values = {**GOOD, 'SEED_ADMIN_ACCOUNT': 'flase'}
    result = _run(str(_write_env(tmp_path / '.env', values)))

    assert result.returncode == 1
    assert result.stderr.startswith('SEED_ADMIN_ACCOUNT:')


# LAB_PLAYGROUND_VERSION names the release image (docker-compose.release.yml).
# A floating value such as `latest` would let a restart silently change
# what production runs, so only an exact version (optionally with digest)
# passes. Source-build hosts do not set it, and then nothing is checked.
DIGEST = 'sha256:' + 'a' * 64


@pytest.mark.parametrize('value', ['latest', 'main', '1.0', 'v1.0.0', '1',
                                   f'1.0.0@{DIGEST[:-1]}', f'1.0.0@{DIGEST.upper()}',
                                   f'latest@{DIGEST}', '1.0.0@sha512:' + 'a' * 64])
def test_release_version_must_be_exact(tmp_path, value):
    result = _run(str(_write_env(tmp_path / '.env',
                                 {**GOOD, 'LAB_PLAYGROUND_VERSION': value})))

    assert result.returncode == 1
    assert result.stderr.startswith('LAB_PLAYGROUND_VERSION:')
    assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize('value', ['1.0.0', '12.3.45', '2.0.0-rc.1',
                                   f'1.0.0@{DIGEST}', None, ''])
def test_exact_or_absent_release_version_passes(tmp_path, value):
    result = _run(str(_write_env(tmp_path / '.env',
                                 {**GOOD, 'LAB_PLAYGROUND_VERSION': value})))

    assert result.returncode == 0, result.stderr


# Catches a host preflight that rejects a valid cloud-only or two-cloud
# host because it still demands the Proxmox-only settings.
@pytest.mark.parametrize('values', [valid_aws_env(), valid_cloud_env()])
def test_valid_cloud_env_passes(tmp_path, values):
    result = _run(str(_write_env(tmp_path / '.env', values)))
    assert result.returncode == 0, result.stderr


# Catches a host preflight that passes although the enabled cloud cannot boot.
@pytest.mark.parametrize('key', [
    'LAB_SITE_ID', 'AWS_REGION', 'AWS_AVAILABILITY_ZONE', 'AWS_LAB_VPC_ID',
    'AWS_ROUTER_INSTANCE_ID', 'AWS_MAX_VMS'])
def test_aws_preflight_names_each_missing_required_key(tmp_path, key):
    values = valid_aws_env()
    values.pop(key)
    result = _run(str(_write_env(tmp_path / '.env', values)))
    assert result.returncode == 1
    problems = result.stderr.splitlines()
    assert len(problems) == 1
    assert problems[0].startswith(f'{key}:')


# Catches the GCP half of the same gap.
@pytest.mark.parametrize('key', list(GCP_ENV))
def test_gcp_preflight_names_each_missing_required_key(tmp_path, key):
    values = valid_cloud_env()
    values.pop(key)
    result = _run(str(_write_env(tmp_path / '.env', values)))
    assert result.returncode == 1
    problems = result.stderr.splitlines()
    assert len(problems) == 1
    assert problems[0].startswith(f'{key}:')


# Catches the host accepting a value the app refuses, and echoing it.
@pytest.mark.parametrize('key, value', [
    ('ENABLED_BACKENDS', 'azure'),
    ('ENABLED_BACKENDS', ','),
    ('LAB_SITE_ID', 'Site-Upper'),
    ('LAB_SITE_ID', 'trailing-'),
    ('AWS_MAX_VMS', '0'),
    ('AWS_MAX_VMS', 'three'),
])
def test_invalid_cloud_value_fails_naming_only_its_key(tmp_path, key, value):
    result = _run(str(_write_env(tmp_path / '.env', {**valid_aws_env(), key: value})))
    assert result.returncode == 1
    assert result.stderr.splitlines()[0].startswith(f'{key}:')
    if len(value) > 1:
        assert value not in result.stdout + result.stderr


# Catches the host script and app accepting different CIDR safety boundaries.
def test_preflight_rejects_reserved_overlapping_and_undersized_cidrs(tmp_path):
    for cidr in ('10.20.0.0/16', '172.17.0.0/16', '172.20.0.0/24'):
        result = _run(str(_write_env(
            tmp_path / f'{cidr.replace("/", "_")}.env',
            {**valid_aws_env(), 'AWS_LAB_CIDR': cidr})))
        assert result.returncode == 1
        assert result.stderr.startswith('AWS_LAB_CIDR:')


# Catches the host accepting a tailnet-overlapping, malformed, IPv6 or
# host-bits base that the app refuses at startup, for either cloud.
@pytest.mark.parametrize('cidr', [
    '100.64.0.0/16', '100.0.0.0/8', 'not-a-cidr', '172.20.0.0/33', 'fd00:20::/48',
    # Host bits outside both clouds' bases, so nothing else can refuse it.
    '172.22.1.0/16',
], ids=['inside-cgnat', 'contains-cgnat', 'malformed', 'bad-prefix', 'ipv6', 'host-bits'])
@pytest.mark.parametrize('key', ['AWS_LAB_CIDR', 'GCP_LAB_CIDR'])
def test_preflight_rejects_cgnat_malformed_ipv6_and_host_bit_cidrs(tmp_path, key, cidr):
    result = _run(str(_write_env(tmp_path / '.env', {**valid_cloud_env(), key: cidr})))
    assert result.returncode == 1
    assert result.stderr.startswith(f'{key}:')


# Catches the two clouds being handed overlapping lab networks.
def test_preflight_rejects_overlapping_cloud_cidrs(tmp_path):
    result = _run(str(_write_env(tmp_path / '.env', {
        **valid_cloud_env(), 'GCP_LAB_CIDR': '172.20.128.0/17'})))
    assert result.returncode == 1
    assert result.stderr.startswith('AWS_LAB_CIDR:')


# Catches cloud reservations draining a Proxmox node named like a backend (M14).
def test_preflight_rejects_fixed_node_named_like_a_backend(tmp_path):
    result = _run(str(_write_env(tmp_path / '.env', {**GOOD, 'FIXED_NODE': 'GCP'})))
    assert result.returncode == 1
    assert result.stderr.startswith('FIXED_NODE:')


# Catches a cloud-only host failing on a Proxmox CA bundle it never uses.
def test_proxmox_ca_bundle_is_ignored_when_proxmox_is_disabled(tmp_path):
    values = {**valid_aws_env(), 'PROXMOX_CA_BUNDLE': str(tmp_path / 'absent.pem')}
    result = _run(str(_write_env(tmp_path / '.env', values)))
    assert result.returncode == 0, result.stderr
