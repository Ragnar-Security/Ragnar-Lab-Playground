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
import re
import shutil
import subprocess
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
