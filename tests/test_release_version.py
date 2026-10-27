"""One version, recorded in several places that must agree.

The release workflow tags the image and the GitHub Release from the git tag,
and refuses a tag that differs from app.__version__ / pyproject.toml. If the
copies drift, an image labelled 1.2.0 reports another version in
/auth/health, and the release notes describe a different release.
"""

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import app

REPO = Path(__file__).resolve().parent.parent
RELEASE_VERSION = REPO / 'scripts' / 'release_version.py'


def _load_release_notes():
    spec = importlib.util.spec_from_file_location(
        'release_notes', REPO / 'scripts' / 'release_notes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pyproject_version_matches_package():
    pyproject = tomllib.loads((REPO / 'pyproject.toml').read_text())
    assert pyproject['project']['version'] == app.__version__


def test_uv_lock_records_the_same_project_version():
    lock = tomllib.loads((REPO / 'uv.lock').read_text())
    project = next(p for p in lock['package'] if p['name'] == 'automation')
    assert project['version'] == app.__version__


def test_top_released_changelog_section_is_the_package_version():
    release_notes = _load_release_notes()
    versions = release_notes.released_versions(
        (REPO / 'CHANGELOG.md').read_text())
    assert versions, 'CHANGELOG.md has no released section'
    assert versions[0] == app.__version__


def test_version_is_semver():
    assert re.fullmatch(r'\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?', app.__version__)


def _run_release_version(*args):
    return subprocess.run(
        [sys.executable, '-I', '-S', str(RELEASE_VERSION), *args],
        capture_output=True, text=True, check=False)


def test_release_version_script_prints_the_agreed_version():
    result = _run_release_version()
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'{app.__version__}\n'


def _fake_repo(root, pyproject_version, package_version):
    (root / 'app').mkdir()
    (root / 'pyproject.toml').write_text(
        f'[project]\nname = "automation"\nversion = "{pyproject_version}"\n')
    (root / 'app' / '__init__.py').write_text(
        f'import os\n\n__version__ = "{package_version}"\n')
    return root


def test_release_version_script_refuses_disagreeing_versions(tmp_path):
    result = _run_release_version(str(_fake_repo(tmp_path, '1.0.0', '1.0.1')))
    assert result.returncode == 1
    assert result.stdout == ''
    assert '1.0.0' in result.stderr and '1.0.1' in result.stderr


def test_release_version_script_reads_the_given_checkout(tmp_path):
    result = _run_release_version(str(_fake_repo(tmp_path, '2.3.4', '2.3.4')))
    assert result.returncode == 0, result.stderr
    assert result.stdout == '2.3.4\n'


# `aliases <version> [tag ...]` decides which moving image tags a release
# may take. Runs can finish in any order, so the answer comes from the
# ruleset-protected git tags: a release takes `latest` only as the highest
# stable version overall, and `X.Y` only as the highest stable in X.Y.

def _aliases(version, *tags, cwd=None):
    return subprocess.run(
        [sys.executable, '-I', '-S', str(RELEASE_VERSION), 'aliases', version,
         *tags], capture_output=True, text=True, check=False, cwd=cwd)


def test_highest_release_takes_both_aliases():
    result = _aliases('1.2.4', 'v1.0.0', 'v1.2.3', 'v1.2.4')
    assert result.returncode == 0, result.stderr
    assert result.stdout == '1.2\nlatest\n'


def test_older_patch_released_after_newer_takes_no_alias():
    result = _aliases('1.2.3', 'v1.2.3', 'v1.2.4')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


def test_patch_of_older_minor_takes_only_its_minor_alias():
    result = _aliases('1.1.5', 'v1.1.4', 'v1.1.5', 'v1.2.0')
    assert result.returncode == 0, result.stderr
    assert result.stdout == '1.1\n'


def test_prerelease_takes_no_alias():
    result = _aliases('2.0.0-rc.1', 'v1.2.4', 'v2.0.0-rc.1')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


def test_prerelease_tags_do_not_outrank_stable_releases():
    result = _aliases('1.2.4', 'v1.2.4', 'v1.3.0-rc.1', 'v1.2.5-rc.1')
    assert result.stdout == '1.2\nlatest\n'


def test_versions_compare_numerically():
    assert _aliases('1.10.0', 'v1.9.0', 'v1.10.0').stdout == '1.10\nlatest\n'
    assert _aliases('1.9.1', 'v1.9.1', 'v1.10.0').stdout == '1.9\n'


def test_tags_that_are_not_versions_are_ignored():
    result = _aliases('1.0.0', 'v1.0.0', 'v9', 'v9.9', 'v9.9.9.9', 'vnext',
                      'release-9.9.9', 'v9.9.9x')
    assert result.stdout == '1.0\nlatest\n'


def test_the_version_counts_even_before_its_tag_exists(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    assert _aliases('1.0.0', cwd=tmp_path).stdout == '1.0\nlatest\n'


@pytest.mark.parametrize('version', ['v1.0.0', '1.0', 'latest', '1.0.0+build'])
def test_aliases_rejects_a_malformed_version(version):
    result = _aliases(version, 'v1.0.0')
    assert result.returncode == 2
    assert result.stdout == ''


def test_aliases_reads_tags_from_the_current_repository(tmp_path):
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.edu',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.edu'}

    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                       capture_output=True, env=env)

    git('init', '-q')
    git('commit', '-q', '--allow-empty', '-m', 'one')
    git('tag', '-a', 'v1.0.0', '-m', 'v1.0.0')
    git('tag', 'v1.1.0')
    git('tag', 'unrelated')

    assert _aliases('1.0.0', cwd=tmp_path).stdout == '1.0\n'
    assert _aliases('1.1.0', cwd=tmp_path).stdout == '1.1\nlatest\n'
