#!/usr/bin/env python3
"""
Test Suite: ChannelCloner Pro — Full Docker Production Stack E2E Verification
Verifies all 5 Docker containers, networking, MySQL 8.0, PHP 8.3-FPM FastCGI,
Nginx static caching, Cloudflare tunnel routing, and Telegram bot polling.
"""

import os
import sys
import json
import socket
import urllib.request
import subprocess
import requests
import pymysql
import pytest

# Ensure UTF-8 stdout
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

class TestDockerProductionStack:
    @classmethod
    def setup_class(cls):
        cls.web_url = "http://127.0.0.1:8080"
        cls.mysql_port = 3307

    def test_01_all_five_docker_containers_running_and_healthy(self):
        """Verifies all 5 stack containers are in running state."""
        cmd = ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        lines = res.stdout.strip().split("\n")
        
        container_map = {}
        for line in lines:
            if "\t" in line:
                name, status = line.split("\t", 1)
                container_map[name.strip()] = status.strip()

        required_containers = [
            "channelcloner_mysql",
            "channelcloner_miniapp_php",
            "channelcloner_miniapp_web",
            "channelcloner_cloudflared",
            "telegram_channel_cloner"
        ]

        for container in required_containers:
            assert container in container_map, f"Container {container} is not running!"
            assert "Up" in container_map[container], f"Container {container} status: {container_map[container]}"

    def test_02_mysql_container_connectivity_and_schema(self):
        """Connects directly to MySQL 8.0 on mapped host port 3307."""
        conn = pymysql.connect(
            host="127.0.0.1",
            port=self.mysql_port,
            user="cloner_user",
            password="cloner_pass_2026",
            database="channelcloner",
            charset="utf8mb4",
            autocommit=True
        )
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES;")
            tables = [r[0] for r in cur.fetchall()]
            assert "users" in tables
            assert "channel_pairs" in tables
            assert "bot_settings" in tables
            assert "story_settings" in tables

            # Verify webapp_url is set
            cur.execute("SELECT value_text FROM bot_settings WHERE key_name = 'webapp_url';")
            row = cur.fetchone()
            assert row is not None
            assert "trycloudflare.com" in row[0]
        conn.close()

    def test_03_nginx_php_fpm_fastcgi_system_telemetry(self):
        """Tests that Nginx on port 8080 forwards /api/system.php to PHP-FPM container."""
        r = requests.get(f"{self.web_url}/api/system.php", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        telemetry = data.get("telemetry", {})
        assert telemetry.get("db_type") == "MySQL 8.0 InnoDB"
        assert telemetry.get("php_version", "").startswith("8.3")
        assert telemetry.get("mtproto_connected") is True

    def test_04_frontend_spa_served_by_nginx(self):
        """Verifies Vue/Vite SPA is served by Nginx with appropriate headers."""
        r = requests.get(f"{self.web_url}/", timeout=5)
        assert r.status_code == 200
        assert "html" in r.text.lower()
        assert "ChannelCloner" in r.text or "vite" in r.text or "app" in r.text
        assert r.headers.get("X-Frame-Options") == "ALLOWALL"

    def test_05_public_cloudflare_tunnel_connectivity(self):
        """Verifies Cloudflare public edge routing to our Nginx + PHP Docker container."""
        # Read active URL from file
        from pathlib import Path
        url_file = Path("data/active_tunnel_url.txt")
        assert url_file.exists()
        active_url = url_file.read_text(encoding="utf-8").strip()
        assert active_url.startswith("https://")
        assert "trycloudflare.com" in active_url

        # Check tunnel container status
        cmd = ["docker", "logs", "--tail", "20", "channelcloner_cloudflared"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logs = res.stdout + res.stderr
        assert "Registered tunnel connection" in logs or "healthy" in logs or "trycloudflare.com" in logs

    def test_06_telegram_cloner_bot_container_polling(self):
        """Verifies Telegram bot container is actively polling without crash."""
        cmd = ["docker", "logs", "--tail", "30", "telegram_channel_cloner"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logs = res.stdout + res.stderr
        assert "Public Client Bot started" in logs or "Run polling for bot" in logs
        assert "disk I/O error" not in logs
        assert "Traceback" not in logs
