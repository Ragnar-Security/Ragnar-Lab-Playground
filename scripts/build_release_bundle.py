#!/usr/bin/env python3
"""Build the deploy bundle a release ships, from committed files only.

    python3 scripts/build_release_bundle.py <version> <outdir> [--ref REF]

Writes <outdir>/lab-playground-<version>-deploy.tar.gz and its line in
<outdir>/SHA256SUMS. Every file comes from the git tree of REF (default
HEAD), never from the working tree: an operator checkout holds untracked
credentials and student records (.env, templates.yaml, data/,
scripts/site.env, config/headscale-acl.json) that the public bundle must
never carry, and uncommitted edits to tracked files stay out too.

The tarball is reproducible: members sorted by path, every mtime the
commit time of REF, owner root with no names, modes 0644 or 0755 from git,
and a gzip header without a name or timestamp. Rebuilding a tag gives the
published bytes. Standard library only.
"""

import argparse
import fnmatch
import gzip
import hashlib
import io
import posixpath
import re
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?')

# (path in the repository, path in the bundle). Exact paths must exist;
# glob patterns match tracked files in that one directory.
REQUIRED = [
    ('docker-compose.prod.yml', None),
    ('docker-compose.release.yml', None),
    ('Caddyfile', None),
    ('.env.example', None),
    ('templates.example.yaml', None),
    ('scripts/preflight_env.py', None),
    # preflight_env.py loads the production policy module by path.
    ('app/production_policy.py', None),
    ('scripts/backup-database.sh', None),
    ('LICENSE', None),
    ('NOTICE', None),
    ('CHANGELOG.md', None),
    ('docs/DEPLOY-FROM-RELEASE.md', 'DEPLOY-FROM-RELEASE.md'),
]
GLOBS = [
    'scripts/migrate_*.py',
    'scripts/systemd/vm-automation-backup.*',
    'config/*.example*',
]


def _git(*args):
    return subprocess.run(['git', '-C', str(REPO), *args], check=True,
                          capture_output=True).stdout


def tracked_blobs(ref):
    """{path: (mode, blob id)} for every file in the tree of ref."""
    blobs = {}
    for entry in _git('ls-tree', '-r', '-z', '--full-tree', ref).split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, blob = meta.split(b' ')
        if kind == b'blob':
            blobs[path.decode()] = (mode.decode(), blob.decode())
    return blobs


def select(blobs):
    """[(repository path, bundle path)] for the bundle, sorted by bundle path."""
    chosen = {}
    for path, target in REQUIRED:
        if path not in blobs:
            raise SystemExit(f'{path} is not tracked in this revision')
        chosen[target or path] = path
    for pattern in GLOBS:
        directory, name = posixpath.split(pattern)
        matches = [path for path in blobs
                   if posixpath.dirname(path) == directory
                   and fnmatch.fnmatchcase(posixpath.basename(path), name)]
        if not matches and not pattern.startswith('config/'):
            raise SystemExit(f'no tracked file matches {pattern}')
        chosen.update({path: path for path in matches})
    return sorted(((source, target) for target, source in chosen.items()),
                  key=lambda pair: pair[1])


def build(version, outdir, ref):
    commit = _git('rev-parse', '--verify', '--end-of-options',
                  f'{ref}^{{commit}}').decode().strip()
    blobs = tracked_blobs(commit)
    mtime = int(_git('log', '-1', '--format=%ct', commit).decode())
    prefix = f'lab-playground-{version}/'
    entries = select(blobs)

    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for source, target in entries:
            mode, blob = blobs[source]
            data = _git('cat-file', 'blob', blob)
            info = tarfile.TarInfo(prefix + target)
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o755 if mode == '100755' else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(data))

    compressed = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=compressed,
                       compresslevel=9, mtime=0) as gz:
        gz.write(tar_bytes.getvalue())

    outdir.mkdir(parents=True, exist_ok=True)
    name = f'lab-playground-{version}-deploy.tar.gz'
    (outdir / name).write_bytes(compressed.getvalue())

    sums_path = outdir / 'SHA256SUMS'
    lines = sums_path.read_text().splitlines() if sums_path.exists() else []
    lines = [line for line in lines if line.split('  ', 1)[-1] != name]
    lines.append(f'{hashlib.sha256(compressed.getvalue()).hexdigest()}  {name}')
    lines.sort(key=lambda line: line.split('  ', 1)[-1])
    sums_path.write_text('\n'.join(lines) + '\n')
    return outdir / name, [target for _, target in entries]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('version', help='release version, e.g. 1.2.0 (no "v")')
    parser.add_argument('outdir', type=Path)
    parser.add_argument('--ref', default='HEAD',
                        help='git revision to package (default HEAD)')
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        parser.error(f'version must look like 1.2.0 or 1.2.0-rc.1, '
                     f'not {args.version!r}')

    try:
        tarball, members = build(args.version, args.outdir, args.ref)
    except subprocess.CalledProcessError as exc:
        print(f'git {exc.cmd[3]} failed: {exc.stderr.decode().strip()}',
              file=sys.stderr)
        return 1
    print(tarball)
    for member in members:
        print(f'  {member}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
