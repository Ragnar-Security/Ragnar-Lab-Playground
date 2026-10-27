#!/bin/bash
# Create student accounts on the jumpbox from a Canvas students.csv.
# Runs ON the jumpbox, as root.
#
# Usage: sudo ./create_student_accounts.sh <path-to-students.csv>
#
# Environment:
#   JUMPBOX_CREDENTIALS_FILE  where generated username,password lines are
#                             written (default: ./jumpbox-credentials.csv, mode 0600)
#
# Every new account gets its own random password, forced to change at first
# login. The password travels only on chpasswd's stdin (never argv) and is
# written only to the credentials file; it is never printed.

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)"
    exit 1
fi

if [ -z "$1" ]; then
    echo "ERROR: Please provide the path to students.csv"
    echo "Usage: sudo $0 <path-to-students.csv>"
    exit 1
fi

CSV_FILE="$1"
JUMPBOX_CREDENTIALS_FILE="${JUMPBOX_CREDENTIALS_FILE:-./jumpbox-credentials.csv}"

if [ ! -f "$CSV_FILE" ]; then
    echo "ERROR: File not found: $CSV_FILE"
    exit 1
fi

# Owner-only: this file holds the one-time initial passwords.
(umask 077; touch "$JUMPBOX_CREDENTIALS_FILE")
chmod 600 "$JUMPBOX_CREDENTIALS_FILE"

echo "Creating student accounts from $CSV_FILE"
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
        password=$(openssl rand -base64 12)
        # printf is a builtin, so the password is in no process listing.
        printf '%s:%s\n' "$login_id" "$password" | chpasswd

        # Force password change on first login
        chage -d 0 "$login_id"

        (umask 077; printf '%s,%s\n' "$login_id" "$password" >> "$JUMPBOX_CREDENTIALS_FILE")
        password=""

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
echo "Initial passwords: $JUMPBOX_CREDENTIALS_FILE (mode 0600)"
echo "Deliver each password privately; it must be changed at first login."
