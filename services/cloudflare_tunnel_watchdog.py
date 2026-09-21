#!/usr/bin/env python3
"""
ChannelCloner Pro — 24/7 Cloudflare Tunnel Self-Healing Watchdog
Maintains 24/7 uninterrupted availability of the Telegram Mini App via Cloudflare.
Monitors tunnel liveness, automatically heals/restarts on edge disconnection,
and synchronizes active HTTPS URL across Bot settings, keyboards, and MySQL.
"""

import os
import sys
import re
import time
import json
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
import requests

logger = logging.getLogger("CloudflareWatchdog")

class CloudflareTunnelWatchdog:
    def __init__(
        self,
        local_target: Optional[str] = None,
        probe_interval: int = 15,
        max_probe_failures: int = 3,
        metrics_port: int = 20261,
        token: Optional[str] = None
    ):
        self.local_target = local_target or os.getenv("MINIAPP_TARGET", "http://127.0.0.1:8089")
        self.probe_interval = probe_interval
        self.max_probe_failures = max_probe_failures
        self.metrics_port = metrics_port
        self.token = token or os.getenv("CLOUDFLARE_TUNNEL_TOKEN")
        self.custom_domain = os.getenv("CLOUDFLARE_TUNNEL_DOMAIN")
        
        self.process: Optional[subprocess.Popen] = None
        self.active_url: Optional[str] = None
        self.started_at: float = 0
        self.restarts_count: int = 0
        self.consecutive_failures: int = 0
        self.is_running: bool = False

        self.data_dir = Path(__file__).parent.parent / "data"
        self.logs_dir = Path(__file__).parent.parent / "logs"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.url_file = self.data_dir / "active_tunnel_url.txt"
        self.status_file = self.logs_dir / "tunnel_status.json"

    def _save_active_url(self, url: str):
        """Persists the active URL to file and runtime configuration."""
        self.active_url = url
        try:
            self.url_file.write_text(url.strip(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not write active tunnel URL file: {e}")

        # Update environment variable in-memory
        os.environ["WEBAPP_URL"] = url

        # Update config.settings in-memory if imported
        try:
            from config.settings import settings
            settings.WEBAPP_URL = url
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        # Update MySQL bot_settings table
        try:
            import pymysql
            host = os.getenv("MYSQL_HOST", "127.0.0.1")
            port = int(os.getenv("MYSQL_PORT", "3306"))
            conn = pymysql.connect(
                host=host,
                port=port,
                user=os.getenv("MYSQL_USER", "cloner_user"),
                password=os.getenv("MYSQL_PASSWORD", "cloner_pass_2026"),
                database=os.getenv("MYSQL_DATABASE", "channelcloner"),
                charset="utf8mb4",
                autocommit=True
            )
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO `bot_settings` (`key_name`, `value_text`) VALUES ('webapp_url', %s) "
                    "ON DUPLICATE KEY UPDATE `value_text` = VALUES(`value_text`);",
                    (url,)
                )
            conn.close()
            logger.info(f"Synchronized active URL ({url}) to MySQL bot_settings table.")
        except Exception as e:
            logger.debug(f"MySQL bot_settings sync skipped: {e}")

    def _write_status(self, status: str, details: str = "", http_code: Optional[int] = None):
        """Dumps current watchdog state to logs/tunnel_status.json."""
        data = {
            "status": status,
            "active_url": self.active_url,
            "local_target": self.local_target,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "restarts_count": self.restarts_count,
            "consecutive_failures": self.consecutive_failures,
            "last_probe_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_http_code": http_code,
            "details": details,
            "mode": "named_token" if self.token else "quick_tunnel"
        }
        try:
            self.status_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    def start_tunnel_process(self) -> bool:
        """Starts cloudflared and monitors until the active HTTPS URL is acquired."""
        self._kill_existing_process()
        self.consecutive_failures = 0

        # Command determination
        if self.token:
            cmd = ["cloudflared", "tunnel", "run", "--token", self.token, "--metrics", f"127.0.0.1:{self.metrics_port}"]
            logger.info("Starting Cloudflare Named Tunnel using TOKEN...")
        else:
            cmd = ["cloudflared", "tunnel", "--url", self.local_target, "--metrics", f"127.0.0.1:{self.metrics_port}", "--no-autoupdate"]
            logger.info(f"Starting Cloudflare Quick Tunnel pointing to {self.local_target} (metrics: {self.metrics_port})...")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace"
            )
            self.started_at = time.time()
        except FileNotFoundError:
            logger.error("cloudflared binary not found in PATH! Ensure cloudflared is installed.")
            self._write_status("error", "cloudflared executable not found in PATH")
            return False
        except Exception as e:
            logger.error(f"Failed to spawn cloudflared process: {e}")
            self._write_status("error", str(e))
            return False

        # If named tunnel with custom domain
        if self.token and self.custom_domain:
            self._save_active_url(f"https://{self.custom_domain}")
            self._write_status("running", f"Bound to {self.active_url}")
            return True

        # Quick tunnel: read lines to find https://*.trycloudflare.com
        logger.info("Awaiting Cloudflare edge URL assignment...")
        url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        timeout_seconds = 35
        start_wait = time.time()

        while time.time() - start_wait < timeout_seconds:
            if self.process.poll() is not None:
                logger.error("cloudflared process terminated prematurely during startup.")
                return False

            line = self.process.stdout.readline()
            if not line:
                time.sleep(0.2)
                continue

            line = line.strip()
            if "trycloudflare.com" in line:
                match = url_pattern.search(line)
                if match:
                    assigned_url = match.group(0)
                    logger.info(f"🚀 Cloudflare assigned live URL: {assigned_url}")
                    self._save_active_url(assigned_url)
                    self._write_status("verifying", "Edge assigned, awaiting edge ready signal")
                    
                    # Verify edge connection via Cloudflare metrics ready probe
                    self._wait_for_edge_ready(timeout=10)
                    self._write_status("healthy", "Edge verified and responsive", http_code=200)
                    return True

        logger.warning("Could not extract trycloudflare.com URL within timeout window.")
        return False

    def _wait_for_edge_ready(self, timeout: float = 10.0) -> bool:
        """Polls cloudflared local /ready endpoint until edge is connected."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"http://127.0.0.1:{self.metrics_port}/ready", timeout=1.5)
                if r.status_code == 200:
                    logger.info("Cloudflare edge connections verified and ready.")
                    return True
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            time.sleep(0.5)
        return False

    def _verify_edge_connectivity(self, url: str, max_retries: int = 5) -> bool:
        """Pings the public URL with backoff to confirm Cloudflare edge routing is ready."""
        for i in range(max_retries):
            try:
                r = requests.get(f"{url}/api/system.php", timeout=3.0, headers={"User-Agent": "CloudflareWatchdog/1.0"})
                if r.status_code == 200:
                    logger.info(f"Edge connectivity verified! HTTP {r.status_code} in {i+1} attempts.")
                    return True
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            time.sleep(1.0)
        return False

    def probe_health(self) -> bool:
        """Performs active health probes to test tunnel and backend health."""
        if not self.process or self.process.poll() is not None:
            logger.warning("Watchdog detected: cloudflared process is dead.")
            self.consecutive_failures += 1
            self._write_status("dead", "cloudflared process terminated")
            return False

        if not self.active_url:
            self.consecutive_failures += 1
            return False

        # 1. Cloudflare Official Ready Endpoint
        edge_ready = False
        try:
            r = requests.get(f"http://127.0.0.1:{self.metrics_port}/ready", timeout=2.5)
            if r.status_code == 200:
                edge_ready = True
            else:
                logger.warning(f"Cloudflare edge not ready: HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Could not reach cloudflared metrics endpoint: {e}")

        # 2. Local Backend Endpoint (PHP + MySQL)
        backend_healthy = False
        try:
            r = requests.get(f"{self.local_target}/api/system.php", timeout=3.0)
            if r.status_code == 200:
                backend_healthy = True
            else:
                logger.warning(f"Local backend unhealthy: HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Could not reach local backend: {e}")

        # 3. Public Edge Endpoint (Best-effort check)
        public_healthy = False
        try:
            r = requests.get(f"{self.active_url}/api/system.php", timeout=3.0)
            if r.status_code == 200:
                public_healthy = True
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        if (edge_ready and backend_healthy) or public_healthy:
            self.consecutive_failures = 0
            details = "Edge ready & backend healthy"
            if public_healthy:
                details += " (public verified)"
            self._write_status("healthy", details, http_code=200)
            return True

        self.consecutive_failures += 1
        self._write_status("degraded", f"edge_ready={edge_ready}, backend_healthy={backend_healthy}")
        return False

    def _kill_existing_process(self):
        """Safely terminates any previously running cloudflared process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            self.process = None

    def run_forever(self):
        """Main 24/7 execution loop: monitors, probes, and auto-heals."""
        self.is_running = True
        logger.info("🛡️ Cloudflare 24/7 Self-Healing Watchdog started.")

        while self.is_running:
            # If no active process or URL, initialize
            if not self.process or self.process.poll() is not None or not self.active_url:
                logger.info("Initializing / Restarting Cloudflare Tunnel...")
                success = self.start_tunnel_process()
                if not success:
                    self.restarts_count += 1
                    logger.warning("Startup attempt failed. Backing off 5 seconds...")
                    time.sleep(5)
                    continue

            # Sleep until next probe interval
            time.sleep(self.probe_interval)

            # Perform probe
            is_healthy = self.probe_health()

            if not is_healthy:
                logger.warning(f"⚠️ Health probe failure ({self.consecutive_failures}/{self.max_probe_failures})")
                if self.consecutive_failures >= self.max_probe_failures:
                    logger.error("🚨 Tunnel unresponsive for max consecutive checks! Triggering immediate self-heal recycle...")
                    self.restarts_count += 1
                    self._kill_existing_process()
                    self.active_url = None
                    self._write_status("recovering", "Threshold exceeded, recycling tunnel")
                    time.sleep(2)

    def stop(self):
        """Stops watchdog and kills child process."""
        self.is_running = False
        self._kill_existing_process()
        self._write_status("stopped", "Watchdog terminated gracefully")
        logger.info("Cloudflare Watchdog stopped.")

# Singleton instance
cloudflare_watchdog = CloudflareTunnelWatchdog()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [CloudflareWatchdog] %(message)s"
    )
    try:
        cloudflare_watchdog.run_forever()
    except KeyboardInterrupt:
        cloudflare_watchdog.stop()
        print("\nShutdown complete.")
