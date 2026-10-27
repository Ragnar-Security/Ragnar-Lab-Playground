#!/bin/bash

# Script to fix database schema issues
# This will drop and recreate the database with the correct schema

set -e

echo "========================================="
echo "Database Schema Fix"
echo "========================================="
echo ""
echo "This will:"
echo "1. Stop containers"
echo "2. Remove database volume"
echo "3. Restart containers with fresh database"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]
then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Stopping containers..."
docker compose down -v

echo ""
echo "Starting containers with fresh database..."
docker compose up -d

echo ""
echo "Waiting for database to initialize (30 seconds)..."
sleep 30

echo ""
echo "Checking database schema..."
docker exec proxmox-vm-automation python -c "
from app import create_app, db
from app.models import VMRecord
import sys

app = create_app()
with app.app_context():
    # Check if created_by_user_id column exists
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns('vm_records')]
    
    if 'created_by_user_id' in columns:
        print('✓ Database schema is correct: created_by_user_id column exists')
        sys.exit(0)
    else:
        print('❌ Database schema is incorrect: created_by_user_id column missing')
        sys.exit(1)
"

if [ $? -eq 0 ]; then
    echo ""
    echo "========================================="
    echo "✓ Database fixed successfully!"
    echo "========================================="
    echo ""
    echo "Account credentials are loaded from .env and are not displayed."
    echo ""
    echo "Access at: http://localhost:8080"
else
    echo ""
    echo "========================================="
    echo "❌ Database schema issue persists"
    echo "========================================="
    echo ""
    echo "Please check the logs:"
    echo "  docker compose logs vm-automation"
fi
