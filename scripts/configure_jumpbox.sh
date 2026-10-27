#!/bin/bash
#
# Configure Jumpbox for Student Access
# Installs Tailscale and sets up student accounts
#
# Prerequisites: Ubuntu VM already created and accessible via SSH
#
# Usage: ./scripts/configure_jumpbox.sh <jumpbox_ip> [ssh_user]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/site_env.sh"
SITE_ENV_FILE="${SITE_ENV_FILE:-$SCRIPT_DIR/site.env}"
load_site_env "$SITE_ENV_FILE" HEADSCALE_URL JUMPBOX_CREDENTIALS_FILE
require_site_var HEADSCALE_URL

JUMPBOX_HOST="${1}"
SSH_USER="${2:-ubuntu}"
STUDENTS_FILE="${STUDENTS_FILE:-./data/students.txt}"
JUMPBOX_CREDENTIALS_FILE="${JUMPBOX_CREDENTIALS_FILE:-./jumpbox-credentials.csv}"

if [ -z "$JUMPBOX_HOST" ]; then
    echo "Usage: $0 <jumpbox_ip> [ssh_user]"
    echo ""
    echo "Examples:"
    echo "  $0 192.0.2.50           # Default user: ubuntu"
    echo "  $0 192.0.2.50 root      # Use root user"
    echo ""
    echo "Environment variables:"
    echo "  HEADSCALE_URL - Headscale server URL (required)"
    echo "  STUDENTS_FILE - Path to students list (default: ./data/students.txt)"
    echo "  JUMPBOX_CREDENTIALS_FILE - Where generated username,password lines are written"
    echo "                             (default: ./jumpbox-credentials.csv, mode 0600)"
    exit 1
fi

echo "========================================"
echo "Configuring Jumpbox: $JUMPBOX_HOST"
echo "========================================"
echo ""

# Check SSH connectivity
echo "Testing SSH connection as $SSH_USER@$JUMPBOX_HOST..."
if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no $SSH_USER@"$JUMPBOX_HOST" "echo 'Connection OK'" 2>/dev/null; then
    echo "❌ Cannot connect to $SSH_USER@$JUMPBOX_HOST"
    echo ""
    echo "Make sure:"
    echo "  1. The VM is running and has an IP"
    echo "  2. You can SSH to it: ssh $SSH_USER@$JUMPBOX_HOST"
    echo "  3. SSH keys are set up, or use: ssh-copy-id $SSH_USER@$JUMPBOX_HOST"
    echo ""
    echo "For first-time setup on Ubuntu cloud image:"
    echo "  - Default user: ubuntu"
    echo "  - Use Proxmox console to get IP: qm guest cmd <vmid> network-get-interfaces"
    exit 1
fi
echo "✓ SSH connection successful"
echo ""

echo "Step 1: Updating system packages..."
ssh $SSH_USER@"$JUMPBOX_HOST" <<'EOF'
set -e
sudo apt-get update
sudo apt-get upgrade -y
echo "✓ System updated"
EOF

echo ""
echo "Step 2: Installing Tailscale..."
ssh $SSH_USER@"$JUMPBOX_HOST" <<'EOF'
set -e

if ! command -v tailscale &> /dev/null; then
    echo "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "✓ Tailscale installed"
else
    echo "✓ Tailscale already installed"
fi
EOF

echo ""
echo "Step 3: Installing useful tools..."
ssh $SSH_USER@"$JUMPBOX_HOST" <<'EOF'
set -e

sudo apt-get install -y \
    vim \
    tmux \
    htop \
    net-tools \
    curl \
    wget \
    git \
    openssh-client \
    qemu-guest-agent

echo "✓ Tools installed"
EOF

echo ""
echo "========================================"
echo "⚠️  MANUAL STEP REQUIRED"
echo "========================================"
echo ""
echo "Register the jumpbox with Headscale:"
echo ""
echo "1. SSH to jumpbox:"
echo "   ssh $SSH_USER@$JUMPBOX_HOST"
echo ""
echo "2. Run Tailscale registration:"
echo "   sudo tailscale up --login-server=$HEADSCALE_URL --hostname=jumpbox --accept-routes"
echo ""
echo "3. Follow the authentication instructions (URL or key)"
echo ""
read -p "Press ENTER after you've registered with Headscale..."

echo ""
echo "Step 4: Verifying Tailscale connection..."
JUMPBOX_VPN_IP=$(ssh $SSH_USER@"$JUMPBOX_HOST" "tailscale ip -4 2>/dev/null" || echo "")

if [ -z "$JUMPBOX_VPN_IP" ]; then
    echo "⚠️  Could not get VPN IP. Make sure Tailscale is connected."
    echo "   Run: ssh $SSH_USER@$JUMPBOX_HOST 'sudo tailscale status'"
    JUMPBOX_VPN_IP="<not_connected>"
else
    echo "✓ Jumpbox VPN IP: $JUMPBOX_VPN_IP"
fi

echo ""
echo "Step 5: Configuring SSH for password authentication..."
ssh $SSH_USER@"$JUMPBOX_HOST" <<'EOF'
set -e

# Enable password authentication
sudo sed -i 's/#\?PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/#\?PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo systemctl restart sshd

echo "✓ SSH configured for password authentication"
EOF

echo ""
echo "Step 6: Creating student accounts..."

if [ -f "$STUDENTS_FILE" ]; then
    echo "Using students file: $STUDENTS_FILE"
    
    # Owner-only: this file holds the one-time initial passwords.
    (umask 077; touch "$JUMPBOX_CREDENTIALS_FILE")
    chmod 600 "$JUMPBOX_CREDENTIALS_FILE"

    student_count=0
    while IFS= read -r student || [ -n "$student" ]; do
        # Skip empty lines and comments
        [[ -z "$student" || "$student" =~ ^# ]] && continue
        
        # Extract username (handle CSV format)
        username=$(echo "$student" | cut -d',' -f1 | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]_-')
        
        if [ -z "$username" ]; then
            continue
        fi
        
        echo "  Creating account: $username"
        password=$(openssl rand -base64 12)
        # The password travels only on ssh's stdin (never argv), and is applied
        # via the printf builtin so it does not appear in any process listing.
        remote_output=$(ssh $SSH_USER@"$JUMPBOX_HOST" <<EOFUSER
set -e
if ! id "$username" &>/dev/null; then
    sudo useradd -m -s /bin/bash "$username"
    printf '%s:%s\\n' "$username" '$password' | sudo chpasswd
    sudo chage -d 0 "$username"
    echo "CREATED"
    echo "    ✓ Created user: $username (must change password at first login)"
else
    echo "    ✓ User $username already exists (password unchanged)"
fi
EOFUSER
)
        if grep -qx "CREATED" <<<"$remote_output"; then
            printf '%s,%s\n' "$username" "$password" >> "$JUMPBOX_CREDENTIALS_FILE"
        fi
        grep -vx "CREATED" <<<"$remote_output" || true
        password=""
        student_count=$((student_count + 1))
    done < "$STUDENTS_FILE"
    
    echo "✓ Created/verified $student_count student accounts"
else
    echo "⚠️  No students file found at $STUDENTS_FILE"
    echo ""
    echo "To create student accounts manually, SSH to jumpbox and run:"
    echo "  sudo useradd -m -s /bin/bash <username>"
    echo "  sudo passwd <username>"
fi

echo ""
echo "========================================"
echo "✅ Jumpbox Configuration Complete!"
echo "========================================"
echo ""
echo "Jumpbox Details:"
echo "  Internal IP:  $JUMPBOX_HOST"
echo "  VPN IP:       $JUMPBOX_VPN_IP"
echo "  SSH User:     $SSH_USER"
echo ""
echo "Student Access Instructions:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Install Tailscale:"
echo "   https://tailscale.com/download"
echo ""
echo "2. Connect to Headscale:"
echo "   tailscale up --login-server=$HEADSCALE_URL"
echo ""
echo "3. SSH to jumpbox:"
echo "   ssh <username>@$JUMPBOX_VPN_IP"
echo "   Initial passwords: $JUMPBOX_CREDENTIALS_FILE (mode 0600; deliver each"
echo "   password privately, it must be changed at first login)"
echo ""
echo "4. From jumpbox, SSH to VMs:"
echo "   ssh root@<internal-ip>  (internal IPs from web interface)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Add to your documentation:"
echo "  Jumpbox VPN IP: $JUMPBOX_VPN_IP"
echo ""
