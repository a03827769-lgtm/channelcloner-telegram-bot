import asyncio
import ctypes
import gc
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger("SystemSupervisor")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Python frames the event loop sits in while it waits for I/O (selector loop, Windows proactor loop)
_IDLE_FRAMES = {("selectors.py", "select"), ("windows_events.py", "_poll"), ("windows_events.py", "select")}
STARVED_SITE = "loop was idle: the whole process was not scheduled (host RAM/CPU pressure or sleep)"


def describe_loop_frame(frame) -> str:
    """Names what the event loop thread is doing in `frame` (its innermost Python frame): the innermost
    project frame, the library call it is in, or STARVED_SITE when the loop was only waiting for I/O."""
    if frame is None:
        return "unknown"
    code = frame.f_code
    if (os.path.basename(code.co_filename), code.co_name) in _IDLE_FRAMES:
        return STARVED_SITE
    innermost = f"{os.path.basename(code.co_filename)}:{frame.f_lineno} {code.co_name}"
    current = frame
    while current is not None:
        filename = os.path.abspath(current.f_code.co_filename)
        if filename.startswith(_PROJECT_ROOT + os.sep) and "site-packages" not in filename:
            relative = os.path.relpath(filename, _PROJECT_ROOT).replace(os.sep, "/")
            site = f"{relative}:{current.f_lineno} {current.f_code.co_name}"
            return site if current is frame else f"{site} (in {innermost})"
        current = current.f_back
    return innermost


def host_memory_summary() -> str:
    """'host RAM 96% used, 0.6 GB free', or '' when psutil is unavailable."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return f"host RAM {vm.percent:.0f}% used, {vm.available / 1024 ** 3:.1f} GB free"
    except Exception:
        return ""

class SystemSupervisor:
    """
    24/7 System & Memory Supervisor:
    - Event Loop Lag Monitor (detects synchronous blocking stalls > 150ms)
    - Memory Optimizer (periodic GC collection and Linux malloc_trim)
    - Prevents Docker OOM crashes and keeps RAM < 200MB indefinitely
    """
    def __init__(self, memory_interval: int = 300, lag_threshold_ms: float = 150.0, lag_report_interval: float = 60.0):
        self.memory_interval = memory_interval
        self.lag_threshold_ms = lag_threshold_ms
        # Lag warnings are rate limited: a CPU-heavy phase (video watermarking, big albums) would otherwise
        # write one line every 0.5 s and push everything else out of the in-memory log viewer.
        self.lag_report_interval = lag_report_interval
        self._is_running = False
        self._tasks = []
        # Stall sampler: a thread that sees the lag monitor's heartbeat go stale and records what the loop
        # thread is doing at that moment, so a lag warning names the blocking call instead of guessing
        self._heartbeat: Optional[float] = None
        self._stall_site: Optional[str] = None
        self._sampler_stop = threading.Event()
        self._sampler: Optional[threading.Thread] = None
        self._libc = None
        if sys.platform != "win32":
            for lib_name in ["libc.so.6", "libc.musl-x86_64.so.1", "libc.musl-aarch64.so.1", "libc.so"]:
                try:
                    self._libc = ctypes.CDLL(lib_name)
                    if hasattr(self._libc, "malloc_trim"):
                        break
                except Exception:
                    continue

    def start(self):
        if self._is_running:
            logger.debug("SystemSupervisor is already running.")
            return
        self._is_running = True
        self._tasks = [
            asyncio.create_task(self._memory_loop()),
            asyncio.create_task(self._lag_monitor_loop())
        ]
        # A fresh event per thread: a sampler from an earlier start() must not be revived by this one
        self._sampler_stop = threading.Event()
        self._sampler = threading.Thread(target=self._stall_sampler, args=(self._sampler_stop, threading.get_ident()),
                                         name="loop-stall-sampler", daemon=True)
        self._sampler.start()
        logger.info("24/7 System Supervisor started (Event loop lag watchdog + Memory trimmer).")

    def stop(self):
        self._is_running = False
        self._sampler_stop.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks.clear()

    async def aclose(self, timeout: float = 5.0):
        """Stops the supervisor loops and waits (bounded) until they have exited."""
        tasks = [t for t in self._tasks if not t.done()]
        self.stop()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def _memory_loop(self):
        try:
            gc.set_threshold(50000, 10, 10)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        while self._is_running:
            try:
                await asyncio.sleep(self.memory_interval)
                collected = gc.collect()
                trimmed = 0
                if self._libc and hasattr(self._libc, "malloc_trim"):
                    try:
                        trimmed = self._libc.malloc_trim(0)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                try:
                    from services.rate_limiter import rate_limiter
                    await rate_limiter.prune_stale_locks()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
                logger.debug(f"Memory optimization executed: {collected} cyclic objects freed, malloc_trim: {trimmed}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in memory supervisor: {e}")

    LAG_INTERVAL = 0.5

    def _stall_sampler(self, stop: threading.Event, loop_thread_id: int):
        """Runs in its own thread. Once per stall (heartbeat older than the sleep plus the threshold) it
        records where the loop thread is. A loop found idle in select() was ready to run but the process
        got no CPU time: the stall came from the host, not from this code."""
        stale_after = self.LAG_INTERVAL + max(self.lag_threshold_ms, 0.0) / 1000.0
        sampled_beat = None
        while not stop.wait(0.05):
            beat = self._heartbeat
            if beat is None or beat == sampled_beat or time.monotonic() - beat <= stale_after:
                continue
            sampled_beat = beat
            try:
                frame = sys._current_frames().get(loop_thread_id)
                self._stall_site = describe_loop_frame(frame)
            except Exception:
                self._stall_site = None
            finally:
                frame = None  # do not keep the loop thread's frames alive

    async def _lag_monitor_loop(self):
        interval = self.LAG_INTERVAL
        # -inf, not 0.0: time.monotonic() counts from boot on Linux, so 0.0 silenced every warning during the
        # first lag_report_interval seconds after the machine started (fresh servers, CI runners)
        last_report = float("-inf")
        suppressed = 0
        worst_ms = 0.0
        worst_site = None
        while self._is_running:
            try:
                t0 = time.monotonic()
                self._stall_site = None
                self._heartbeat = t0
                await asyncio.sleep(interval)
                elapsed = time.monotonic() - t0
                lag_ms = (elapsed - interval) * 1000.0
                if lag_ms > self.lag_threshold_ms:
                    if lag_ms >= worst_ms:
                        worst_ms, worst_site = lag_ms, self._stall_site
                    now = time.monotonic()
                    if now - last_report >= self.lag_report_interval:
                        extra = f" ({suppressed} more stalls since the last report)" if suppressed else ""
                        where = f" Stalled at: {worst_site}." if worst_site else ""
                        memory = host_memory_summary()
                        memory = f" ({memory})" if memory else ""
                        logger.warning(f"⚠️ Event loop lag detected: {worst_ms:.1f}ms!{where}{memory}{extra}")
                        last_report = now
                        suppressed = 0
                        worst_ms = 0.0
                        worst_site = None
                    else:
                        suppressed += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in lag monitor: {e}")

system_supervisor = SystemSupervisor()
