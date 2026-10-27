#!/usr/bin/env python3
"""
Database Migration Script: Add dual VLAN fields to users and student_whitelist tables

This script adds entry_vlan_tag and internal_vlan_tag columns and:
1. Adds columns (nullable, unique, integer) to both tables
2. Assigns unique VLAN tags to existing entries
3. Creates unique indexes

Usage:
    python scripts/migrate_add_vlan.py
"""

import sys
import os

# Add parent directory to path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.models import db, User, StudentWhitelist
from sqlalchemy import text

# VLAN range configuration
ENTRY_VLAN_MIN = 100
ENTRY_VLAN_MAX = 200
INTERNAL_VLAN_MIN = 200
INTERNAL_VLAN_MAX = 300


def migrate_database():
    """Run the migration to add dual VLAN fields"""
    app = create_app()

    with app.app_context():
        print("Starting database migration: Add dual VLAN fields")
        print("=" * 60)

        try:
            # Add missing columns to vm_records first
            migrate_vm_records_table()

            # Check and migrate student_whitelist table first
            # (so columns exist before users table tries to query them)
            migrate_whitelist_table()

            # Check and migrate users table
            migrate_users_table()

            print("\n" + "=" * 60)
            print("Migration completed!")
            print(f"\nVLAN Configuration:")
            print(f"  - Entry VLAN Range: {ENTRY_VLAN_MIN}-{ENTRY_VLAN_MAX}")
            print(f"  - Internal VLAN Range: {INTERNAL_VLAN_MIN}-{INTERNAL_VLAN_MAX}")

        except Exception as e:
            print(f"\nMigration failed: {str(e)}")
            db.session.rollback()
            raise


def migrate_vm_records_table():
    """Add missing columns to vm_records table"""
    print("\n--- VM Records Table ---")

    result = db.session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='vm_records' AND column_name='is_entry_point'"
    ))
    has_is_entry_point = result.fetchone() is not None

    if has_is_entry_point:
        print("  is_entry_point column already exists")
    else:
        print("  Adding is_entry_point column...")
        db.session.execute(text("ALTER TABLE vm_records ADD COLUMN is_entry_point BOOLEAN DEFAULT FALSE"))
        db.session.commit()
        print("  VM Records table migration complete")


def migrate_users_table():
    """Migrate users table to dual VLAN fields"""
    print("\n--- Users Table ---")

    # Check if old vlan_tag column exists
    result = db.session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='vlan_tag'"
    ))
    has_old_vlan = result.fetchone() is not None

    # Check if new columns exist
    result = db.session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='entry_vlan_tag'"
    ))
    has_new_vlans = result.fetchone() is not None

    if has_new_vlans:
        print("  entry_vlan_tag and internal_vlan_tag columns already exist")
        # Assign VLANs to users that don't have them
        users_without = User.query.filter(User.entry_vlan_tag.is_(None)).all()
        if users_without:
            print(f"  Found {len(users_without)} users without VLANs")
            assign_vlans_to_users(users_without)
        else:
            print("  All users have VLAN assignments")
        return

    # Add new columns
    print("  Adding entry_vlan_tag column...")
    db.session.execute(text("ALTER TABLE users ADD COLUMN entry_vlan_tag INTEGER"))
    print("  Adding internal_vlan_tag column...")
    db.session.execute(text("ALTER TABLE users ADD COLUMN internal_vlan_tag INTEGER"))
    db.session.commit()

    # Create unique indexes
    db.session.execute(text("CREATE UNIQUE INDEX idx_users_entry_vlan_tag ON users(entry_vlan_tag)"))
    db.session.execute(text("CREATE UNIQUE INDEX idx_users_internal_vlan_tag ON users(internal_vlan_tag)"))
    db.session.commit()

    # Migrate data from old vlan_tag if it exists
    if has_old_vlan:
        print("  Migrating from old vlan_tag column...")
        users_with_old = User.query.filter(User.entry_vlan_tag.is_(None)).all()
        for user in users_with_old:
            # Check whitelist for pre-assigned VLANs
            whitelist = StudentWhitelist.query.filter_by(login_id=user.login_id).first()
            if whitelist and whitelist.entry_vlan_tag:
                user.entry_vlan_tag = whitelist.entry_vlan_tag
                user.internal_vlan_tag = whitelist.internal_vlan_tag
                print(f"  Copied VLANs from whitelist for {user.login_id}")
        db.session.commit()

    # Assign VLANs to remaining users
    users_without = User.query.filter(User.entry_vlan_tag.is_(None)).all()
    if users_without:
        assign_vlans_to_users(users_without)

    print("  Users table migration complete")


def migrate_whitelist_table():
    """Migrate student_whitelist table to include VLAN fields"""
    print("\n--- Student Whitelist Table ---")

    result = db.session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='student_whitelist' AND column_name='entry_vlan_tag'"
    ))
    has_new_vlans = result.fetchone() is not None

    if has_new_vlans:
        print("  entry_vlan_tag and internal_vlan_tag columns already exist")
        # Assign VLANs to entries that don't have them
        entries_without = StudentWhitelist.query.filter(StudentWhitelist.entry_vlan_tag.is_(None)).all()
        if entries_without:
            print(f"  Found {len(entries_without)} entries without VLANs")
            assign_vlans_to_whitelist(entries_without)
        else:
            print("  All whitelist entries have VLAN assignments")
        return

    print("  Adding entry_vlan_tag column...")
    db.session.execute(text("ALTER TABLE student_whitelist ADD COLUMN entry_vlan_tag INTEGER"))
    print("  Adding internal_vlan_tag column...")
    db.session.execute(text("ALTER TABLE student_whitelist ADD COLUMN internal_vlan_tag INTEGER"))
    db.session.commit()

    db.session.execute(text("CREATE UNIQUE INDEX idx_whitelist_entry_vlan ON student_whitelist(entry_vlan_tag)"))
    db.session.execute(text("CREATE UNIQUE INDEX idx_whitelist_internal_vlan ON student_whitelist(internal_vlan_tag)"))
    db.session.commit()

    # Assign VLANs to all entries
    all_entries = StudentWhitelist.query.filter(StudentWhitelist.entry_vlan_tag.is_(None)).all()
    if all_entries:
        assign_vlans_to_whitelist(all_entries)

    print("  Whitelist table migration complete")


def assign_vlans_to_users(users):
    """Assign unique dual VLAN tags to a list of users"""
    assigned_entry = set(
        u.entry_vlan_tag for u in User.query.filter(User.entry_vlan_tag.isnot(None)).all()
    )

    next_vlan_offset = 0
    for vlan in range(ENTRY_VLAN_MIN, ENTRY_VLAN_MAX + 1):
        if vlan not in assigned_entry:
            next_vlan_offset = vlan - ENTRY_VLAN_MIN
            break

    assigned_count = 0
    for user in users:
        if user.entry_vlan_tag is not None:
            continue

        entry_vlan = ENTRY_VLAN_MIN + next_vlan_offset
        internal_vlan = INTERNAL_VLAN_MIN + next_vlan_offset

        if entry_vlan > ENTRY_VLAN_MAX:
            print(f"  WARNING: Ran out of VLANs for user {user.login_id}")
            break

        user.entry_vlan_tag = entry_vlan
        user.internal_vlan_tag = internal_vlan
        assigned_entry.add(entry_vlan)
        print(f"  Assigned VLANs to {user.login_id}: entry={entry_vlan}, internal={internal_vlan}")
        assigned_count += 1
        next_vlan_offset += 1

    db.session.commit()
    print(f"  Assigned {assigned_count} new dual VLAN tags to users")


def assign_vlans_to_whitelist(entries):
    """Assign unique dual VLAN tags to whitelist entries"""
    assigned_entry = set(
        e.entry_vlan_tag for e in StudentWhitelist.query.filter(StudentWhitelist.entry_vlan_tag.isnot(None)).all()
    )

    next_vlan_offset = 0
    for vlan in range(ENTRY_VLAN_MIN, ENTRY_VLAN_MAX + 1):
        if vlan not in assigned_entry:
            next_vlan_offset = vlan - ENTRY_VLAN_MIN
            break

    assigned_count = 0
    for entry in entries:
        if entry.entry_vlan_tag is not None:
            continue

        entry_vlan = ENTRY_VLAN_MIN + next_vlan_offset
        internal_vlan = INTERNAL_VLAN_MIN + next_vlan_offset

        if entry_vlan > ENTRY_VLAN_MAX:
            print(f"  WARNING: Ran out of VLANs for {entry.login_id}")
            break

        entry.entry_vlan_tag = entry_vlan
        entry.internal_vlan_tag = internal_vlan
        assigned_entry.add(entry_vlan)
        print(f"  Assigned VLANs to {entry.login_id}: entry={entry_vlan}, internal={internal_vlan}")
        assigned_count += 1
        next_vlan_offset += 1

    db.session.commit()
    print(f"  Assigned {assigned_count} new dual VLAN tags to whitelist entries")


if __name__ == '__main__':
    migrate_database()
