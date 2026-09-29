#!/usr/bin/env python3
"""
ChannelCloner Pro — Mini App URL synchronizer & edge watchdog.

Production publishes the Mini App through a named Cloudflare tunnel on a permanent domain
(WEBAPP_URL=https://app... in .env), so the Telegram menu button simply points at WEBAPP_URL and the
worker only probes the public /health endpoint and writes the status file.

Only when WEBAPP_URL is not a permanent https URL (local development with an ad-hoc cloudflared quick
tunnel) the legacy discovery runs as a fallback: the *.trycloudflare.com URL is read from the cloudflared
metrics endpoint or from data/active_tunnel_url.txt, and the menu button follows it when it changes.
The retired PHP/MySQL Mini App stack is no longer written to.
"""

import os
import re
import time
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from config.settings import settings, PROJECT_ROOT

logger = logging.getLogger("TunnelSyncService")

QUICK_TUNNEL_MARKER = "trycloudflare.com"
MENU_BUTTON_TEXT = "📱 Mini App"


def is_permanent_webapp_url(url: Optional[str]) -> bool:
    """A named-tunnel / own-domain https URL (quick tunnel URLs change on every cloudflared restart)."""
    value = (url or "").strip()
    return value.startswith("https://") and len(value) > len("https://") + 3 and QUICK_TUNNEL_MARKER not in value


class TunnelSyncService:
    def __init__(self, check_interval: int = 12, probe_interval: int = 120, base_dir: Optional[Path] = None):
        self.check_interval = check_interval      # quick-tunnel discovery cadence (fallback mode)
        self.probe_interval = probe_interval      # public health probe cadence (permanent URL)
        self.active_url: Optional[str] = None
        self.is_running: bool = False
        self.consecutive_failures: int = 0
        self.max_failures: int = 3
        self.last_sync_time: float = 0
        self.last_probe_status: Optional[int] = None
        self.bot_menu_synced_url: Optional[str] = None
        self._configured_url: Optional[str] = None

        base = Path(base_dir) if base_dir else PROJECT_ROOT
        self.data_dir = base / "data"
        self.logs_dir = base / "logs"
        self.url_file = self.data_dir / "active_tunnel_url.txt"
        self.status_file = self.logs_dir / "tunnel_status.json"

    # --- URL SOURCES ---------------------------------------------------------------------

    @staticmethod
    def configured_url() -> Optional[str]:
        """WEBAPP_URL (settings loaded from .env, or the process environment) when it is permanent."""
        for candidate in (getattr(settings, "WEBAPP_URL", "") or "", os.getenv("WEBAPP_URL", "") or ""):
            if is_permanent_webapp_url(candidate):
                return candidate.strip().rstrip("/")
        return None

    async def fetch_url_from_metrics(self) -> Optional[str]:
        """Queries cloudflared Prometheus metrics endpoints to extract the assigned quick-tunnel hostname"""
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
        """Reads the last discovered quick-tunnel URL from data/active_tunnel_url.txt"""
        if self.url_file.exists():
            try:
                url = self.url_file.read_text(encoding="utf-8").strip()
                if url.startswith("https://"):
                    return url.rstrip('/')
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return None

    async def discover_quick_tunnel_url(self) -> Optional[str]:
        return await self.fetch_url_from_metrics() or self.read_saved_url()

    # --- PUBLISHING ----------------------------------------------------------------------

    async def sync_menu_button(self, url: str, bot=None) -> bool:
        """Points the Telegram chat menu button (global default and each super admin) at `url`."""
        if bot is None:
            return False
        if self.bot_menu_synced_url == url:
            return True
        try:
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            # Cache buster: Telegram clients cache the Mini App page aggressively per URL
            separator = "&" if "?" in url else "?"
            menu_btn = MenuButtonWebApp(
                text=MENU_BUTTON_TEXT,
                web_app=WebAppInfo(url=f"{url}{separator}v={int(time.time())}")
            )
            await bot.set_chat_menu_button(menu_button=menu_btn)
            logger.info(f"Telegram global chat_menu_button updated to: {url}")

            # Admins may carry an older chat-specific button; overwrite it as well
            for admin_id in getattr(settings, "admin_ids", set()):
                try:
                    await bot.set_chat_menu_button(chat_id=admin_id, menu_button=menu_btn)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            self.bot_menu_synced_url = url
            return True
        except Exception as e_btn:
            logger.warning(f"Could not set Telegram chat menu button: {e_btn}")
            return False

    async def persist_active_url(self, url: str, bot=None, legacy_publish: bool = False):
        """Records the active Mini App URL and syncs the menu button. With legacy_publish (quick-tunnel
        fallback) the URL is also written to data/active_tunnel_url.txt and the in-memory settings, which
        is where the bot keyboards look for a quick-tunnel URL."""
        url = url.strip().rstrip('/')
        self.active_url = url
        self.last_sync_time = time.time()

        if legacy_publish:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.url_file.write_text(url, encoding="utf-8")
            except Exception as e:
                logger.warning(f"Failed to write url_file: {e}")
            try:
                settings.WEBAPP_URL = url
                os.environ["WEBAPP_URL"] = url
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        await self.sync_menu_button(url, bot)

    async def probe_endpoint(self, url: str) -> bool:
        """Pings the public edge /health to verify live routing"""
        probe_url = f"{url}/health"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6.0)) as session:
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
            "mode": "permanent" if self._configured_url else "quick_tunnel",
            "last_probe_status": self.last_probe_status,
            "consecutive_failures": self.consecutive_failures,
            "last_sync_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_sync_time)) if self.last_sync_time else None,
            "details": details,
            "timestamp": time.time()
        }
        payload = json.dumps(data, indent=2)
        for target in (self.status_file, self.data_dir / "tunnel_status.json"):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

    # --- WORKER --------------------------------------------------------------------------

    async def sync_once(self, bot=None) -> Optional[str]:
        """One synchronization cycle; returns the Mini App URL in use (None while none is known)."""
        if self._configured_url:
            url = self._configured_url
            if url != self.active_url or self.bot_menu_synced_url != url:
                await self.persist_active_url(url, bot)
        else:
            url = await self.discover_quick_tunnel_url()
            if not url:
                self.write_status("waiting", "WEBAPP_URL is not a permanent https URL; awaiting a Cloudflare quick tunnel URL...")
                return None
            if url != self.active_url or self.bot_menu_synced_url != url:
                logger.info(f"🔄 TunnelSyncService: quick tunnel URL detected: {url}")
                await self.persist_active_url(url, bot, legacy_publish=True)

        if await self.probe_endpoint(url):
            self.consecutive_failures = 0
            self.write_status("healthy", f"Edge verified and responsive at {url}")
        else:
            self.consecutive_failures += 1
            self.write_status("degraded", f"Probe failed ({self.consecutive_failures}/{self.max_failures})")
            logger.warning(f"Tunnel probe degraded: {url} (fails: {self.consecutive_failures})")
        return url

    async def start_worker(self, bot=None):
        """24/7 background worker running continuously alongside the Telegram bot"""
        self.is_running = True
        # Captured once: other components may later overwrite settings.WEBAPP_URL in memory
        self._configured_url = self.configured_url()
        if self._configured_url:
            logger.info(f"🚀 TunnelSyncService: Mini App published at {self._configured_url} (WEBAPP_URL).")
        else:
            logger.info("🚀 TunnelSyncService: WEBAPP_URL is not a permanent https URL; using quick tunnel discovery.")

        while self.is_running:
            try:
                await self.sync_once(bot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TunnelSyncService cycle: {e}")

            try:
                await asyncio.sleep(self.probe_interval if self._configured_url else self.check_interval)
            except asyncio.CancelledError:
                break

        self.is_running = False
        logger.info("TunnelSyncService worker terminated.")


tunnel_sync_service = TunnelSyncService()
