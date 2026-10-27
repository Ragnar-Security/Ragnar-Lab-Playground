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
    python scripts/build_students_merged.py CANVAS KEYS OUT_CSV
"""

import argparse
import csv
import sys

FIELDNAMES = ['SIS User ID', 'SIS Login ID', 'email', 'headscale_key']


def read_canvas(path):
    """Return [(uid, login)] for the real students in a Canvas export."""
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
    parser.add_argument('canvas', help='Canvas gradebook export')
    parser.add_argument('keys', help='headscale key file: login,email,key')
    parser.add_argument('out_csv', help='where to write students_merged.csv')
    args = parser.parse_args()

    students = read_canvas(args.canvas)
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
