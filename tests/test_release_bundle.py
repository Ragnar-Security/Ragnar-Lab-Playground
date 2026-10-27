"""scripts/build_release_bundle.py builds the deploy tarball a release ships.

The bundle is published on a public GitHub Release. An operator's checkout
holds untracked files with credentials and student records (.env,
templates.yaml, data/, scripts/site.env, config/headscale-acl.json), so the
bundle must be built from committed content only, whatever the working tree
holds. It must also be byte-reproducible, so anyone can rebuild a release's
tarball from its tag and compare it with the published SHA256SUMS.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERSION = '1.0.0'
TARBALL = f'lab-playground-{VERSION}-deploy.tar.gz'
PREFIX = f'lab-playground-{VERSION}/'
MARKER = 'PLANTED-OPERATOR-SECRET-7f3c'

# Untracked operator files (CLAUDE.md rule 13) plus untracked files that
# match the bundle's own globs, and must still stay out.
PLANTED = {
    '.env': f'SECRET_KEY={MARKER}\n',
    'templates.yaml': f'# {MARKER}\n',
    'data/students_merged.csv': f'login,key\nstudent,{MARKER}\n',
    'scripts/site.env': f'PORTAL_HOST={MARKER}\n',
    'config/headscale-acl.json': f'{{"secret": "{MARKER}"}}\n',
    'scripts/migrate_planted.py': f'# {MARKER}\n',
    'config/site.example.local.json': f'{{"secret": "{MARKER}"}}\n',
}

GIT_ENV = {
    'GIT_AUTHOR_NAME': 'Release Test', 'GIT_AUTHOR_EMAIL': 'test@example.edu',
    'GIT_COMMITTER_NAME': 'Release Test',
    'GIT_COMMITTER_EMAIL': 'test@example.edu',
    'GIT_AUTHOR_DATE': '2026-10-27T12:00:00+00:00',
    'GIT_COMMITTER_DATE': '2026-10-27T12:00:00+00:00',
}

pytestmark = pytest.mark.skipif(shutil.which('git') is None,
                                reason='git not installed')


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


@pytest.fixture(scope='module')
def clone(tmp_path_factory):
    """A clone of this repository whose HEAD is the current working tree.

    Overlaying the working tree's tracked and unignored files lets the test
    cover uncommitted work, while the builder still sees an ordinary
    checkout. Operator files are planted only afterwards, uncommitted.
    """
    root = tmp_path_factory.mktemp('bundle') / 'clone'
    subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(REPO),
                    str(root)], check=True)
    listed = _git(REPO, 'ls-files', '-z', '--cached', '--others',
                  '--exclude-standard').split('\0')
    for rel in filter(None, listed):
        source = REPO / rel
        if source.is_file():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    _git(root, 'add', '-A')
    if _git(root, 'status', '--porcelain'):
        _git(root, 'commit', '-q', '--no-verify', '-m', 'working tree')
    for rel, content in PLANTED.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # A tracked file edited but not committed: the bundle carries the commit.
    with open(root / 'Caddyfile', 'a') as handle:
        handle.write(f'# {MARKER}\n')
    return root


def _build(clone_root, outdir, *args):
    return subprocess.run(
        [sys.executable, '-I', '-S',
         str(clone_root / 'scripts' / 'build_release_bundle.py'),
         VERSION, str(outdir), *args],
        capture_output=True, text=True, check=False, cwd=clone_root)


@pytest.fixture(scope='module')
def bundle(clone, tmp_path_factory):
    outdir = tmp_path_factory.mktemp('out')
    result = _build(clone, outdir)
    assert result.returncode == 0, result.stderr
    return outdir / TARBALL


def _members(path):
    with tarfile.open(path) as tar:
        return {m.name: m for m in tar.getmembers()}


def _read(path, name):
    with tarfile.open(path) as tar:
        return tar.extractfile(name).read()


def test_bundle_holds_the_deploy_files(clone, bundle):
    names = set(_members(bundle))
    migrations = [m for m in _git(clone, 'ls-files', 'scripts/').split()
                  if Path(m).name.startswith('migrate_') and m.endswith('.py')]
    expected = {
        'docker-compose.prod.yml', 'docker-compose.release.yml', 'Caddyfile',
        '.env.example', 'templates.example.yaml', 'scripts/preflight_env.py',
        'app/production_policy.py', 'scripts/backup-database.sh',
        'scripts/systemd/vm-automation-backup.service',
        'scripts/systemd/vm-automation-backup.timer',
        'scripts/systemd/vm-automation-backup.env.example',
        'config/headscale-acl.example.json', 'LICENSE', 'NOTICE',
        'CHANGELOG.md', 'DEPLOY-FROM-RELEASE.md', *migrations,
    }
    assert len(migrations) >= 6
    assert names == {PREFIX + name for name in expected}


def test_untracked_operator_files_never_enter_the_bundle(bundle):
    names = set(_members(bundle))
    for rel in PLANTED:
        assert PREFIX + rel not in names
    with tarfile.open(bundle) as tar:
        for member in tar.getmembers():
            assert MARKER.encode() not in tar.extractfile(member).read(), \
                member.name


def test_bundle_carries_committed_content_not_working_tree_edits(clone, bundle):
    committed = _git(clone, 'show', 'HEAD:Caddyfile').encode()
    assert _read(bundle, PREFIX + 'Caddyfile') == committed


def test_bundle_is_byte_reproducible(clone, bundle, tmp_path):
    # Different working-tree timestamps must not change the bytes.
    for path in (clone / 'scripts').iterdir():
        os.utime(path, (1, 1))
    result = _build(clone, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / TARBALL).read_bytes() == bundle.read_bytes()


def test_member_metadata_is_normalised(clone, bundle):
    commit_time = int(_git(clone, 'log', '-1', '--format=%ct', 'HEAD'))
    members = _members(bundle)
    names = list(members)

    assert names == sorted(names)
    for member in members.values():
        assert member.isfile()
        assert member.mtime == commit_time
        assert (member.uid, member.gid, member.uname, member.gname) == \
            (0, 0, '', '')
    assert members[PREFIX + 'scripts/backup-database.sh'].mode == 0o755
    assert members[PREFIX + 'CHANGELOG.md'].mode == 0o644


def test_sha256sums_lists_the_tarball(bundle):
    sums = (bundle.parent / 'SHA256SUMS').read_text()
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()

    assert sums == f'{digest}  {TARBALL}\n'


def test_extracted_preflight_runs_without_a_checkout(bundle, tmp_path):
    """preflight_env.py loads app/production_policy.py by path, so the
    bundle must carry it for the documented preflight step to work."""
    with tarfile.open(bundle) as tar:
        tar.extractall(tmp_path, filter='data')
    env = tmp_path / 'good.env'
    env.write_text(
        'MYSQL_DATABASE=vmdatabase\nMYSQL_USER=vmuser\n'
        'SECRET_KEY=' + 'a3f1' * 16 + '\n'
        'MYSQL_ROOT_PASSWORD=Rt-9vKq2xW7mPz4LsN8d\n'
        'MYSQL_PASSWORD=Us-4hJd8cQ1nYb6TeR3w\n'
        'ADMIN_PASSWORD=Ad-7gFs2kL9pXv5MuC1q\n'
        'SESSION_COOKIE_SAMESITE=Lax\nPROXMOX_VERIFY_SSL=true\n')
    result = subprocess.run(
        [sys.executable, '-I', '-S',
         str(tmp_path / PREFIX / 'scripts' / 'preflight_env.py'), str(env)],
        capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('version', ['1.0', 'v1.0.0', '../1.0.0', '1.0.0/x'])
def test_rejects_a_malformed_version(clone, tmp_path, version):
    result = subprocess.run(
        [sys.executable, '-I', '-S',
         str(clone / 'scripts' / 'build_release_bundle.py'), version,
         str(tmp_path)],
        capture_output=True, text=True, check=False, cwd=clone)

    assert result.returncode == 2
    assert list(tmp_path.iterdir()) == []
