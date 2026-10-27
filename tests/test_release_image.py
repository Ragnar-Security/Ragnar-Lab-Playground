"""Dockerfile.prod must carry the build's identity into the image.

/auth/health reports the revision from APP_REVISION, so the build argument
has to become a runtime variable. The OCI labels let `docker inspect` and
GHCR show which release and commit an image is, and link the package to the
public repository.
"""

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parent.parent / 'Dockerfile.prod').read_text()


def _instructions(keyword):
    # Join continuation lines so a LABEL spread over several lines is one entry.
    joined = re.sub(r'\\\n\s*', ' ', DOCKERFILE)
    return [line.split(None, 1)[1] for line in joined.splitlines()
            if line.split(None, 1)[:1] == [keyword]]


def test_app_revision_build_arg_becomes_runtime_env():
    assert 'APP_REVISION' in _instructions('ARG')
    assert any(re.fullmatch(r'APP_REVISION=\$\{?APP_REVISION\}?', env)
               for env in _instructions('ENV'))


def test_oci_labels_are_set():
    labels = ' '.join(_instructions('LABEL'))
    for key, value in {
        'org.opencontainers.image.version': r'"?\$\{?APP_VERSION\}?"?',
        'org.opencontainers.image.revision': r'"?\$\{?APP_REVISION\}?"?',
        'org.opencontainers.image.source':
            r'"?https://github\.com/Ragnar-Security/Ragnar-Lab-Playground"?',
        'org.opencontainers.image.licenses': r'"?Apache-2\.0"?',
    }.items():
        assert re.search(re.escape(key) + '=' + value + r'(\s|$)', labels), key


def test_release_helper_scripts_stay_out_of_the_runtime_image():
    """Dockerfile.prod copies scripts/ for the migrations; the release
    tooling runs only in the workflow and on maintainers' machines."""
    ignored = {line.strip() for line in
               (Path(__file__).resolve().parent.parent / '.dockerignore')
               .read_text().splitlines()}
    for script in ('build_release_bundle.py', 'release_notes.py',
                   'release_version.py'):
        assert f'scripts/{script}' in ignored, script
