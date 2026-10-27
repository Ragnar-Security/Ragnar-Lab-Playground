#!/bin/bash
# Production Deployment Quick Start Script
# This script helps set up the production environment

set -e

echo "======================================"
echo "Proxmox VM Automation - Production Setup"
echo "======================================"
echo

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo "⚠️  Warning: Running as root. Consider using a non-root user with Docker access."
fi

# Check prerequisites
echo "Checking prerequisites..."

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi
echo "✓ Docker found: $(docker --version)"

# Check Docker Compose
if ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi
echo "✓ Docker Compose found: $(docker compose version)"

# Check if .env exists
if [ -f .env ]; then
    echo "⚠️  .env file already exists."
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Keeping existing .env file."
    else
        echo "Creating .env from template..."
        cp .env.example .env
        echo "✓ .env file created"
    fi
else
    echo "Creating .env from template..."
    cp .env.example .env
    echo "✓ .env file created"
fi

# Replace KEY=... in .env without leaving a backup: `sed -i.bak` would drop a
# world-readable .env.bak holding the generated secrets next to the repo.
# Values go through python so characters special to sed (/ & \) are safe.
set_env() {
  local tmp
  tmp=$(mktemp .env.XXXXXX) || return 1
  chmod 600 "$tmp"
  KEY="$1" VAL="$2" python3 - .env > "$tmp" <<'PY' || { rm -f "$tmp"; return 1; }
import os, sys
key, val = os.environ["KEY"], os.environ["VAL"]
for line in open(sys.argv[1]):
    print(f"{key}={val}" if line.startswith(key + "=") else line.rstrip("\n"))
PY
  mv "$tmp" .env
}

# Generate secret keys
echo
echo "Generating secure credentials..."

SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32)
MYSQL_ROOT_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null || openssl rand -base64 32)
MYSQL_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))' 2>/dev/null || openssl rand -base64 24)

# Update .env with generated secrets
set_env SECRET_KEY "$SECRET_KEY"
set_env MYSQL_ROOT_PASSWORD "$MYSQL_ROOT_PASSWORD"
set_env MYSQL_PASSWORD "$MYSQL_PASSWORD"

echo "✓ Secure credentials generated"

# Prompt for Proxmox details
echo
echo "======================================"
echo "Proxmox Configuration"
echo "======================================"
read -p "Enter Proxmox host (IP or hostname): " PROXMOX_HOST
read -p "Enter Proxmox user [root@pam]: " PROXMOX_USER
PROXMOX_USER=${PROXMOX_USER:-root@pam}
read -sp "Enter Proxmox password: " PROXMOX_PASSWORD
echo

set_env PROXMOX_HOST "$PROXMOX_HOST"
set_env PROXMOX_USER "$PROXMOX_USER"
set_env PROXMOX_PASSWORD "$PROXMOX_PASSWORD"

echo "✓ Proxmox configuration saved"

# Test Proxmox connection
echo
echo "Testing Proxmox connection..."
# Values travel through the environment, never through Python source.
PROXMOX_HOST="$PROXMOX_HOST" PROXMOX_USER="$PROXMOX_USER" PROXMOX_PASSWORD="$PROXMOX_PASSWORD" python3 - <<'PY' || echo "⚠️  Connection test failed. Please verify credentials."
import os
import sys
from proxmoxer import ProxmoxAPI
try:
    verify = os.environ.get('PROXMOX_CA_BUNDLE') or os.environ.get('PROXMOX_VERIFY_SSL', 'true').lower() != 'false'
    px = ProxmoxAPI(os.environ['PROXMOX_HOST'], user=os.environ['PROXMOX_USER'], password=os.environ['PROXMOX_PASSWORD'], verify_ssl=verify)
    version = px.version.get()
    print('✓ Successfully connected to Proxmox version:', version['version'])
except Exception as e:
    print('❌ Failed to connect to Proxmox:', str(e))
    sys.exit(1)
PY

# Check templates.yaml
echo
if [ ! -f templates.yaml ]; then
    echo "⚠️  templates.yaml not found. You need to create it before deploying."
    echo "See PRODUCTION_DEPLOYMENT.md for template configuration."
else
    echo "✓ templates.yaml found"
fi

# Create required directories
echo
echo "Creating required directories..."
mkdir -p data backups
chmod 755 data backups
echo "✓ Directories created"

# Set permissions
echo
echo "Setting file permissions..."
chmod 600 .env
chmod 755 scripts/*.sh 2>/dev/null || true
echo "✓ Permissions set"

# Summary
echo
echo "======================================"
echo "Setup Complete!"
echo "======================================"
echo
echo "Important credentials have been saved to .env"
echo "Please review and update .env with your specific settings."
echo
echo "Next steps:"
echo "1. Review and configure templates.yaml"
echo "2. Deploy with: docker compose -f docker-compose.prod.yml up -d --build"
echo "3. Check logs: docker compose -f docker-compose.prod.yml logs -f"
echo "4. Access at: http://$(hostname -I | awk '{print $1}'):5000"
echo
echo "⚠️  IMPORTANT SECURITY REMINDERS:"
echo "- Change admin password immediately after first login"
echo "- Review firewall rules"
echo "- Consider setting up HTTPS with a reverse proxy"
echo "- Set up automated backups (scripts/backup-database.sh)"
echo
echo "For detailed documentation, see PRODUCTION_DEPLOYMENT.md"
echo
