#!/usr/bin/env python3
"""
Database cleanup utility
Removes all VM records from the database
"""

import sys
import os

# Add parent directory to path so we can import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models import VMRecord

def cleanup_database():
    """Remove all VM records from database"""
    app = create_app()
    
    with app.app_context():
        # Count records before deletion
        count = VMRecord.query.count()
        
        if count == 0:
            print("No VM records found in database")
            return
        
        # Ask for confirmation
        print(f"Found {count} VM record(s) in database")
        response = input("Delete all records? (yes/no): ")
        
        if response.lower() in ['yes', 'y']:
            # Delete all records
            VMRecord.query.delete()
            db.session.commit()
            print(f"✓ Successfully deleted {count} VM record(s)")
        else:
            print("Operation cancelled")

if __name__ == '__main__':
    cleanup_database()
