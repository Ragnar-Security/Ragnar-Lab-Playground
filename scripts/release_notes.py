#!/usr/bin/env python3
"""Print one release's CHANGELOG.md section: the body of a GitHub Release.

    python3 scripts/release_notes.py <version> [--changelog PATH]

Prints the section under `## [<version>]` without that heading, and exits 0.
Exits 1 when the section is missing or empty, or has no non-empty
`### Upgrade notes` subsection: operators upgrading a deployment read the
release body for the steps the release needs, so the release workflow
refuses to publish without them. Exits 2 when the changelog cannot be read.
Headings inside fenced code blocks do not count. Standard library only.
"""

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELEASE_HEADING = re.compile(r'^## \[(?P<version>[^\]]+)\](?:\s+-\s+.*)?\s*$')
LINK_DEFINITION = re.compile(r'^\[[^\]]+\]:\s+\S')
UPGRADE_NOTES = '### Upgrade notes'


def _headings(lines):
    """Yield (index, line) for lines that are Markdown headings, skipping
    anything inside a fenced code block."""
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith('```'):
            fenced = not fenced
        elif not fenced and line.startswith('#'):
            yield index, line


def released_versions(text):
    """Versions with a release section, newest (topmost) first."""
    versions = []
    for _, line in _headings(text.splitlines()):
        match = RELEASE_HEADING.match(line)
        if match and match['version'] != 'Unreleased':
            versions.append(match['version'])
    return versions


def section_body(text, version):
    """The lines under `## [version]` up to the next level-2 heading, with
    trailing link reference definitions removed; None if there is none."""
    lines = text.splitlines()
    headings = list(_headings(lines))
    for position, (index, line) in enumerate(headings):
        match = RELEASE_HEADING.match(line)
        if not match or match['version'] != version or version == 'Unreleased':
            continue
        end = next((i for i, h in headings[position + 1:] if h.startswith('## ')),
                   len(lines))
        body = lines[index + 1:end]
        while body and (not body[-1].strip() or LINK_DEFINITION.match(body[-1])):
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        return body
    return None


def upgrade_notes(body):
    """The non-blank lines of the body's `### Upgrade notes` subsection."""
    headings = list(_headings(body))
    for position, (index, line) in enumerate(headings):
        if line.strip() != UPGRADE_NOTES:
            continue
        end = next((i for i, h in headings[position + 1:]
                    if h.startswith('## ') or h.startswith('### ')), len(body))
        return [line for line in body[index + 1:end] if line.strip()]
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('version', help='release version, e.g. 1.2.0 (no "v")')
    parser.add_argument('--changelog', type=Path, default=REPO / 'CHANGELOG.md')
    args = parser.parse_args(argv)

    try:
        text = args.changelog.read_text(encoding='utf-8')
    except OSError as exc:
        print(f'cannot read {args.changelog}: {exc.strerror}', file=sys.stderr)
        return 2

    body = section_body(text, args.version)
    if body is None:
        print(f'{args.changelog.name}: no "## [{args.version}]" section',
              file=sys.stderr)
        return 1
    if not body:
        print(f'{args.changelog.name}: the [{args.version}] section is empty',
              file=sys.stderr)
        return 1
    if not upgrade_notes(body):
        print(f'{args.changelog.name}: the [{args.version}] section needs a '
              f'non-empty "{UPGRADE_NOTES}" subsection', file=sys.stderr)
        return 1
    sys.stdout.write('\n'.join(body) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
