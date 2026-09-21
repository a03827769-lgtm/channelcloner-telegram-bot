import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional, Dict
import psutil

logger = logging.getLogger(__name__)

class HardwareAwareRenderQueue:
    """
    Hardware-Aware Queue that throttles and serializes CPU/RAM intensive tasks (like FFmpeg video encoding).
    Prevents Linux OOM (Out Of Memory) killer and keeps system responsiveness intact.
    """

    def __init__(self, max_concurrent: int = 1, memory_threshold_percent: float = 80.0):
        self.max_concurrent = max_concurrent
        self.memory_threshold_percent = memory_threshold_percent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_renders = 0
        self._total_queued = 0
        self._total_completed = 0
        self._lock = asyncio.Lock()

    def get_memory_usage(self) -> float:
        """Returns current system virtual memory percentage (0-100)"""
        try:
            return psutil.virtual_memory().percent
        except Exception:
            return 0.0

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
        return {
            "active_renders": self._active_renders,
            "total_queued": self._total_queued,
            "total_completed": self._total_completed,
            "max_concurrent": self.max_concurrent,
            "ram_percent": self.get_memory_usage()
        }

render_queue = HardwareAwareRenderQueue(max_concurrent=1, memory_threshold_percent=80.0)
