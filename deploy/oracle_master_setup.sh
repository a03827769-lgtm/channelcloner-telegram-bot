#!/usr/bin/env bash
# ==============================================================================
# Telegram Channel Cloner — VPS bootstrap (Ubuntu/Debian or Oracle Linux/RHEL; x86_64 or ARM64)
#
# Documented deployment path (DEPLOYMENT.md):
#   git clone --branch v1.4.0 https://github.com/<owner>/<repo>.git ~/channelcloner
#   cd ~/channelcloner && cp .env.example .env && nano .env
#   sudo bash deploy/oracle_master_setup.sh [--domain app.example.com --email admin@example.com]
#                                          [--with-anti-reclaim] [--with-webhook]
#   docker compose --profile docker-bot up -d --build telegram-cloner
#
# Idempotent. What it does:
#   1. installs Docker Engine + compose plugin, nginx, certbot, git, curl
#   2. firewall: only 22, 80 and 443 are reachable; the bot (8080) and the webhook (9000) stay on loopback
#   3. nginx site -> 127.0.0.1:8080 over HTTP. With --domain, certbot issues the certificate first and
#      then adds the HTTPS server (443) and the redirect itself, so nginx never references a certificate
#      that was not issued yet
#   4. optional: --with-anti-reclaim (Oracle Always Free helper), --with-webhook (GitHub tag deployer)
# It never makes the Docker socket world-writable (the app user joins the `docker` group instead) and it
# does not start the bot: create .env first, then run the docker compose command above.
# ==============================================================================
set -euo pipefail

usage() {
    sed -n '2,21p' "$0"
    exit "${1:-0}"
}

DOMAIN=""
EMAIL=""
WITH_ANTI_RECLAIM=0
WITH_WEBHOOK=0
BOT_PORT="${BOT_HTTP_PORT:-8080}"

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --email) EMAIL="${2:?--email needs a value}"; shift 2 ;;
        --with-anti-reclaim) WITH_ANTI_RECLAIM=1; shift ;;
        --with-webhook) WITH_WEBHOOK=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0 [options]" >&2
    exit 1
fi
if [ -n "$DOMAIN" ]; then
    if ! [[ "$DOMAIN" =~ ^([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$ ]]; then
        echo "Invalid --domain: $DOMAIN" >&2
        exit 1
    fi
    if [ -z "$EMAIL" ]; then
        echo "--domain requires --email (Let's Encrypt account)" >&2
        exit 1
    fi
fi
if ! [[ "$BOT_PORT" =~ ^[0-9]{2,5}$ ]]; then
    echo "Invalid BOT_HTTP_PORT: $BOT_PORT" >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-}"
if [ -z "$APP_USER" ] || [ "$APP_USER" = "root" ]; then
    APP_USER="$(stat -c '%U' "$APP_DIR")"
fi

# shellcheck disable=SC1091
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) OS_FAMILY="debian" ;;
    *" rhel "*|*" fedora "*|*" centos "*|*" ol "*) OS_FAMILY="rhel" ;;
    *) echo "Unsupported OS: ${PRETTY_NAME:-unknown}" >&2; exit 1 ;;
esac

echo "========================================================="
echo " Telegram Channel Cloner — server bootstrap"
echo " App dir: $APP_DIR | App user: $APP_USER | OS: ${PRETTY_NAME:-$OS_FAMILY}"
echo "========================================================="

# ── 1. Packages ───────────────────────────────────────────────
echo "[1/5] Installing packages (Docker, nginx, certbot)..."
if [ "$OS_FAMILY" = "debian" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl git nginx certbot python3-certbot-nginx python3
else
    dnf install -y ca-certificates curl git nginx python3 firewalld
    if ! command -v certbot >/dev/null 2>&1; then
        # certbot is shipped in EPEL on RHEL-family systems
        OS_MAJOR="${VERSION_ID:-9}"
        OS_MAJOR="${OS_MAJOR%%.*}"
        if ! dnf install -y "oracle-epel-release-el${OS_MAJOR}" && ! dnf install -y epel-release; then
            echo "[WARN] EPEL repository not available; certbot will be missing" >&2
        fi
        if ! dnf install -y certbot python3-certbot-nginx; then
            echo "[WARN] certbot could not be installed; --domain (HTTPS) will not work" >&2
        fi
    fi
fi

if ! command -v docker >/dev/null 2>&1; then
    if [ "$OS_FAMILY" = "debian" ]; then
        curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
        sh /tmp/get-docker.sh
        rm -f /tmp/get-docker.sh
    else
        dnf install -y dnf-plugins-core
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
fi
systemctl enable --now docker
if ! docker compose version >/dev/null 2>&1; then
    echo "The docker compose plugin is missing (docker-compose-plugin)" >&2
    exit 1
fi
if [ "$APP_USER" != "root" ]; then
    # Group membership instead of a world-writable socket; takes effect at the next login
    usermod -aG docker "$APP_USER"
fi

# ── 2. Firewall: 22, 80, 443 only ─────────────────────────────
echo "[2/5] Firewall: allowing only SSH (22), HTTP (80) and HTTPS (443)..."
open_iptables_port() {
    local port="$1"
    if ! iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
        iptables -I INPUT 1 -p tcp -m state --state NEW --dport "$port" -j ACCEPT
    fi
}
if [ "$OS_FAMILY" = "debian" ]; then
    if [ -f /etc/iptables/rules.v4 ] && command -v netfilter-persistent >/dev/null 2>&1; then
        # Oracle Cloud Ubuntu images ship persistent iptables rules ending in REJECT: keep them, open 80/443
        open_iptables_port 80
        open_iptables_port 443
        netfilter-persistent save
    else
        apt-get install -y ufw
        ufw allow 22/tcp
        ufw allow 80/tcp
        ufw allow 443/tcp
        ufw --force enable
    fi
else
    systemctl enable --now firewalld
    firewall-cmd --permanent --add-service=ssh
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
fi
echo "      Oracle Cloud: also allow TCP 80/443 in the VCN security list (cloud-side firewall)."

# ── 3. nginx reverse proxy (HTTP) ─────────────────────────────
echo "[3/5] nginx reverse proxy -> 127.0.0.1:${BOT_PORT} ..."
SERVER_NAME="${DOMAIN:-_}"
NGINX_TEMPLATE="$APP_DIR/deploy/nginx/channelcloner.conf"
if [ -d /etc/nginx/sites-available ]; then
    NGINX_CONF=/etc/nginx/sites-available/channelcloner.conf
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/channelcloner.conf
    rm -f /etc/nginx/sites-enabled/default
    # One location only: a second copy in conf.d duplicates the upstream / limit_req zone
    rm -f /etc/nginx/conf.d/channelcloner.conf
else
    NGINX_CONF=/etc/nginx/conf.d/channelcloner.conf
fi
if [ -f "$NGINX_CONF" ] && grep -q "managed by Certbot" "$NGINX_CONF"; then
    echo "      $NGINX_CONF already contains the certbot HTTPS configuration; left unchanged."
else
    sed -e "s#__SERVER_NAME__#${SERVER_NAME}#g" -e "s#__BOT_PORT__#${BOT_PORT}#g" "$NGINX_TEMPLATE" > "$NGINX_CONF"
fi
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
    # SELinux: allow nginx to connect to the bot on 127.0.0.1
    setsebool -P httpd_can_network_connect 1
fi
nginx -t
systemctl enable nginx
systemctl reload nginx 2>/dev/null || systemctl restart nginx

# ── 4. HTTPS via certbot (only after the HTTP site works) ─────
if [ -n "$DOMAIN" ]; then
    echo "[4/5] Let's Encrypt certificate for $DOMAIN (certbot adds the HTTPS server and redirect)..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
    nginx -t
    systemctl reload nginx
else
    echo "[4/5] No --domain given: HTTP only. Telegram Mini Apps need HTTPS — use a domain or a Cloudflare tunnel."
fi

# ── 5. Optional services ──────────────────────────────────────
render_unit() {
    sed -e "s#__APP_USER__#${APP_USER}#g" -e "s#__APP_DIR__#${APP_DIR}#g" "$1" > "$2"
    chmod 644 "$2"
}
echo "[5/5] Optional services..."
if [ "$WITH_ANTI_RECLAIM" = "1" ]; then
    render_unit "$APP_DIR/deploy/systemd/anti-reclaim.service" /etc/systemd/system/anti-reclaim.service
    systemctl daemon-reload
    systemctl enable anti-reclaim.service
    systemctl restart anti-reclaim.service
    echo "      anti-reclaim helper enabled"
fi
if [ "$WITH_WEBHOOK" = "1" ]; then
    install -d -m 700 /etc/channelcloner
    if [ ! -s /etc/channelcloner/webhook.env ]; then
        ( umask 077; printf 'WEBHOOK_SECRET=%s\n' "$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n')" > /etc/channelcloner/webhook.env )
    fi
    chmod 600 /etc/channelcloner/webhook.env
    render_unit "$APP_DIR/deploy/webhook/github-webhook.service" /etc/systemd/system/github-webhook.service
    systemctl daemon-reload
    systemctl enable github-webhook.service
    systemctl restart github-webhook.service
    echo "      webhook deployer on 127.0.0.1:9000, published as https://${DOMAIN:-<domain>}/deploy-webhook"
    echo "      its secret is in /etc/channelcloner/webhook.env (root only) — copy it into the GitHub webhook"
fi

echo "========================================================="
echo " Server ready. Next steps:"
echo "  1. $APP_DIR/.env   (cp .env.example .env — never commit it)"
echo "  2. cd $APP_DIR && docker compose --profile docker-bot up -d --build telegram-cloner"
echo "     (log out and back in once so '$APP_USER' can use docker without sudo)"
echo "  3. curl -fsS http://127.0.0.1:${BOT_PORT}/ready"
echo " Run the bot on exactly ONE host at a time."
echo "========================================================="
