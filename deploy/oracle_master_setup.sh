#!/bin/bash
# ==============================================================================
# Oracle Cloud Always Free (Ampere A1 ARM64) Master Setup & Deployment Script
# Multi-OS Support: Ubuntu / Debian / Oracle Linux 8 & 9 (RHEL)
# ==============================================================================

set -euo pipefail

echo "========================================================="
echo "   Oracle Cloud Ampere ARM64 Master Production Setup"
echo "========================================================="

# Detect running user (opc or ubuntu)
CURRENT_USER="${SUDO_USER:-$(whoami)}"
if [ "$CURRENT_USER" = "root" ]; then
    if id "opc" &>/dev/null; then
        CURRENT_USER="opc"
    elif id "ubuntu" &>/dev/null; then
        CURRENT_USER="ubuntu"
    fi
fi

APP_DIR="/home/$CURRENT_USER/app"
mkdir -p "$APP_DIR"
echo "[*] Configuring for user: $CURRENT_USER (Home: /home/$CURRENT_USER)"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_FAMILY="$ID"
else
    OS_FAMILY="unknown"
fi
echo "[*] Detected OS: $OS_FAMILY ($PRETTY_NAME)"

# Step 1: Package Manager & Base Tools
echo "[1/6] Installing dependencies and security updates..."
if [[ "$OS_FAMILY" =~ (ubuntu|debian) ]]; then
    apt update -y
    apt install -y curl wget git ufw fail2ban htop unzip ca-certificates gnupg lsb-release nginx certbot python3-certbot-nginx python3-pip python3-venv || true
elif [[ "$OS_FAMILY" =~ (ol|oracle|rhel|centos|fedora|rocky|almalinux) ]]; then
    dnf install -y dnf-plugins-core oracle-epel-release-el9 || true
    dnf install -y curl wget git unzip ca-certificates nginx python3-pip python3 firewalld || true
fi

# Step 2: Firewall Configuration
echo "[2/6] Configuring Cloud Firewall (Ports 22, 80, 443, 8080, 9000)..."
if [[ "$OS_FAMILY" =~ (ubuntu|debian) ]]; then
    systemctl stop netfilter-persistent 2>/dev/null || true
    apt remove --purge -y iptables-persistent netfilter-persistent 2>/dev/null || true
    iptables -P INPUT ACCEPT || true
    iptables -P FORWARD ACCEPT || true
    iptables -P OUTPUT ACCEPT || true
    iptables -F || true
    rm -rf /etc/iptables/rules.v* 2>/dev/null || true

    ufw default deny incoming || true
    ufw default allow outgoing || true
    ufw allow 22/tcp || true
    ufw allow 80/tcp || true
    ufw allow 443/tcp || true
    ufw allow 8080/tcp || true
    ufw allow 9000/tcp || true
    ufw --force enable || true
elif [[ "$OS_FAMILY" =~ (ol|oracle|rhel|centos|fedora|rocky|almalinux) ]]; then
    systemctl enable --now firewalld || true
    firewall-cmd --permanent --add-port=22/tcp || true
    firewall-cmd --permanent --add-port=80/tcp || true
    firewall-cmd --permanent --add-port=443/tcp || true
    firewall-cmd --permanent --add-port=8080/tcp || true
    firewall-cmd --permanent --add-port=9000/tcp || true
    firewall-cmd --reload || true
fi

# Step 3: Docker & Docker Compose ARM64
echo "[3/6] Installing official Docker ARM64 runtime..."
if ! command -v docker &> /dev/null; then
    if [[ "$OS_FAMILY" =~ (ol|oracle|rhel|centos|fedora|rocky|almalinux) ]]; then
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo || true
        dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || true
    else
        curl -fsSL https://get.docker.com | sh || true
    fi
    usermod -aG docker "$CURRENT_USER" || true
    systemctl enable --now docker || true
    chmod 666 /var/run/docker.sock 2>/dev/null || true
else
    echo "[*] Docker already installed."
    chmod 666 /var/run/docker.sock 2>/dev/null || true
fi

# Step 4: Anti-Reclamation Keep-Alive Service
echo "[4/6] Setting up Anti-Reclamation Daemon..."
chmod +x "$APP_DIR/deploy/anti_reclaim.sh" "$APP_DIR/deploy/anti_reclaim.py" 2>/dev/null || true
cat << EOF > /etc/systemd/system/anti-reclaim.service
[Unit]
Description=Oracle Cloud Always Free Anti-Reclamation Service
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$APP_DIR/deploy
ExecStart=/usr/bin/python3 $APP_DIR/deploy/anti_reclaim.py
Restart=always
RestartSec=5
Nice=19
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now anti-reclaim.service

# Step 5: GitHub Webhook CI/CD Service
echo "[5/6] Setting up GitHub Webhook CI/CD Service..."
chmod +x "$APP_DIR/deploy/webhook/deploy_webhook.py" 2>/dev/null || true
cat << EOF > /etc/systemd/system/github-webhook.service
[Unit]
Description=GitHub Webhook Auto-Deployment Service
After=network.target docker.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$APP_DIR
Environment=PROJECT_DIR=$APP_DIR
Environment=WEBHOOK_PORT=9000
ExecStart=/usr/bin/python3 $APP_DIR/deploy/webhook/deploy_webhook.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now github-webhook.service

# Step 6: Nginx Reverse Proxy & SELinux Policy
echo "[6/6] Configuring Nginx Reverse Proxy..."
mkdir -p /etc/nginx/conf.d /etc/nginx/sites-available /etc/nginx/sites-enabled 2>/dev/null || true
if [ -f "$APP_DIR/deploy/nginx/channelcloner.conf" ]; then
    if [ -d /etc/nginx/sites-available ]; then
        cp "$APP_DIR/deploy/nginx/channelcloner.conf" /etc/nginx/sites-available/channelcloner.conf
        ln -sf /etc/nginx/sites-available/channelcloner.conf /etc/nginx/sites-enabled/
        rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    fi
    cp "$APP_DIR/deploy/nginx/channelcloner.conf" /etc/nginx/conf.d/channelcloner.conf 2>/dev/null || true
    rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
    
    if command -v setsebool &>/dev/null; then
        setsebool -P httpd_can_network_connect 1 2>/dev/null || true
    fi
    
    systemctl enable --now nginx 2>/dev/null || true
    nginx -t && (systemctl reload nginx || systemctl restart nginx) || true
fi

echo "========================================================="
echo " [SUCCESS] Oracle Cloud Production Environment Ready!"
echo " User: $CURRENT_USER | App Directory: $APP_DIR"
echo "========================================================="
