#!/bin/bash
# Script to create student accounts on the jumpbox from students.csv
# Usage: sudo ./create_student_accounts.sh <path-to-students.csv>

if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: This script must be run as root (use sudo)"
    exit 1
fi

if [ -z "$1" ]; then
    echo "ERROR: Please provide the path to students.csv"
    echo "Usage: sudo $0 <path-to-students.csv>"
    exit 1
fi

CSV_FILE="$1"

if [ ! -f "$CSV_FILE" ]; then
    echo "ERROR: File not found: $CSV_FILE"
    exit 1
fi

# Default password (students will be forced to change on first login)
DEFAULT_PASSWORD="ChangeMe2026!"

echo "Creating student accounts from $CSV_FILE"
echo "Default password: $DEFAULT_PASSWORD"
echo "Students will be forced to change password on first login"
echo ""

CREATED=0
SKIPPED=0

while IFS=, read -r sis_id login_id; do
    # Trim whitespace and carriage returns (Windows CRLF issue)
    login_id=$(echo "$login_id" | tr -d '\r' | xargs)
    
    # Skip header row
    if [ "$login_id" = "SIS Login ID" ]; then
        continue
    fi
    
    # Skip empty lines
    if [ -z "$login_id" ]; then
        continue
    fi
    
    # Check if user already exists
    if id "$login_id" &>/dev/null; then
        echo "SKIP: User $login_id already exists"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi
    
    # Create user with home directory
    if useradd -m -s /bin/bash "$login_id"; then
        # Set default password
        echo "$login_id:$DEFAULT_PASSWORD" | chpasswd
        
        # Force password change on first login
        chage -d 0 "$login_id"
        
        echo "CREATED: $login_id"
        CREATED=$((CREATED + 1))
    else
        echo "ERROR: Failed to create user $login_id"
    fi
    
done < "$CSV_FILE"

echo ""
echo "=========================================="
echo "Account creation complete!"
echo "Created: $CREATED"
echo "Skipped: $SKIPPED"
echo "=========================================="
echo ""
echo "Students can log in with:"
echo "  Username: <their-login-id>"
echo "  Password: $DEFAULT_PASSWORD"
echo "  (They will be forced to change on first login)"
