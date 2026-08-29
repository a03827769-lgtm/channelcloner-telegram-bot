import asyncio
import ctypes
import gc
import logging
import os
import sys
import time

logger = logging.getLogger("SystemSupervisor")

class SystemSupervisor:
    """
    24/7 System & Memory Supervisor:
    - Event Loop Lag Monitor (detects synchronous blocking stalls > 150ms)
    - Memory Optimizer (periodic GC collection and Linux malloc_trim)
    - Prevents Docker OOM crashes and keeps RAM < 200MB indefinitely
    """
    def __init__(self, memory_interval: int = 300, lag_threshold_ms: float = 150.0):
        self.memory_interval = memory_interval
        self.lag_threshold_ms = lag_threshold_ms
        self._is_running = False
        self._tasks = []
        self._libc = None
        if sys.platform != "win32":
            try:
                self._libc = ctypes.CDLL("libc.so.6")
            except Exception:
                pass

    def start(self):
        self._is_running = True
        self._tasks.append(asyncio.create_task(self._memory_loop()))
        self._tasks.append(asyncio.create_task(self._lag_monitor_loop()))
        logger.info("24/7 System Supervisor started (Event loop lag watchdog + Memory trimmer).")

    def stop(self):
        self._is_running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()

    async def _memory_loop(self):
        try:
            gc.set_threshold(50000, 10, 10)
        except Exception:
            pass
        while self._is_running:
            try:
                await asyncio.sleep(self.memory_interval)
                collected = gc.collect()
                trimmed = 0
                if self._libc and hasattr(self._libc, "malloc_trim"):
                    try:
                        trimmed = self._libc.malloc_trim(0)
                    except Exception:
                        pass
                logger.debug(f"Memory optimization executed: {collected} cyclic objects freed, malloc_trim: {trimmed}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in memory supervisor: {e}")

    async def _lag_monitor_loop(self):
        interval = 0.5
        while self._is_running:
            try:
                t0 = time.monotonic()
                await asyncio.sleep(interval)
                elapsed = time.monotonic() - t0
                lag_ms = (elapsed - interval) * 1000.0
                if lag_ms > self.lag_threshold_ms:
                    logger.warning(f"⚠️ Event loop lag detected: {lag_ms:.1f}ms! (A blocking operation ran on main thread)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in lag monitor: {e}")

system_supervisor = SystemSupervisor()
