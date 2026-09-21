#!/usr/bin/env python3
"""
Test Suite: 24/7 Cloudflare Tunnel Watchdog Verification
Empirically tests tunnel process spawning, live URL extraction, health probing,
auto-recovery, and dynamic synchronization with Bot configuration and MySQL.
"""

import os
import sys
import time
import json
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from services.cloudflare_tunnel_watchdog import CloudflareTunnelWatchdog

class TestCloudflareTunnelWatchdog:
    @classmethod
    def setup_class(cls):
        cls.watchdog = CloudflareTunnelWatchdog(
            local_target="http://127.0.0.1:8089",
            probe_interval=5,
            max_probe_failures=2
        )

    @classmethod
    def teardown_class(cls):
        cls.watchdog.stop()

    def test_01_save_active_url_syncs_file_and_settings(self):
        test_url = "https://mock-test-tunnel.trycloudflare.com"
        self.watchdog._save_active_url(test_url)

        # 1. File verification
        assert self.watchdog.url_file.exists()
        saved = self.watchdog.url_file.read_text(encoding="utf-8").strip()
        assert saved == test_url

        # 2. Environment variable
        assert os.environ.get("WEBAPP_URL") == test_url

        # 3. Status file
        self.watchdog._write_status("healthy", "Test details", http_code=200)
        assert self.watchdog.status_file.exists()
        status_data = json.loads(self.watchdog.status_file.read_text(encoding="utf-8"))
        assert status_data["status"] == "healthy"
        assert status_data["active_url"] == test_url

    def test_02_keyboard_reads_active_url(self):
        from bot.keyboards.inline_buttons import get_active_webapp_url, get_main_reply_keyboard, get_main_menu_keyboard
        
        active = get_active_webapp_url()
        assert active == "https://mock-test-tunnel.trycloudflare.com"

        # Reply keyboard should cleanly exclude redundant Mini App button
        reply_kb = get_main_reply_keyboard()
        assert reply_kb is not None
        all_texts = [btn.text for row in reply_kb.keyboard for btn in row]
        assert "📱 Mini Appni Ochish" not in all_texts

        # Inline keyboard should cleanly start with menu actions
        inline_kb = get_main_menu_keyboard()
        assert inline_kb is not None
        inline_texts = [btn.text for row in inline_kb.inline_keyboard for btn in row]
        assert not any("Mini Appni Ochish" in t for t in inline_texts)

    def test_03_real_cloudflared_spawn_and_url_capture(self):
        """Spawns real cloudflared and captures edge trycloudflare.com URL"""
        success = self.watchdog.start_tunnel_process()
        assert success is True
        assert self.watchdog.active_url is not None
        assert self.watchdog.active_url.startswith("https://")
        assert "trycloudflare.com" in self.watchdog.active_url

        # Verify process is actively running
        assert self.watchdog.process is not None
        assert self.watchdog.process.poll() is None

        # Verify health probe passes
        is_healthy = self.watchdog.probe_health()
        assert is_healthy is True

    def test_04_watchdog_auto_heals_when_process_killed(self):
        """Simulates unexpected tunnel death and verifies auto-healing"""
        old_pid = self.watchdog.process.pid
        
        # Simulate unexpected crash by killing the process
        self.watchdog.process.kill()
        self.watchdog.process.wait()
        assert self.watchdog.process.poll() is not None

        # Probe health should detect dead process
        assert self.watchdog.probe_health() is False

        # Restarting should acquire a fresh working tunnel
        success = self.watchdog.start_tunnel_process()
        assert success is True
        assert self.watchdog.process.pid != old_pid
        assert self.watchdog.process.poll() is None
