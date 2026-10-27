"""One version, recorded in several places that must agree.

The release workflow tags the image and the GitHub Release from the git tag,
and refuses a tag that differs from app.__version__ / pyproject.toml. If the
copies drift, an image labelled 1.2.0 reports another version in
/auth/health, and the release notes describe a different release.
"""

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

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
