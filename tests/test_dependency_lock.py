"""The images must install exactly what uv.lock pins.

Docker used to `pip install -r requirements.txt`, a hand-edited file with
range pins (`gevent>=23.9,<26`, `apscheduler>=3.10`) and no transitive pins.
Two builds of one commit could therefore resolve different package sets, and
a rebuild mid-semester could pick up an unreviewed transitive release.

`requirements.txt` is now generated from `uv.lock` (with hashes), so that
`pip install -r requirements.txt` in the image is a lock install. These tests
fail when the file drifts from the lock, or when an image stops installing
it with hash enforcement.
"""
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ['uv', 'export', '--frozen', '--no-dev', '--no-emit-project']


@pytest.mark.skipif(shutil.which('uv') is None, reason='uv not installed')
def test_requirements_txt_is_the_export_of_uv_lock():
    exported = subprocess.run(EXPORT, cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout
    # Drop the "# via: uv export ... -o requirements.txt" banner: it differs
    # between the command line used to write the file and this one.
    def body(text):
        return [l for l in text.splitlines() if not l.startswith('#')]
    committed = (ROOT / 'requirements.txt').read_text()
    assert body(committed) == body(exported), (
        'requirements.txt drifted from uv.lock; regenerate with: '
        'uv export --frozen --no-dev --no-emit-project -o requirements.txt')


def test_requirements_txt_pins_every_package_exactly_with_hashes():
    text = (ROOT / 'requirements.txt').read_text()
    # Join backslash continuations, then every requirement line must be an
    # exact pin carrying at least one hash.
    entries = [e for e in text.replace('\\\n', ' ').splitlines()
               if e.strip() and not e.lstrip().startswith('#')]
    assert entries
    for entry in entries:
        assert re.match(r'^[A-Za-z0-9._-]+==\S+', entry), entry
        assert '--hash=sha256:' in entry, entry


@pytest.mark.parametrize('name', ['Dockerfile', 'Dockerfile.prod'])
def test_dockerfile_installs_the_locked_requirements_with_hashes(name):
    installs = [l for l in (ROOT / name).read_text().splitlines()
                if 'pip install' in l]
    assert installs, f'{name} has no pip install'
    for line in installs:
        assert '--require-hashes' in line and 'requirements.txt' in line, (
            f'{name}: {line.strip()!r} does not enforce the lock')


def _requirement_name(value):
    return re.split(r'[<>=!~;\[]', value, maxsplit=1)[0].strip().lower()


def test_cloud_sdks_are_runtime_and_moto_ec2_is_dev_only():
    # Catches shipping a cloud backend whose SDK is absent from the image, or shipping moto in production.
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    runtime = {_requirement_name(v) for v in project['project']['dependencies']}
    dev = project['dependency-groups']['dev']
    assert {'boto3', 'google-auth'} <= runtime
    assert any(_requirement_name(v) == 'moto' and '[ec2]' in v.lower()
               for v in dev)
    assert 'moto' not in runtime
    # Catches a bare constraint letting an unrelated `uv lock` upgrade a cloud SDK (M10).
    pinned = {_requirement_name(v): v for v in project['project']['dependencies']
              if _requirement_name(v) in {'boto3', 'google-auth'}}
    assert all(re.search(r'==\d', v) for v in pinned.values()), pinned
    assert all(re.search(r'==\d', v) for v in dev if _requirement_name(v) == 'moto')

    exported = subprocess.run(EXPORT, cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.lower()
    assert re.search(r'^boto3==', exported, re.MULTILINE)
    assert re.search(r'^google-auth==', exported, re.MULTILINE)
    assert not re.search(r'^moto==', exported, re.MULTILINE)


def test_boto3_imports_after_gevent_monkey_patch():
    # Catches an SDK/urllib3 update that is incompatible with the process's mandatory patch order.
    program = '''
from gevent import monkey
monkey.patch_all()
assert monkey.is_module_patched("socket")
import boto3
import botocore.httpsession
assert botocore.httpsession.URLLib3Session.__module__ == "botocore.httpsession"
boto3.client(
    "ec2",
    region_name="us-east-1",
    aws_access_key_id="testing",
    aws_secret_access_key="testing",
    endpoint_url="http://127.0.0.1:9",
)
'''
    env = dict(os.environ, AWS_EC2_METADATA_DISABLED='true')
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'MonkeyPatchWarning' not in result.stderr
