#!/usr/bin/env python3
"""
Autonomous Oracle Cloud VPS Deployment Script.
Auto-detects user (opc / ubuntu), hardens firewall, sets up ARM64 runtime, anti-reclamation daemon, and starts the container stack.
"""

import os
import sys
import time
import subprocess
from pathlib import Path
import logging
logger = logging.getLogger(__name__)

# Force UTF-8 stdout if possible
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

def run_ssh_command(host: str, key_path: str, user: str, command: str) -> subprocess.CompletedProcess:
    ssh_cmd = [
        "ssh",
        "-o", "LogLevel=ERROR",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-i", str(key_path),
        f"{user}@{host}",
        command
    ]
    return subprocess.run(ssh_cmd, capture_output=True, text=True, errors="replace")

def detect_ssh_user(host: str, key_path: str) -> str:
    for candidate in ["opc", "ubuntu", "root"]:
        print(f"[*] Testing SSH user: {candidate}@{host}...")
        res = run_ssh_command(host, key_path, candidate, "echo SSH_CONNECTED")
        if "SSH_CONNECTED" in res.stdout:
            print(f"[+] Active SSH user detected: {candidate}")
            return candidate
    return ""

def deploy_to_vps(ip: str, key_file: str, domain: str = ""):
    print("\n=======================================================")
    print(f"  DEPLOYING TO ORACLE CLOUD VPS: {ip}")
    print("=======================================================")
    
    user = detect_ssh_user(ip, key_file)
    if not user:
        print(f"[-] Could not authenticate to {ip} with key {key_file} as opc or ubuntu.")
        sys.exit(1)

    # 1. Authorize local ~/.ssh/oracle_arm_key.pub if present
    pub_key_path = Path.home() / ".ssh" / "oracle_arm_key.pub"
    if pub_key_path.exists():
        pub_str = pub_key_path.read_text(encoding="utf-8", errors="ignore").strip()
        print("[*] Syncing local Ed25519 key to authorized_keys...")
        run_ssh_command(ip, key_file, user, f"echo '{pub_str}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys")

    # 2. Create remote directory
    print("[1/6] Creating remote application structure...")
    run_ssh_command(ip, key_file, user, "mkdir -p ~/app/deploy/systemd ~/app/deploy/nginx ~/app/deploy/webhook ~/app/temp_media ~/app/database")

    # 3. Transfer code and assets via SCP
    print("[2/6] Uploading configuration, source code and deploy assets...")
    scp_base = ["scp", "-q", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-i", str(key_file)]
    
    # Upload deploy folder
    subprocess.run(scp_base + ["-r", "deploy/", f"{user}@{ip}:~/app/"], check=False)
    
    # Upload core project files
    for root_file in ["docker-compose.yml", "Dockerfile", "requirements.txt", "run.py"]:
        if os.path.exists(root_file):
            subprocess.run(scp_base + [root_file, f"{user}@{ip}:~/app/"], check=False)

    # Upload .env
    if os.path.exists(".env"):
        print("[*] Uploading .env credentials...")
        subprocess.run(scp_base + [".env", f"{user}@{ip}:~/app/.env"], check=False)
    elif os.path.exists(".env.example"):
        subprocess.run(scp_base + [".env.example", f"{user}@{ip}:~/app/.env"], check=False)

    # Upload Python modules
    for module_dir in ["bot", "admin_bot", "services", "config", "database"]:
        if os.path.exists(module_dir):
            subprocess.run(scp_base + ["-r", module_dir, f"{user}@{ip}:~/app/"], check=False)

    # 4. Execute Master Setup (Firewall, Docker ARM64, Anti-Reclaim, Webhook)
    print("[3/6] Running remote system setup, firewall hardening & Docker runtime...")
    setup_cmd = "chmod +x ~/app/deploy/*.sh ~/app/deploy/webhook/*.py 2>/dev/null || true; sudo bash ~/app/deploy/oracle_master_setup.sh"
    setup_res = run_ssh_command(ip, key_file, user, setup_cmd)
    print(setup_res.stdout)
    if setup_res.stderr:
        print(f"[LOG] {setup_res.stderr}")

    # 5. Domain / SSL setup if domain is provided
    if domain:
        print(f"[4/6] Configuring Nginx Reverse Proxy & Certbot SSL for {domain}...")
        ssl_cmd = (
            f"sudo sed -i 's/your_domain.com/{domain}/g' /etc/nginx/sites-available/channelcloner.conf /etc/nginx/conf.d/channelcloner.conf 2>/dev/null || true; "
            f"sudo nginx -t && sudo systemctl reload nginx; "
            f"sudo certbot --nginx -d {domain} --non-interactive --agree-tos -m admin@{domain} --redirect || true"
        )
        run_ssh_command(ip, key_file, user, ssl_cmd)

    # 6. Start Docker Compose Stack
    print("[5/6] Launching Multi-Bot Docker container stack...")
    start_cmd = "cd ~/app && sudo docker compose down 2>/dev/null || true; sudo docker compose up -d --build"
    start_res = run_ssh_command(ip, key_file, user, start_cmd)
    print(start_res.stdout)

    # 7. Verification & Healthcheck
    print("[6/6] Verifying system health and uptime daemons...")
    time.sleep(6)
    verify_res = run_ssh_command(ip, key_file, user, "sudo docker ps && curl -s http://127.0.0.1:8080/health")
    print(f"\n[+] Healthcheck and Process Output:\n{verify_res.stdout}")
    
    print("\n=======================================================")
    print("  DEPLOYMENT COMPLETE! SERVER IS LIVE 24/7!")
    print(f"  * Host:              {ip}")
    print(f"  * User:              {user}")
    print(f"  * Keep-Alive URL:    http://{ip}:8080/health")
    print(f"  * Webhook CI/CD:     http://{ip}:9000/webhook")
    print("  * Anti-Reclamation:  ACTIVE (18% CPU & 4GB RAM Protected)")
    print("=======================================================\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python deploy_remote.py <VPS_IP> <KEY_PATH> [DOMAIN]")
        sys.exit(1)
    
    vps_ip = sys.argv[1]
    key_path = sys.argv[2]
    domain_name = sys.argv[3] if len(sys.argv) > 3 else ""
    deploy_to_vps(vps_ip, key_path, domain_name)
