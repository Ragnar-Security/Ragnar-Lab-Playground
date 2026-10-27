#!/usr/bin/env python3
"""
Quick migration script to add missing columns to production database
Run this with: docker exec -it proxmox-vm-automation-prod python add_missing_columns.py
"""

import os
import sys
from sqlalchemy import create_engine, text

# Get database URL from environment
db_user = os.environ.get('MYSQL_USER', 'vmuser')
db_password = os.environ.get('MYSQL_PASSWORD', 'vmpassword')
db_host = os.environ.get('MYSQL_HOST', 'db')
db_name = os.environ.get('MYSQL_DATABASE', 'vmdatabase')
db_url = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"

print(f"Connecting to database: {db_host}/{db_name}")

try:
    engine = create_engine(db_url)
    
    with engine.connect() as conn:
        # Check if deletion_retry_count column exists
        result = conn.execute(text("""
            SELECT COUNT(*) as count
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = :db_name
            AND TABLE_NAME = 'vm_records'
            AND COLUMN_NAME = 'deletion_retry_count'
        """), {"db_name": db_name})
        
        column_exists = result.fetchone()[0] > 0
        
        if not column_exists:
            print("→ Adding 'deletion_retry_count' column to vm_records...")
            conn.execute(text("""
                ALTER TABLE vm_records
                ADD COLUMN deletion_retry_count INT DEFAULT 0 NOT NULL
            """))
            conn.commit()
            print("✓ Added 'deletion_retry_count' column")
        else:
            print("✓ Column 'deletion_retry_count' already exists")
        
        # Check if admin_alerts table exists
        result = conn.execute(text("""
            SELECT COUNT(*) as count
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = :db_name
            AND TABLE_NAME = 'admin_alerts'
        """), {"db_name": db_name})
        
        table_exists = result.fetchone()[0] > 0
        
        if not table_exists:
            print("→ Creating 'admin_alerts' table...")
            conn.execute(text("""
                CREATE TABLE admin_alerts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    alert_type VARCHAR(100) NOT NULL,
                    severity VARCHAR(50) NOT NULL,
                    message TEXT NOT NULL,
                    vmid INT,
                    vm_name VARCHAR(255),
                    details TEXT,
                    created_at DATETIME NOT NULL,
                    acknowledged BOOLEAN DEFAULT FALSE,
                    acknowledged_at DATETIME,
                    INDEX idx_alert_type (alert_type),
                    INDEX idx_created_at (created_at),
                    INDEX idx_acknowledged (acknowledged),
                    INDEX idx_vmid (vmid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
            conn.commit()
            print("✓ Created 'admin_alerts' table")
        else:
            print("✓ Table 'admin_alerts' already exists")
        
        print("\n✅ Database migration completed successfully!")
        
except Exception as e:
    print(f"\n❌ Migration failed: {str(e)}", file=sys.stderr)
    sys.exit(1)
