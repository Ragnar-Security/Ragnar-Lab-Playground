#!/usr/bin/env python3
"""Print the release version, after checking both copies of it agree.

    python3 scripts/release_version.py [checkout-dir]    # default: this repo

The version lives in pyproject.toml (`[project] version`) and in
app/__init__.py (`__version__`, which /auth/health reports). Prints it and
exits 0 when they agree; exits 1 naming both values when they differ.
Standard library only, and app/__init__.py is parsed, not imported, so the
release workflow needs none of the app's dependencies to run it.
"""

import ast
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else REPO
    package, project = package_version(root), pyproject_version(root)
    if package != project:
        print(f'version mismatch: app/__init__.py __version__ = {package}, '
              f'pyproject.toml version = {project}', file=sys.stderr)
        return 1
    print(package)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
