"""
Store rotated Headscale pre-auth keys in the portal database.

Run inside the app container; scripts/rotate_headscale_keys.sh does that over
`docker compose exec -T`:

    python scripts/update_headscale_keys.py < keys.csv

stdin holds `login,key` rows with no header. Keys arrive on stdin, never as
arguments, so they do not show up in `ps` or shell history.

Two columns hold a student's key. `student_whitelist.headscale_key` seeds the
account at registration; `users.headscale_key` is what a registered student
sees on the dashboard. Updating only one leaves the student looking at a key
that is about to be expired, so both change in a single transaction or not at
all. The CSV alone is not enough: init_db.py imports it only while the
whitelist is empty.

A login present in neither table is reported as unknown and left alone; the
rotation script keeps that student's old key alive because the portal could
not hand out the new one. Re-running with the same keys changes nothing.

The last stdout line is a JSON summary of logins (never keys):
    {"updated": [...], "unchanged": [...], "unknown": [...]}
"""

import csv
import json
import os
import sys

from sqlalchemy import bindparam, create_engine, text

TABLES = ('student_whitelist', 'users')


def update_keys(engine, keys):
    """Write {login: key} to both key columns in one transaction.

    Returns sorted login lists under 'updated', 'unchanged' and 'unknown'.
    """
    logins = sorted(keys)
    updated, unchanged, unknown = [], [], []
    with engine.begin() as conn:
        current = {}
        for table in TABLES:
            rows = conn.execute(
                text(f"SELECT login_id, headscale_key FROM {table} WHERE login_id IN :logins")
                .bindparams(bindparam('logins', expanding=True)),
                {'logins': logins},
            ).all()
            current[table] = dict(rows)

        for login in logins:
            key = keys[login]
            present = [table for table in TABLES if login in current[table]]
            if not present:
                unknown.append(login)
                continue
            stale = [table for table in present if current[table][login] != key]
            for table in stale:
                conn.execute(
                    text(f"UPDATE {table} SET headscale_key = :key WHERE login_id = :login"),
                    {'key': key, 'login': login},
                )
            (updated if stale else unchanged).append(login)
    return {'updated': updated, 'unchanged': unchanged, 'unknown': unknown}


def read_keys(stream):
    """Parse login,key rows; reject anything that could blank or mix up a key."""
    keys = {}
    for number, row in enumerate(csv.reader(stream), 1):
        if not row:
            continue
        if len(row) != 2:
            raise ValueError(f"line {number}: expected login,key")
        login, key = row[0].strip().lower(), row[1].strip()
        if not login or not key:
            raise ValueError(f"line {number}: empty login or key")
        if login in keys:
            raise ValueError(f"line {number}: {login} appears twice")
        keys[login] = key
    if not keys:
        raise ValueError("no keys on stdin")
    return keys


def main():
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit("Error: DATABASE_URL is not set")
    try:
        keys = read_keys(sys.stdin)
    except ValueError as error:
        sys.exit(f"Error: {error}. Nothing was written.")

    engine = create_engine(url)
    try:
        result = update_keys(engine, keys)
    finally:
        engine.dispose()

    print(f"Updated {len(result['updated'])}, unchanged {len(result['unchanged'])}, "
          f"unknown {len(result['unknown'])}.", file=sys.stderr)
    if result['unknown']:
        print("Not in the portal database: " + ", ".join(result['unknown']), file=sys.stderr)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
