#!/bin/bash

# Deployment script for user authentication system
# Run this script to apply all authentication changes

set -e

echo "========================================="
echo "User Authentication System Deployment"
echo "========================================="
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if .env has required variables
echo "1. Checking environment configuration..."
if ! grep -q "SECRET_KEY=" .env; then
    echo -e "${YELLOW}⚠️  SECRET_KEY not found in .env${NC}"
    echo "Generating SECRET_KEY..."
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "SECRET_KEY=$SECRET_KEY" >> .env
    echo -e "${GREEN}✓ SECRET_KEY added to .env${NC}"
else
    echo -e "${GREEN}✓ SECRET_KEY found${NC}"
fi

if ! grep -q "ADMIN_LOGIN_ID=" .env; then
    echo -e "${YELLOW}⚠️  ADMIN_LOGIN_ID not found in .env${NC}"
    echo "ADMIN_LOGIN_ID=admin" >> .env
    echo -e "${GREEN}✓ ADMIN_LOGIN_ID=admin added${NC}"
fi

if ! grep -q '^ADMIN_PASSWORD=.' .env; then
    echo -e "${RED}ADMIN_PASSWORD must be set to a strong operator-supplied value in .env${NC}"
    exit 1
fi

if ! grep -q "ADMIN_USERS=" .env; then
    echo "ADMIN_USERS=admin" >> .env
    echo -e "${GREEN}✓ ADMIN_USERS added${NC}"
fi

echo ""

# Rebuild containers
echo "2. Rebuilding Docker containers..."
docker compose down
docker compose build
echo -e "${GREEN}✓ Containers rebuilt${NC}"
echo ""

# Start containers
echo "3. Starting containers..."
docker compose up -d
echo -e "${GREEN}✓ Containers started${NC}"
echo ""

# Wait for MySQL to be ready
echo "4. Waiting for MySQL to be ready..."
sleep 10
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker exec proxmox-vm-automation-mysql-1 mysqladmin ping -h localhost --silent 2>/dev/null; then
        echo -e "${GREEN}✓ MySQL is ready${NC}"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT+1))
    echo "Waiting... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo -e "${RED}❌ MySQL failed to start${NC}"
    exit 1
fi

echo ""

# Initialize database
echo "5. Initializing database..."
docker exec proxmox-vm-automation python scripts/init_db.py
echo -e "${GREEN}✓ Database initialized${NC}"
echo ""

# Run migration
echo "6. Running database migration..."
docker exec proxmox-vm-automation python scripts/migrate_db.py
echo -e "${GREEN}✓ Migration completed${NC}"
echo ""

# Check if students.csv exists
echo "7. Checking for student whitelist..."
if [ -f "students.csv" ]; then
    echo "Found students.csv, importing..."
    docker cp students.csv proxmox-vm-automation:/app/
    docker exec proxmox-vm-automation python scripts/import_students.py students.csv
    echo -e "${GREEN}✓ Student whitelist imported${NC}"
else
    echo -e "${YELLOW}⚠️  students.csv not found. You'll need to import students manually:${NC}"
    echo "   1. Export Canvas roster as CSV"
    echo "   2. Copy to project directory"
    echo "   3. Run: docker cp students.csv proxmox-vm-automation:/app/"
    echo "   4. Run: docker exec proxmox-vm-automation python scripts/import_students.py students.csv"
fi

echo ""
echo "========================================="
echo -e "${GREEN}✓ Deployment Complete!${NC}"
echo "========================================="
echo ""
echo "Next steps:"
echo "1. Access the application at http://localhost:8080"
echo "2. Login with admin credentials from .env"
echo "3. Import student whitelist if not done already"
echo ""
echo "Admin login ID is configured in .env; the password is not displayed."
echo ""
