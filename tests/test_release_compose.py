"""docker-compose.release.yml runs a published image instead of building.

Production builds from docker-compose.prod.yml alone and must keep doing so.
The override must only swap the build for the GHCR image of the version the
operator names: every other setting (environment, mounts, ulimits, the
database and Caddy) has to stay exactly as the production file defines it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE = 'ghcr.io/ragnar-security/lab-playground'

DUMMY_ENV = {
    'MYSQL_USER': 'vmuser',
    'MYSQL_PASSWORD': 'dummy-password',
    'MYSQL_DATABASE': 'vmdatabase',
    'MYSQL_ROOT_PASSWORD': 'dummy-root-password',
    'SECRET_KEY': 'dummy-secret',
    'SITE_DOMAIN': 'lab.example.edu',
    'ACME_EMAIL': 'admin@example.edu',
}


def _compose_available():
    if shutil.which('docker') is None:
        return False
    probe = subprocess.run(['docker', 'compose', 'version'],
                           capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _compose_available(),
                                reason='docker compose not available')


def _config(tmp_path, files, **env):
    env_file = tmp_path / 'dummy.env'
    env_file.write_text(''.join(
        f'{key}={value}\n' for key, value in {**DUMMY_ENV, **env}.items()))
    # Only what docker needs to find its CLI plugins and daemon config: a
    # variable such as LAB_PLAYGROUND_VERSION in the caller's shell would
    # otherwise override the env file.
    clean_env = {key: os.environ[key] for key in
                 ('PATH', 'HOME', 'DOCKER_CONFIG', 'DOCKER_HOST')
                 if key in os.environ}
    args = ['docker', 'compose', '--project-directory', str(REPO),
            '--env-file', str(env_file)]
    for name in files:
        args += ['-f', str(REPO / name)]
    return subprocess.run(args + ['config', '--format', 'json'],
                          capture_output=True, text=True, check=False,
                          env=clean_env)


def _services(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)['services']


def test_release_override_runs_the_named_ghcr_image(tmp_path):
    services = _services(_config(
        tmp_path, ['docker-compose.prod.yml', 'docker-compose.release.yml'],
        LAB_PLAYGROUND_VERSION='1.0.0'))

    app = services['vm-automation']
    assert app['image'] == f'{IMAGE}:1.0.0'
    assert 'build' not in app


def test_release_override_changes_nothing_but_build_and_image(tmp_path):
    prod = _services(_config(tmp_path, ['docker-compose.prod.yml']))
    release = _services(_config(
        tmp_path, ['docker-compose.prod.yml', 'docker-compose.release.yml'],
        LAB_PLAYGROUND_VERSION='1.0.0'))

    assert set(release) == set(prod)
    for name in prod:
        if name == 'vm-automation':
            continue
        assert release[name] == prod[name], name
    expected = {key: value for key, value in prod['vm-automation'].items()
                if key not in ('build', 'image')}
    actual = {key: value for key, value in release['vm-automation'].items()
              if key != 'image'}
    assert actual == expected


def test_production_file_alone_still_builds_from_source(tmp_path):
    app = _services(_config(tmp_path, ['docker-compose.prod.yml']))['vm-automation']

    assert app['build']['dockerfile'] == 'Dockerfile.prod'
    assert not app.get('image', '').startswith(IMAGE)


def test_release_override_requires_a_version(tmp_path):
    result = _config(
        tmp_path, ['docker-compose.prod.yml', 'docker-compose.release.yml'])

    assert result.returncode != 0
    assert 'LAB_PLAYGROUND_VERSION' in result.stderr
