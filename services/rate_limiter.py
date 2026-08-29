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
    def __init__(self, min_delay: float = 0.3, max_delay: float = 1.0):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_post_time: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._map_lock = asyncio.Lock()

    async def _get_channel_lock(self, channel: str) -> asyncio.Lock:
        async with self._map_lock:
            if channel not in self._locks:
                self._locks[channel] = asyncio.Lock()
            return self._locks[channel]

    async def wait_for_slot(self, target_channel: str):
        """Enforces a smooth, natural pacing delay before posting to target_channel"""
        channel_lock = await self._get_channel_lock(target_channel)
        async with channel_lock:
            now = time.time()
            last_time = self._last_post_time.get(target_channel, 0.0)
            elapsed = now - last_time

            # Generate natural jitter delay
            target_delay = random.uniform(self.min_delay, self.max_delay)

            if elapsed < target_delay:
                sleep_needed = target_delay - elapsed
                await asyncio.sleep(sleep_needed)

            self._last_post_time[target_channel] = time.time()

rate_limiter = SmartDelayEngine()

