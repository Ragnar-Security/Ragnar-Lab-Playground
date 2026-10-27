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
}


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
