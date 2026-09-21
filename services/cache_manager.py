import asyncio
import os
import time
import logging
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Any, Tuple, Iterable

logger = logging.getLogger(__name__)
# Live Auto-Reload verified: hot-reload engine active

class LRUSet:
    """Fixed-size in-memory set that evicts oldest items to prevent memory unbounded growth"""
    def __init__(self, maxsize: int = 150000):
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_lock(self) -> asyncio.Lock:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._lock is None or self._lock_loop != curr_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = curr_loop
        return self._lock

    async def add(self, key: Tuple[int, int]):
        async with self._get_lock():
            if key in self._data:
                self._data.move_to_end(key)
                return
            self._data[key] = True
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def add_batch(self, keys: Iterable[Tuple[int, int]]):
        async with self._get_lock():
            for key in keys:
                if key in self._data:
                    self._data.move_to_end(key)
                else:
                    self._data[key] = True
                    if len(self._data) > self.maxsize:
                        self._data.popitem(last=False)

    async def contains(self, key: Tuple[int, int]) -> bool:
        async with self._get_lock():
            if key in self._data:
                self._data.move_to_end(key)
                return True
            return False

class TTLCache:
    """In-memory key-value cache with TTL expiration"""
    def __init__(self, default_ttl: float = 60.0):
        self.default_ttl = default_ttl
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_lock(self) -> asyncio.Lock:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._lock is None or self._lock_loop != curr_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = curr_loop
        return self._lock

    async def get(self, key: str) -> Optional[Any]:
        async with self._get_lock():
            if key in self._data:
                exp, val = self._data[key]
                if time.time() < exp:
                    return val
                del self._data[key]
            return None

    async def set(self, key: str, value: Any, ttl: Optional[float] = None):
        async with self._get_lock():
            exp = time.time() + (ttl or self.default_ttl)
            self._data[key] = (exp, value)

    async def delete(self, key: str):
        async with self._get_lock():
            self._data.pop(key, None)

    async def prune_expired(self):
        async with self._get_lock():
            now = time.time()
            expired = [k for k, (exp, _) in self._data.items() if now >= exp]
            for k in expired:
                self._data.pop(k, None)

    async def clear(self):
        async with self._get_lock():
            self._data.clear()

class HighLoadCacheManager:
    def __init__(self):
        self.dedup_cache = LRUSet(maxsize=100000)
        self.sub_cache = TTLCache(default_ttl=45.0)
        self.settings_cache = TTLCache(default_ttl=120.0)
        self.seen_users_cache = TTLCache(default_ttl=300.0)
        
        self._media_sem: Optional[asyncio.Semaphore] = None
        self._media_sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._translate_sem: Optional[asyncio.Semaphore] = None
        self._translate_sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._cloner_sem: Optional[asyncio.Semaphore] = None
        self._cloner_sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._prune_task: Optional[asyncio.Task] = None

    @property
    def media_semaphore(self) -> asyncio.Semaphore:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._media_sem is None or self._media_sem_loop != curr_loop:
            cpu_count = os.cpu_count() or 2
            media_limit = max(2, min(4, cpu_count))
            self._media_sem = asyncio.Semaphore(media_limit)
            self._media_sem_loop = curr_loop
        return self._media_sem

    @property
    def translate_semaphore(self) -> asyncio.Semaphore:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._translate_sem is None or self._translate_sem_loop != curr_loop:
            self._translate_sem = asyncio.Semaphore(50)
            self._translate_sem_loop = curr_loop
        return self._translate_sem

    @property
    def cloner_semaphore(self) -> asyncio.Semaphore:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None
        if self._cloner_sem is None or self._cloner_sem_loop != curr_loop:
            self._cloner_sem = asyncio.Semaphore(100)
            self._cloner_sem_loop = curr_loop
        return self._cloner_sem

    def start_pruner(self):
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = asyncio.create_task(self._prune_loop())

    async def stop_pruner(self):
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass

    async def _prune_loop(self):
        while True:
            try:
                await asyncio.sleep(60.0)
                await self.sub_cache.prune_expired()
                await self.settings_cache.prune_expired()
                await self.seen_users_cache.prune_expired()
                try:
                    from services.rate_limiter import rate_limiter
                    await rate_limiter.prune_stale_locks()
                except Exception as err:
                    logger.error(f"Error pruning rate limiter locks: {err}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache pruner loop: {e}")
                await asyncio.sleep(5.0)

cache_manager = HighLoadCacheManager()
