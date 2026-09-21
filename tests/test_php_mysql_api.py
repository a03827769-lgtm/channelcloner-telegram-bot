#!/usr/bin/env python3
"""
Test Suite: ChannelCloner Pro — PHP 8.3 + MySQL 8.0 Full Stack Verification
Empirically tests all PHP API endpoints running against the live MySQL database.
"""

import os
import sys
import time
import socket
import subprocess
import requests
import pytest
from pathlib import Path

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

class TestPhpMysqlAPI:
    server_process = None
    base_url = ""
    port = 0

    @classmethod
    def setup_class(cls):
        cls.port = find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        
        # Start PHP built-in server serving miniapp
        cmd = [
            "php",
            "-S", f"127.0.0.1:{cls.port}",
            "-t", "miniapp"
        ]
        
        env = os.environ.copy()
        env["MYSQL_HOST"] = "127.0.0.1"
        env["MYSQL_PORT"] = "3306"
        env["MYSQL_DATABASE"] = "channelcloner"
        env["MYSQL_USER"] = "cloner_user"
        env["MYSQL_PASSWORD"] = "cloner_pass_2026"

        cls.server_process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path(__file__).parent.parent)
        )

        # Wait for server ready
        time.sleep(1.0)
        for _ in range(30):
            try:
                r = requests.get(f"{cls.base_url}/api/auth.php", timeout=2.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)

    @classmethod
    def teardown_class(cls):
        if cls.server_process:
            cls.server_process.terminate()
            cls.server_process.wait(timeout=5)

    def test_01_auth_endpoint(self):
        url = f"{self.base_url}/api/auth.php"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "user" in data
        user = data["user"]
        assert user["user_id"] == 1110001
        assert user["is_admin"] is True
        assert user["tier"] in ["pro", "vip", "free"]
        assert "pairs_count" in user

    def test_02_pairs_list(self):
        url = f"{self.base_url}/api/pairs.php"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert isinstance(data["pairs"], list)
        assert len(data["pairs"]) >= 1

    def test_03_pairs_crud_lifecycle(self):
        # 1. Create Pair
        url = f"{self.base_url}/api/pairs.php"
        payload = {
            "source_channel": "@py_test_source",
            "source_title": "PyTest Source Channel",
            "target_channel": "@py_test_target",
            "target_title": "PyTest Target Channel",
            "clone_mode": "clean",
            "clean_links": 1,
            "custom_signature": "✍️ PyTest Sig",
            "image_watermark_type": "text",
            "image_watermark_text": "PYTEST_WM",
            "image_watermark_pos": "bottom_right",
            "drip_delay_minutes": 0
        }
        r = requests.post(url, json=payload, timeout=5.0)
        assert r.status_code == 201
        res = r.json()
        assert res["ok"] is True
        pair_id = res["pair_id"]
        assert pair_id > 0

        # 2. Toggle Pair
        toggle_url = f"{self.base_url}/api/pairs.php?action=toggle&id={pair_id}"
        r_tog = requests.post(toggle_url, timeout=5.0)
        assert r_tog.status_code == 200
        assert r_tog.json()["ok"] is True

        # 3. Test Post
        test_url = f"{self.base_url}/api/pairs.php?action=test&id={pair_id}"
        r_test = requests.post(test_url, timeout=5.0)
        assert r_test.status_code == 200
        assert r_test.json()["ok"] is True

        # 4. Delete Pair
        del_url = f"{self.base_url}/api/pairs.php?action=delete&id={pair_id}"
        r_del = requests.post(del_url, timeout=5.0)
        assert r_del.status_code == 200
        assert r_del.json()["ok"] is True

    def test_04_story_settings(self):
        url = f"{self.base_url}/api/story.php?action=settings"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "settings" in data
        assert data["settings"]["min_price"] >= 700.0

    def test_05_audio_tracks_list_and_stream(self):
        # 1. List
        url = f"{self.base_url}/api/audio.php"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["tracks"]) >= 12
        first_track = data["tracks"][0]
        assert first_track["exists"] is True

        # 2. Stream
        stream_url = f"{self.base_url}/api/audio.php?track={first_track['filename']}"
        r_stream = requests.get(stream_url, timeout=5.0)
        assert r_stream.status_code == 200
        assert "audio/mpeg" in r_stream.headers.get("Content-Type", "")
        assert len(r_stream.content) > 1000

    def test_06_backfill(self):
        url = f"{self.base_url}/api/backfill.php"
        r = requests.post(url, json={"pair_id": 1, "limit": 20}, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["limit"] == 20
        assert data["status"] == "queued"
        assert len(data["logs"]) >= 2

    def test_07_billing_tariffs(self):
        url = f"{self.base_url}/api/billing.php?action=tariffs"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["tariffs"]) == 3

    def test_08_system_telemetry(self):
        url = f"{self.base_url}/api/system.php"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "telemetry" in data
        telem = data["telemetry"]
        assert "MySQL" in telem["db_type"]
        assert telem["db_size_mb"] > 0
        assert "logs" in data

    def test_09_public_spa_index(self):
        url = f"{self.base_url}/public/index.html"
        r = requests.get(url, timeout=5.0)
        assert r.status_code == 200
        assert "ChannelCloner Pro" in r.text
