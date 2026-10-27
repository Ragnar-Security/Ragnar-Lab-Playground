"""
Convert a roster file (students.yml) into the simplified CSV that
scripts/import_students.py reads.

students.yml holds the 9-digit university IDs ("SIS User ID") only. It does not
hold the login IDs ("SIS Login ID"). You MUST pass the Canvas gradebook export
named in the header of students.yml with --canvas.

This script writes NO CSV when any UID has no login ID. scripts/import_students.py
skips every row with an empty login ID, so such a CSV would import nobody.
The script prints each UID that is missing and where to get the login ID.
It prints a warning when the UID count differs from the count in the students.yml
header. It never imports anything and never opens the database.

Usage:
    python scripts/convert_canvas_roster.py STUDENTS_YML OUT_CSV --canvas EXPORT.csv
"""

import argparse
import csv
import re
import sys

UID_LINE = re.compile(r'^\s*-\s*"?(\d{9})"?\s*$')
CLAIMED_COUNT = re.compile(r'(\d+)\s+enrolled', re.IGNORECASE)


def read_uids(path):
    """Return the 9-digit UIDs from students.yml, in file order."""
    with open(path, 'r', encoding='utf-8') as handle:
        return [m.group(1) for m in (UID_LINE.match(line) for line in handle) if m]


def read_claimed_count(path):
    """Return the student count that the students.yml header claims, or None."""
    with open(path, 'r', encoding='utf-8') as handle:
        for line in handle:
            if not line.startswith('#'):
                break
            match = CLAIMED_COUNT.search(line)
            if match:
                return int(match.group(1))
    return None


def read_logins(path):
    """Return {SIS User ID: SIS Login ID} from a Canvas gradebook export."""
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'SIS User ID', 'SIS Login ID'} <= set(reader.fieldnames or []):
            sys.exit("Error: Canvas export needs 'SIS User ID' and 'SIS Login ID' columns")
        return {row['SIS User ID'].strip(): row['SIS Login ID'].strip().lower()
                for row in reader if row['SIS User ID'].strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('students_yml')
    parser.add_argument('out_csv')
    parser.add_argument('--canvas', help='Canvas gradebook export that holds the login IDs')
    args = parser.parse_args()

    uids = read_uids(args.students_yml)
    if not uids:
        sys.exit(f"Error: no 9-digit UIDs found in {args.students_yml}")
    claimed = read_claimed_count(args.students_yml)
    print(f"Found {len(uids)} UIDs in {args.students_yml}.")
    if claimed is not None and claimed != len(uids):
        print(f"WARNING: the header of {args.students_yml} claims {claimed} students. "
              f"The file lists {len(uids)}. Resolve this difference before you import.")

    if not args.canvas:
        sys.exit(
            "Error: students.yml has no login IDs, so no CSV was written.\n"
            "Pass the Canvas export with --canvas. The header of students.yml names it.\n"
            "Get it from the Canvas gradebook.")

    logins = read_logins(args.canvas)
    missing = [uid for uid in uids if not logins.get(uid)]
    if missing:
        sys.exit(
            f"Error: {len(missing)} of {len(uids)} UIDs have no login ID in {args.canvas}. "
            "No CSV was written.\nMissing UIDs:\n  " + "\n  ".join(missing))

    with open(args.out_csv, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['SIS User ID', 'SIS Login ID'])
        for uid in uids:
            writer.writerow([uid, logins[uid]])
    print(f"{len(uids)} rows written to {args.out_csv}.")


if __name__ == '__main__':
    main()
