#!/usr/bin/env python3
"""Release version helpers for the release workflow.

    python3 scripts/release_version.py [checkout-dir]    # default: this repo
    python3 scripts/release_version.py aliases <version> [<tag> ...]

Without a subcommand: print the version after checking both copies agree.
It lives in pyproject.toml (`[project] version`) and in app/__init__.py
(`__version__`, which /auth/health reports). Exits 1 naming both values
when they differ. app/__init__.py is parsed, not imported, so the workflow
needs none of the app's dependencies.

`aliases`: print, one per line, the moving image tags the release of
<version> may take: `X.Y` when it is the highest stable version of X.Y, and
`latest` when it is the highest stable version of all. A prerelease takes
neither. The other versions are the given tags, or else the `v*` tags of the
git repository in the current directory; those are protected by a
ruleset, so the answer does not depend on which release run finishes last.
Tags that are not versions are ignored. Exits 2 for a malformed <version>.

Standard library only.
"""

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z][0-9A-Za-z.-]*)?')


def package_version(root):
    tree = ast.parse((root / 'app' / '__init__.py').read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '__version__'
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise ValueError('app/__init__.py does not assign __version__ a literal')


def pyproject_version(root):
    with open(root / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)['project']['version']


def _parse(version):
    """(major, minor, patch, is_prerelease) or None."""
    match = VERSION.fullmatch(version)
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre is not None


def aliases(version, tags):
    """The moving tags `version` may take, given the released tags."""
    parsed = _parse(version)
    if parsed is None or parsed[3]:
        return []
    stable = {parsed[:3]}
    for tag in tags:
        other = _parse(tag[1:]) if tag.startswith('v') else None
        if other and not other[3]:
            stable.add(other[:3])
    major, minor, _ = parsed[:3]
    found = []
    if parsed[:3] == max(v for v in stable if v[:2] == (major, minor)):
        found.append(f'{major}.{minor}')
    if parsed[:3] == max(stable):
        found.append('latest')
    return found


def _git_tags():
    return subprocess.run(['git', 'tag', '--list', 'v*'], check=True,
                          capture_output=True, text=True).stdout.split()


def _check_versions(argv):
    root = Path(argv[1]) if len(argv) > 1 else REPO
    package, project = package_version(root), pyproject_version(root)
    if package != project:
        print(f'version mismatch: app/__init__.py __version__ = {package}, '
              f'pyproject.toml version = {project}', file=sys.stderr)
        return 1
    print(package)
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == 'aliases':
        if len(argv) < 3 or _parse(argv[2]) is None:
            print('usage: release_version.py aliases <X.Y.Z[-pre]> [<tag> ...]',
                  file=sys.stderr)
            return 2
        for alias in aliases(argv[2], argv[3:] or _git_tags()):
            print(alias)
        return 0
    return _check_versions(argv)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
