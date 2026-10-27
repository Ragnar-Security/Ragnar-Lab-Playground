"""scripts/release_notes.py gates a release on its CHANGELOG section.

The release workflow publishes whatever this script prints as the GitHub
Release body, and refuses to release when it fails. Operators upgrading a
deployment read that body for the steps the release needs, so a section
without upgrade notes must stop the release rather than ship silently.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'scripts' / 'release_notes.py'

CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

### Added
- Something not yet released.

## [1.1.0] - 2026-11-02

### Changed
- Faster listing.

```bash
## not a heading: inside a code block
### Upgrade notes
```

### Upgrade notes

- Run `scripts/migrate_example.py` before restarting.

## [1.0.1] - 2026-10-30

### Fixed
- A bug.

### Upgrade notes

### Security
- Nothing to do above, so this release must be refused.

## [1.0.0] - 2026-10-27

### Upgrade notes

- First release.

## [0.9.0] - 2026-10-01

## [0.8.0] - 2026-09-01

### Added
- No upgrade notes in this one.

[Unreleased]: https://github.com/example/repo/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/example/repo/compare/v1.0.1...v1.1.0
[1.0.0]: https://github.com/example/repo/releases/tag/v1.0.0
"""


def _run(tmp_path, version, text=CHANGELOG):
    changelog = tmp_path / 'CHANGELOG.md'
    changelog.write_text(text)
    return subprocess.run(
        [sys.executable, '-I', '-S', str(SCRIPT), version,
         '--changelog', str(changelog)],
        capture_output=True, text=True, check=False)


def test_prints_the_section_body_without_its_heading(tmp_path):
    result = _run(tmp_path, '1.1.0')

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('### Changed\n- Faster listing.\n')
    assert result.stdout.rstrip().endswith(
        '- Run `scripts/migrate_example.py` before restarting.')
    assert '## [1.1.0]' not in result.stdout
    assert '[1.0.1]' not in result.stdout and 'A bug.' not in result.stdout


def test_last_section_excludes_link_reference_definitions(tmp_path):
    result = _run(tmp_path, '1.0.0')

    assert result.returncode == 0, result.stderr
    assert result.stdout == '### Upgrade notes\n\n- First release.\n'


def test_missing_section_fails(tmp_path):
    result = _run(tmp_path, '2.0.0')

    assert result.returncode == 1
    assert result.stdout == ''
    assert '2.0.0' in result.stderr


def test_version_must_match_exactly(tmp_path):
    """1.0 must not select 1.0.0, nor v1.0.0 the 1.0.0 section."""
    assert _run(tmp_path, '1.0').returncode == 1
    assert _run(tmp_path, 'v1.0.0').returncode == 1


def test_empty_section_fails(tmp_path):
    result = _run(tmp_path, '0.9.0')

    assert result.returncode == 1
    assert 'empty' in result.stderr


def test_section_without_upgrade_notes_fails(tmp_path):
    result = _run(tmp_path, '0.8.0')

    assert result.returncode == 1
    assert 'Upgrade notes' in result.stderr


def test_empty_upgrade_notes_fail(tmp_path):
    result = _run(tmp_path, '1.0.1')

    assert result.returncode == 1
    assert 'Upgrade notes' in result.stderr


def test_upgrade_notes_heading_inside_code_block_does_not_count(tmp_path):
    text = CHANGELOG.replace(
        '### Upgrade notes\n\n- Run `scripts/migrate_example.py` before '
        'restarting.\n', '')
    result = _run(tmp_path, '1.1.0', text)

    assert result.returncode == 1
    assert 'Upgrade notes' in result.stderr


def test_unreleased_is_not_a_release(tmp_path):
    assert _run(tmp_path, 'Unreleased').returncode == 1


def test_unreadable_changelog_fails(tmp_path):
    result = subprocess.run(
        [sys.executable, '-I', '-S', str(SCRIPT), '1.0.0',
         '--changelog', str(tmp_path / 'missing.md')],
        capture_output=True, text=True, check=False)

    assert result.returncode == 2
    assert result.stdout == ''


def test_repository_changelog_has_valid_notes_for_every_release():
    """Every released section in the real CHANGELOG passes the gate."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('release_notes', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    text = (REPO / 'CHANGELOG.md').read_text()
    versions = module.released_versions(text)
    assert versions
    for version in versions:
        result = subprocess.run(
            [sys.executable, '-I', '-S', str(SCRIPT), version],
            capture_output=True, text=True, check=False)
        assert result.returncode == 0, (version, result.stderr)
