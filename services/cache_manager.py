import asyncio
import time
import logging
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Any, Tuple

logger = logging.getLogger(__name__)

class LRUSet:
    """Fixed-size in-memory set that evicts oldest items to prevent memory unbounded growth"""
    def __init__(self, maxsize: int = 150000):
        self.maxsize = maxsize
        self._data = OrderedDict()
        self._lock = asyncio.Lock()

    async def add(self, key: Tuple[int, int]):
        async with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return
            self._data[key] = True
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def contains(self, key: Tuple[int, int]) -> bool:
        async with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return True
            return False

class TTLCache:
    """In-memory key-value cache with TTL expiration"""
    def __init__(self, default_ttl: float = 60.0):
        self.default_ttl = default_ttl
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            if key in self._data:
                exp, val = self._data[key]
                if time.time() < exp:
                    return val
                del self._data[key]
            return None

    async def set(self, key: str, value: Any, ttl: Optional[float] = None):
        async with self._lock:
            exp = time.time() + (ttl or self.default_ttl)
            self._data[key] = (exp, value)

    async def delete(self, key: str):
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._data.clear()

class HighLoadCacheManager:
    def __init__(self):
        self.dedup_cache = LRUSet(maxsize=200000)
        self.sub_cache = TTLCache(default_ttl=45.0)
        self.settings_cache = TTLCache(default_ttl=120.0)
        
        # Concurrency limiters to protect CPU and RAM
        self.media_semaphore = asyncio.Semaphore(25)
        self.translate_semaphore = asyncio.Semaphore(50)
        self.cloner_semaphore = asyncio.Semaphore(100)

cache_manager = HighLoadCacheManager()
