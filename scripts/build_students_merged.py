"""
Build data/students_merged.csv from a Canvas gradebook export and the headscale
key file.

This is the file that scripts/init_db.py seeds the student whitelist from. The
seed runs on EVERY container start, not only the first one: import_student_whitelist()
re-imports whenever the whitelist holds no rows beyond the test student. A purge
of the database therefore does NOT remove last term's students by itself. The
next container start puts them back from this CSV. Replace this file during a
term changeover, or the purge undoes itself.

Inputs:
  CANVAS   a Canvas gradebook export. Needs 'SIS User ID' and 'SIS Login ID'.
  KEYS     the headscale key file that scripts/headscale_term_changeover.sh
           writes: login,email,preauth_key with no header row.

Output columns are those that init_db.py reads:
  SIS User ID,SIS Login ID,email,headscale_key

The script refuses to write a partial file. Every student on the roster must
have a key, and every key must belong to a student on the roster. A student
with no key cannot join the VPN, and the failure appears only when that one
student tries to connect, which is the worst time to find it.

Canvas puts synthetic rows in the export. A row is synthetic when it has no
SIS User ID. The real example is "Student, Test", whose login is a 40-character
hex digest. Such rows are dropped, and the script says how many it dropped.

Usage:
    python scripts/build_students_merged.py (--site-env PATH | --bands SPEC) CANVAS KEYS OUT_CSV

The roster may hold at most as many students as the entry band of the VLAN
layout has tags. Name the layout explicitly: --site-env reads VLAN_BANDS
from that file (scripts/site.env; no VLAN_BANDS line means the default
bands), --bands gives the string itself (empty means the default bands).
"""

import argparse
import csv
import importlib.util
import re
import sys
from pathlib import Path

FIELDNAMES = ['SIS User ID', 'SIS Login ID', 'email', 'headscale_key']
SCRIPTS = Path(__file__).resolve().parent


def _load(name, path):
    # By path, not `import app...`: this script stays standard-library only.
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def roster_max(bands):
    """How many students the portal can number under the VLAN layout `bands`
    (a VLAN_BANDS string; empty means the default): one VLAN pair each, so
    the size of its entry band."""
    spec = (bands or '').strip()
    if not spec:
        spec = _load('_jumpbox_netgen', SCRIPTS / 'jumpbox_netgen.py').DEFAULT_BANDS
    slots = _load('_lab_network_slots', SCRIPTS.parent / 'app' / 'backends' / 'network_slots.py')
    try:
        return slots.parse_vlan_layout(spec).roster_max
    except ValueError as error:
        sys.exit(f"Error: VLAN_BANDS {error}")


def site_env_value(path, name):
    """The value the site.env file at `path` gives `name` ('' when unset)."""
    pattern = re.compile(r'\s*(?:export\s+)?' + re.escape(name) + r'=(.*)')
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        sys.exit(f"Error: cannot read the site settings {path}: {error.strerror}")
    value = ''
    for line in lines:
        match = pattern.fullmatch(line)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
    return value


def bands_from_site_env(path):
    """VLAN_BANDS as the site.env file at `path` sets it ('' when it has no
    VLAN_BANDS line, which means the default bands)."""
    return site_env_value(path, 'VLAN_BANDS')


def read_canvas(path, bands):
    """Return [(uid, login)] for the real students in a Canvas export, refusing
    more than the VLAN layout `bands` can number (see roster_max)."""
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        have = set(reader.fieldnames or [])
        need = {'SIS User ID', 'SIS Login ID'}
        if not need <= have:
            sys.exit(f"Error: {path} has no {sorted(need - have)} column")

        students, dropped = [], []
        for row in reader:
            uid = (row.get('SIS User ID') or '').strip()
            login = (row.get('SIS Login ID') or '').strip().lower()
            if not uid or not login:
                if login or uid:
                    dropped.append(login or uid)
                continue
            students.append((uid, login))

    if dropped:
        print(f"Dropped {len(dropped)} row(s) with no SIS User ID: {', '.join(dropped)}")
    cap = roster_max(bands)
    if len(students) > cap:
        sys.exit(f"Error: {path} has {len(students)} students; the portal numbers "
                 f"at most {cap} (one VLAN pair each, from the entry band of VLAN_BANDS)")
    return students


def read_keys(path):
    """Return {login: (email, key)} from the headscale key file."""
    keys = {}
    with open(path, 'r', encoding='utf-8', newline='') as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
            if len(parts) != 3:
                sys.exit(f"Error: {path} line {number} has {len(parts)} fields, not 3")
            login, email, key = (part.strip() for part in parts)
            if not key:
                sys.exit(f"Error: {path} line {number} ({login}) has an empty key")
            keys[login.lower()] = (email, key)
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    layout = parser.add_mutually_exclusive_group(required=True)
    layout.add_argument('--site-env', help='site.env whose VLAN_BANDS sets the roster cap')
    layout.add_argument('--bands', help='the VLAN_BANDS string (empty: the default bands)')
    parser.add_argument('canvas', help='Canvas gradebook export')
    parser.add_argument('keys', help='headscale key file: login,email,key')
    parser.add_argument('out_csv', help='where to write students_merged.csv')
    args = parser.parse_args()

    bands = args.bands if args.bands is not None else bands_from_site_env(args.site_env)
    students = read_canvas(args.canvas, bands)
    if not students:
        sys.exit(f"Error: {args.canvas} holds no student with a SIS User ID")
    keys = read_keys(args.keys)
    print(f"Roster: {len(students)} students. Keys: {len(keys)}.")

    logins = {login for _, login in students}
    no_key = sorted(logins - set(keys))
    no_student = sorted(set(keys) - logins)
    if no_key:
        sys.exit(f"Error: {len(no_key)} student(s) have no headscale key. "
                 "No CSV was written.\n  " + "\n  ".join(no_key))
    if no_student:
        sys.exit(f"Error: {len(no_student)} key(s) belong to nobody on the roster. "
                 "The key file is from another term. No CSV was written.\n  "
                 + "\n  ".join(no_student))

    with open(args.out_csv, 'w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDNAMES)
        for uid, login in students:
            email, key = keys[login]
            writer.writerow([uid, login, email, key])

    print(f"{len(students)} rows written to {args.out_csv}.")


if __name__ == '__main__':
    main()
