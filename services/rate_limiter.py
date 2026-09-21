import asyncio
import time
import random
import logging
from typing import Dict

logger = logging.getLogger(__name__)

class SmartDelayEngine:
    """
    Intelligent rate limiter and anti-ban delay supervisor.
    Ensures posts are published with natural pacing to avoid Telegram FloodWait.
    Uses per-channel locking so different channels never block each other.
    """
    def __init__(self, min_delay: float = 0.5, max_delay: float = 1.5):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_post_time: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._map_lock = asyncio.Lock()

    @staticmethod
    def _normalize_channel_key(channel: str) -> str:
        ch = str(channel).strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if ch.startswith(prefix):
                ch = ch[len(prefix):]
                break
        if not ch.startswith("+") and not ch.startswith("joinchat/"):
            ch = ch.lstrip("@").strip().lower()
        return ch

    async def _get_channel_lock(self, channel: str) -> asyncio.Lock:
        async with self._map_lock:
            if channel not in self._locks:
                self._locks[channel] = asyncio.Lock()
            return self._locks[channel]

    async def wait_for_slot(self, target_channel: str):
        """Enforces a smooth, natural pacing delay before posting to target_channel"""
        norm = self._normalize_channel_key(target_channel)
        channel_lock = await self._get_channel_lock(norm)
        async with channel_lock:
            now_mono = time.monotonic()
            now_wall = time.time()
            last_time = self._last_post_time.get(norm, 0.0)
            elapsed = (now_wall - last_time) if last_time > 1e8 else (now_mono - last_time)

            # Generate natural jitter delay
            target_delay = random.uniform(self.min_delay, self.max_delay)

            if elapsed < target_delay:
                sleep_needed = target_delay - elapsed
                await asyncio.sleep(sleep_needed)

            self._last_post_time[norm] = time.monotonic()

    async def prune_stale_locks(self, max_idle_seconds: float = 3600.0):
        """Prunes channels that have not posted in max_idle_seconds to prevent memory leak"""
        async with self._map_lock:
            now_mono = time.monotonic()
            now_wall = time.time()
            stale = [
                ch for ch, t in list(self._last_post_time.items())
                if ((now_wall - t) if t > 1e8 else (now_mono - t)) > max_idle_seconds
            ]
            for ch in stale:
                lock = self._locks.get(ch)
                if lock and not lock.locked():
                    self._locks.pop(ch, None)
                    self._last_post_time.pop(ch, None)
            orphan_channels = [ch for ch, lock in list(self._locks.items()) if ch not in self._last_post_time and not lock.locked()]
            for ch in orphan_channels:
                self._locks.pop(ch, None)

rate_limiter = SmartDelayEngine()

