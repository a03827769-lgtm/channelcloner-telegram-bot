#!/usr/bin/env python3
"""
ChannelCloner Pro — 24/7 Live Tunnel URL Synchronizer & Edge Watchdog
Automatically discovers the live Cloudflare HTTPS URL from container metrics / logs / file,
updates MySQL `bot_settings`, and sets the Telegram Bot Chat Menu Button to WebApp 24/7.
"""

import os
import re
import time
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from config.settings import settings

logger = logging.getLogger("TunnelSyncService")

class TunnelSyncService:
    def __init__(self, check_interval: int = 12):
        self.check_interval = check_interval
        self.active_url: Optional[str] = None
        self.is_running: bool = False
        self.consecutive_failures: int = 0
        self.max_failures: int = 3
        self.last_sync_time: float = 0
        self.last_probe_status: Optional[int] = None
        self.bot_menu_synced_url: Optional[str] = None

        self.data_dir = Path(__file__).parent.parent / "data"
        self.logs_dir = Path(__file__).parent.parent / "logs"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.url_file = self.data_dir / "active_tunnel_url.txt"
        self.status_file = self.logs_dir / "tunnel_status.json"

    async def fetch_url_from_metrics(self) -> Optional[str]:
        """Queries cloudflared Prometheus metrics endpoints to extract assigned userHostname"""
        candidates = [
            "http://cloudflared:20241/metrics",
            "http://127.0.0.1:20241/metrics",
            "http://127.0.0.1:20261/metrics",
            "http://localhost:20241/metrics"
        ]
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5)) as session:
            for endpoint in candidates:
                try:
                    async with session.get(endpoint) as resp:
                        if resp.status == 200:
                            body = await resp.text()
                            match = re.search(r'cloudflared_tunnel_user_hostnames_counts\{userHostname="(https://[^"]+)"\}', body)
                            if match:
                                return match.group(1).rstrip('/')
                except Exception:
                    continue
        return None

    def read_saved_url(self) -> Optional[str]:
        """Reads persisted URL from data/active_tunnel_url.txt"""
        if self.url_file.exists():
            try:
                url = self.url_file.read_text(encoding="utf-8").strip()
                if url.startswith("https://"):
                    return url.rstrip('/')
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return None

    async def fetch_url_from_mysql(self) -> Optional[str]:
        """Reads active URL from MySQL bot_settings table"""
        def _read_db():
            try:
                import pymysql
                host = os.getenv("MYSQL_HOST", "127.0.0.1")
                port = int(os.getenv("MYSQL_PORT", "3307" if host in ("127.0.0.1", "localhost") else "3306"))
                conn = pymysql.connect(
                    host=host,
                    port=port,
                    user=os.getenv("MYSQL_USER", "cloner_user"),
                    password=os.getenv("MYSQL_PASSWORD", "cloner_pass_2026"),
                    database=os.getenv("MYSQL_DATABASE", "channelcloner"),
                    charset="utf8mb4"
                )
                with conn.cursor() as cur:
                    cur.execute("SELECT value_text FROM bot_settings WHERE key_name = 'webapp_url' LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0] and row[0].startswith("https://"):
                        return row[0].strip().rstrip('/')
                conn.close()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            return None

        return await asyncio.to_thread(_read_db)

    async def persist_active_url(self, url: str, bot=None):
        """Persists detected URL to file, MySQL, settings, and Telegram Bot Menu Button"""
        url = url.strip().rstrip('/')
        self.active_url = url
        self.last_sync_time = time.time()

        # 1. Write to data/active_tunnel_url.txt
        try:
            self.url_file.write_text(url, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write url_file: {e}")

        # 2. Update config.settings in-memory
        try:
            settings.WEBAPP_URL = url
            os.environ["WEBAPP_URL"] = url
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

        # 3. Update MySQL bot_settings
        def _save_db(target_url):
            try:
                import pymysql
                host = os.getenv("MYSQL_HOST", "127.0.0.1")
                port = int(os.getenv("MYSQL_PORT", "3307" if host in ("127.0.0.1", "localhost") else "3306"))
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
                        "INSERT INTO bot_settings (key_name, value_text) VALUES ('webapp_url', %s) "
                        "ON DUPLICATE KEY UPDATE value_text = %s",
                        (target_url, target_url)
                    )
                conn.close()
            except Exception as e_db:
                logger.debug(f"MySQL bot_settings write skipped: {e_db}")

        await asyncio.to_thread(_save_db, url)

        # 4. Update Telegram Bot Chat Menu Button (Default & Admins)
        if bot and self.bot_menu_synced_url != url:
            try:
                from aiogram.types import MenuButtonWebApp, WebAppInfo
                from time import time
                target_url = url
                cache_buster_url = f"{target_url}?v={int(time())}"
                menu_btn = MenuButtonWebApp(
                    text="📱 Mini App",
                    web_app=WebAppInfo(url=cache_buster_url)
                )
                
                # Set as global default menu button
                await bot.set_chat_menu_button(menu_button=menu_btn)
                logger.info(f"Telegram global chat_menu_button updated to: {url}")

                # Set explicitly for each configured admin
                for admin_id in getattr(settings, "admin_ids", set()):
                    try:
                        await bot.set_chat_menu_button(chat_id=admin_id, menu_button=menu_btn)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

                self.bot_menu_synced_url = url
            except Exception as e_btn:
                logger.warning(f"Could not set Telegram chat menu button: {e_btn}")

    async def probe_endpoint(self, url: str) -> bool:
        """Pings public edge /api/system.php to verify live routing"""
        probe_url = f"{url}/api/system.php"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0)) as session:
                headers = {"User-Agent": "ChannelCloner-Watchdog/2.0"}
                async with session.get(probe_url, headers=headers) as resp:
                    self.last_probe_status = resp.status
                    return resp.status == 200
        except Exception:
            self.last_probe_status = None
            return False

    def write_status(self, status: str, details: str = ""):
        """Dumps health and telemetry to status file"""
        data = {
            "status": status,
            "active_url": self.active_url,
            "last_probe_status": self.last_probe_status,
            "consecutive_failures": self.consecutive_failures,
            "last_sync_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_sync_time)) if self.last_sync_time else None,
            "details": details,
            "timestamp": time.time()
        }
        try:
            self.status_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            (self.data_dir / "tunnel_status.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    async def start_worker(self, bot=None):
        """24/7 background worker running continuously alongside Telegram Bot"""
        self.is_running = True
        logger.info("🚀 TunnelSyncService: 24/7 Tunnel Synchronizer worker started.")

        # Initial fast sync
        candidate_url = await self.fetch_url_from_metrics()
        if not candidate_url:
            candidate_url = self.read_saved_url() or await self.fetch_url_from_mysql()
        if candidate_url:
            await self.persist_active_url(candidate_url, bot)

        while self.is_running:
            try:
                # 1. Check for live URL from cloudflared metrics
                live_url = await self.fetch_url_from_metrics()
                
                # Fallbacks if metrics not ready
                if not live_url:
                    live_url = self.read_saved_url() or await self.fetch_url_from_mysql()

                if live_url:
                    # If URL changed or menu button not yet set
                    if live_url != self.active_url or self.bot_menu_synced_url != live_url:
                        logger.info(f"🔄 TunnelSyncService: New live tunnel URL detected: {live_url}")
                        await self.persist_active_url(live_url, bot)

                    # 2. Probe health
                    healthy = await self.probe_endpoint(live_url)
                    if healthy:
                        self.consecutive_failures = 0
                        self.write_status("healthy", f"Edge verified and responsive at {live_url}")
                    else:
                        self.consecutive_failures += 1
                        self.write_status("degraded", f"Probe failed ({self.consecutive_failures}/{self.max_failures})")
                        logger.warning(f"Tunnel probe degraded: {live_url} (fails: {self.consecutive_failures})")
                else:
                    self.write_status("waiting", "Awaiting Cloudflare tunnel URL assignment...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TunnelSyncService cycle: {e}")

            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

        self.is_running = False
        logger.info("TunnelSyncService worker terminated.")

tunnel_sync_service = TunnelSyncService()
