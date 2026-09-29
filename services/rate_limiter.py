import asyncio
import time
import random
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Telegram allows about 20 messages per minute in one group/channel: one post every ~3 seconds per chat
DEFAULT_MIN_INTERVAL = 3.0
DEFAULT_MAX_INTERVAL = 3.6


class SmartDelayEngine:
    """
    Intelligent rate limiter and anti-ban delay supervisor.
    Ensures posts are published with natural pacing to avoid Telegram FloodWait.
    Uses per-channel locking so different channels never block each other.

    Every chat gets one budget no matter how it is spelled (@name, t.me link, -100 id, raw id): posts to the
    same chat are spaced by min_delay..max_delay seconds per message they contain (an album of N items
    reserves N slots).
    """
    def __init__(self, min_delay: float = DEFAULT_MIN_INTERVAL, max_delay: float = DEFAULT_MAX_INTERVAL):
        self.min_delay = min_delay
        self.max_delay = max(max_delay, min_delay)
        self._last_post_time: Dict[str, float] = {}
        # Earliest monotonic time the next message may be sent to the chat
        self._next_allowed: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        # Callers that fetched a chat lock and have not released it yet (a lock in use must never be pruned)
        self._lock_users: Dict[str, int] = {}
        self._map_lock = asyncio.Lock()

    @staticmethod
    def _normalize_channel_key(channel: Any) -> str:
        ch = str(channel).strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if ch.startswith(prefix):
                ch = ch[len(prefix):]
                break
        if ch.startswith("+") or ch.startswith("joinchat/"):
            # Private invite hashes are case-sensitive
            return ch
        ch = ch.lstrip("@").strip()
        # Numeric chat ids in any stored form (-1001234, -1234, 1234) share one key
        digits = ch[1:] if ch.startswith("-") else ch
        if digits.isdigit():
            if digits.startswith("100") and ch.startswith("-100") and len(digits) > 3:
                digits = digits[3:]
            return f"id:{int(digits)}"
        return ch.lower()

    @classmethod
    def _slot_key(cls, target_channel: Any, peer_id: Any = None) -> str:
        if peer_id is not None and str(peer_id).strip():
            key = cls._normalize_channel_key(peer_id)
            if key.startswith("id:"):
                return key
        return cls._normalize_channel_key(target_channel)

    async def _get_channel_lock(self, channel: str) -> asyncio.Lock:
        async with self._map_lock:
            if channel not in self._locks:
                self._locks[channel] = asyncio.Lock()
            return self._locks[channel]

    async def _acquire_channel_lock(self, channel: str) -> asyncio.Lock:
        """Fetches the chat lock and registers the caller as a user of it in one step, so prune_stale_locks can
        never drop a lock between "fetched" and "acquired" (two callers would then hold different locks)."""
        async with self._map_lock:
            lock = self._locks.get(channel)
            if lock is None:
                lock = self._locks[channel] = asyncio.Lock()
            self._lock_users[channel] = self._lock_users.get(channel, 0) + 1
            return lock

    def _release_lock_user(self, channel: str):
        count = self._lock_users.get(channel, 0) - 1
        if count > 0:
            self._lock_users[channel] = count
        else:
            self._lock_users.pop(channel, None)

    async def wait_for_slot(self, target_channel: Any, peer_id: Any = None, cost: int = 1):
        """Waits until the chat may receive the next post. `peer_id` (the pair's numeric target id) makes all
        spellings of a chat share one budget; `cost` is the number of messages about to be sent (album items)."""
        norm = self._slot_key(target_channel, peer_id)
        channel_lock = await self._acquire_channel_lock(norm)
        try:
            async with channel_lock:
                # A timer may fire slightly early (Windows clock granularity is ~15 ms): wait until the
                # slot is really due, so the per-chat spacing is never undercut
                while True:
                    remaining = self._next_allowed.get(norm, 0.0) - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(remaining)
                sent_at = time.monotonic()
                self._last_post_time[norm] = sent_at
                interval = random.uniform(self.min_delay, self.max_delay)
                self._next_allowed[norm] = sent_at + interval * max(1, int(cost or 1))
        finally:
            self._release_lock_user(norm)

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
                if self._lock_users.get(ch):
                    continue
                if lock is None or not lock.locked():
                    self._locks.pop(ch, None)
                    self._last_post_time.pop(ch, None)
                    self._next_allowed.pop(ch, None)
            orphan_channels = [
                ch for ch, lock in list(self._locks.items())
                if ch not in self._last_post_time and not lock.locked() and not self._lock_users.get(ch)
            ]
            for ch in orphan_channels:
                self._locks.pop(ch, None)
                self._next_allowed.pop(ch, None)

rate_limiter = SmartDelayEngine()
