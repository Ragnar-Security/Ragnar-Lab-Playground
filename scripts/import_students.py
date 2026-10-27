"""
Import students from Canvas CSV or students.csv into the whitelist table

Supports two CSV formats:
  1. Full Canvas CSV: columns include Student, ID, SIS User ID, SIS Login ID, Section, ...
  2. Simplified students.csv: columns are SIS User ID, SIS Login ID
"""

import csv
import sys
import os

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db, StudentWhitelist


def import_students_from_csv(csv_path):
    """
    Import students from Canvas CSV export or simplified students.csv

    Args:
        csv_path (str): Path to the CSV file
    """
    app = create_app()

    with app.app_context():
        # Ensure the table exists
        db.create_all()

        print(f"Importing students from {csv_path}...")

        imported_count = 0
        updated_count = 0
        skipped_count = 0

        with open(csv_path, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            headers = reader.fieldnames

            # Detect CSV format
            is_full_canvas = 'Student' in headers and 'Section' in headers
            is_simple = 'SIS User ID' in headers and 'SIS Login ID' in headers
            has_headscale = 'headscale_key' in headers

            if not is_simple:
                print("Error: CSV must contain 'SIS User ID' and 'SIS Login ID' columns")
                sys.exit(1)

            format_name = 'Full Canvas CSV' if is_full_canvas else 'Simplified students.csv'
            if has_headscale:
                format_name += ' (with headscale keys)'
            print(f"  Format detected: {format_name}")

            for row in reader:
                login_id = row.get('SIS Login ID', '').strip().lower()
                sis_user_id = row.get('SIS User ID', '').strip()

                # Full Canvas CSV has additional fields
                full_name = row.get('Student', '').strip() if is_full_canvas else None
                student_id = row.get('ID', '').strip() if is_full_canvas else None
                section = row.get('Section', '').strip() if is_full_canvas else None

                # Skip if no login ID or if it's a metadata row
                if not login_id:
                    continue
                if full_name and 'Points Possible' in full_name:
                    continue

                # Canvas adds a synthetic "Test Student" to every gradebook
                # export. It carries no SIS User ID and its SIS Login ID is a
                # 40-character hex digest, so neither guard above catches it
                # and it imports as a real student: a whitelist row, a user
                # account and a pair of VLAN tags nobody owns.
                if full_name and full_name.strip().lower() in ('student, test', 'test student'):
                    print(f"  Skipping Canvas Test Student")
                    continue
                if not sis_user_id and len(login_id) == 40 and all(c in '0123456789abcdef' for c in login_id):
                    print(f"  Skipping a row with no SIS User ID and a digest login id")
                    continue

                # Check if student already exists
                existing = StudentWhitelist.query.filter_by(login_id=login_id).first()

                if existing:
                    updated = False
                    # Update sis_user_id if it's missing
                    if sis_user_id and not existing.sis_user_id:
                        existing.sis_user_id = sis_user_id
                        updated = True
                    # Update headscale_key if it's missing
                    headscale_key = row.get('headscale_key', '').strip() if has_headscale else None
                    if headscale_key and not existing.headscale_key:
                        existing.headscale_key = headscale_key
                        updated = True
                    if updated:
                        print(f"  Updated {login_id}")
                        updated_count += 1
                    else:
                        print(f"  Skipping {login_id} - already exists")
                        skipped_count += 1
                    continue

                # Determine next available VLAN offset
                max_entry_vlan = db.session.query(db.func.max(StudentWhitelist.entry_vlan_tag)).scalar() or 99
                entry_vlan = max_entry_vlan + 1
                internal_vlan = entry_vlan + 100  # 200-300 range mirrors 100-200

                # Get headscale key if available
                headscale_key = row.get('headscale_key', '').strip() if has_headscale else None

                # Add new student to whitelist with pre-assigned VLANs
                student = StudentWhitelist(
                    login_id=login_id,
                    sis_user_id=sis_user_id,
                    full_name=full_name,
                    student_id=student_id,
                    section=section,
                    entry_vlan_tag=entry_vlan,
                    internal_vlan_tag=internal_vlan,
                    headscale_key=headscale_key or None
                )

                db.session.add(student)
                print(f"  Added {login_id} (SIS User ID: {sis_user_id}, entry VLAN: {entry_vlan}, internal VLAN: {internal_vlan})")
                imported_count += 1

        # Commit all changes
        db.session.commit()

        print(f"\nImport complete!")
        print(f"  New imports: {imported_count}")
        print(f"  Updated: {updated_count}")
        print(f"  Skipped: {skipped_count}")
        print(f"  Total in whitelist: {StudentWhitelist.query.count()}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python import_students.py <path_to_csv>")
        print("")
        print("Supported formats:")
        print("  Full Canvas CSV:    python import_students.py ./Grades-export.csv")
        print("  Simplified CSV:     python import_students.py ./data/students.csv")
        print("  Merged CSV:         python import_students.py ./data/students_merged.csv")
        sys.exit(1)

    csv_path = sys.argv[1]
    import_students_from_csv(csv_path)
