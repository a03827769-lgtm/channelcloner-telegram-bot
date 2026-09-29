import asyncio
import logging
import os
import time
from typing import Any, Callable, Coroutine, Optional, Dict, Tuple
import psutil

logger = logging.getLogger(__name__)

# cgroup v2 (unified) and v1 memory controller files; inside a container psutil.virtual_memory() reports the HOST
# memory, so a 512 MB container would look nearly empty until the kernel OOM-kills it.
_CGROUP_V2_DIR = "/sys/fs/cgroup"
_CGROUP_V1_DIR = "/sys/fs/cgroup/memory"
# cgroup v1 reports "no limit" as a huge page-aligned number
_CGROUP_UNLIMITED_THRESHOLD = 1 << 60
# Hosts with less memory than this render video stories in the reduced 720x1280 profile
LOW_MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
LOW_MEMORY_AVAILABLE_BYTES = 600 * 1024 * 1024


def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="ascii") as f:
            raw = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_stat_value(path: str, key: str) -> int:
    try:
        with open(path, "r", encoding="ascii") as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value.strip())
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return 0


def read_cgroup_memory() -> Optional[Tuple[int, int]]:
    """Returns (usage_bytes, limit_bytes) of the container memory cgroup, or None when no limit applies
    (bare metal / Windows / unlimited cgroup). Reclaimable page cache (inactive_file) is not counted as used,
    matching what `docker stats` shows."""
    # cgroup v2
    limit = _read_int_file(os.path.join(_CGROUP_V2_DIR, "memory.max"))
    usage = _read_int_file(os.path.join(_CGROUP_V2_DIR, "memory.current"))
    if limit and usage is not None:
        inactive = _read_stat_value(os.path.join(_CGROUP_V2_DIR, "memory.stat"), "inactive_file")
        return max(0, usage - inactive), limit
    # cgroup v1
    limit = _read_int_file(os.path.join(_CGROUP_V1_DIR, "memory.limit_in_bytes"))
    usage = _read_int_file(os.path.join(_CGROUP_V1_DIR, "memory.usage_in_bytes"))
    if limit and usage is not None and limit < _CGROUP_UNLIMITED_THRESHOLD:
        inactive = _read_stat_value(os.path.join(_CGROUP_V1_DIR, "memory.stat"), "total_inactive_file")
        return max(0, usage - inactive), limit
    return None


class HardwareAwareRenderQueue:
    """
    Hardware-Aware Queue that throttles and serializes CPU/RAM intensive tasks (like FFmpeg video encoding).
    Prevents Linux OOM (Out Of Memory) killer and keeps system responsiveness intact.
    The memory guard uses the container's cgroup limit when one exists, host memory otherwise.
    """

    def __init__(self, max_concurrent: int = 1, memory_threshold_percent: float = 80.0):
        self.max_concurrent = max_concurrent
        self.memory_threshold_percent = memory_threshold_percent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_renders = 0
        self._total_queued = 0
        self._total_completed = 0
        self._lock = asyncio.Lock()

    def get_memory_info(self) -> Dict[str, Any]:
        """Memory usage of the effective limit: {'percent', 'used', 'limit', 'available', 'source'}"""
        cg = read_cgroup_memory()
        if cg:
            used, limit = cg
            return {
                "percent": round(used / limit * 100.0, 1) if limit else 0.0,
                "used": used,
                "limit": limit,
                "available": max(0, limit - used),
                "source": "cgroup",
            }
        try:
            vm = psutil.virtual_memory()
            return {
                "percent": float(vm.percent),
                "used": int(vm.total - vm.available),
                "limit": int(vm.total),
                "available": int(vm.available),
                "source": "host",
            }
        except Exception:
            return {"percent": 0.0, "used": 0, "limit": 0, "available": 0, "source": "unknown"}

    def get_memory_usage(self) -> float:
        """Returns current memory usage percentage (0-100) of the container limit or of the host"""
        return float(self.get_memory_info().get("percent", 0.0))

    def is_low_memory_host(self) -> bool:
        """True on small containers / hosts where full HD video rendering risks the OOM killer"""
        info = self.get_memory_info()
        limit = info.get("limit") or 0
        available = info.get("available") or 0
        if limit and limit < LOW_MEMORY_LIMIT_BYTES:
            return True
        return bool(limit) and available < LOW_MEMORY_AVAILABLE_BYTES

    async def _wait_for_memory_headroom(self, max_wait_seconds: float = 30.0):
        """Pauses execution if RAM is above memory_threshold_percent until garbage collected"""
        start_time = time.time()
        while True:
            mem_pct = self.get_memory_usage()
            if mem_pct < self.memory_threshold_percent:
                return
            if time.time() - start_time > max_wait_seconds:
                logger.warning(f"Memory headroom wait timed out ({mem_pct}% > {self.memory_threshold_percent}%). Proceeding cautiously.")
                return
            logger.info(f"RAM high ({mem_pct:.1f}%). Throttling render queue for 2s...")
            await asyncio.sleep(2.0)

    async def run_render_job(self, coro_fn: Callable[..., Coroutine[Any, Any, Any]], *args, **kwargs) -> Any:
        """
        Executes an asynchronous rendering job through the concurrency semaphore and memory supervisor.
        """
        async with self._lock:
            self._total_queued += 1

        async with self._semaphore:
            async with self._lock:
                self._active_renders += 1

            try:
                # 1. Guard against memory spike
                await self._wait_for_memory_headroom()

                # 2. Execute render coroutine
                result = await coro_fn(*args, **kwargs)
                return result
            finally:
                async with self._lock:
                    self._active_renders = max(0, self._active_renders - 1)
                    self._total_completed += 1

    def get_status(self) -> Dict[str, Any]:
        """Returns diagnostic metrics for admin dashboard and healthchecks"""
        mem = self.get_memory_info()
        return {
            "active_renders": self._active_renders,
            "total_queued": self._total_queued,
            "total_completed": self._total_completed,
            "max_concurrent": self.max_concurrent,
            "ram_percent": mem.get("percent", 0.0),
            "memory_source": mem.get("source", "unknown")
        }


render_queue = HardwareAwareRenderQueue(max_concurrent=1, memory_threshold_percent=80.0)
