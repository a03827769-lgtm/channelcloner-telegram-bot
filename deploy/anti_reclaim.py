#!/usr/bin/env python3
"""
==============================================================================
Oracle Cloud Always Free anti-reclamation helper (OPTIONAL)
==============================================================================
Oracle may reclaim an Always Free instance that stayed idle for 7 days: 95th-percentile CPU below 20%,
network below 20% and — on Ampere A1 shapes — memory below 20%. This helper keeps the VM above them:

* Memory: reserves ANTI_RECLAIM_RAM_PERCENT (default 24%) of the RAM and keeps the pages resident
  (on hosts with less than 2 GB only a small, safe amount).
* CPU: one worker PROCESS per core, each busy for ANTI_RECLAIM_CPU_PERCENT (default 23%) of every second
  at nice 19 — threads could not do this, the GIL serialised them onto a single core (~23% of ONE core).
  The bot always wins the CPU because the workers run at the lowest priority.
* Network: a light HTTPS heartbeat every minute.

Status for the admin bot is written atomically to <tempdir>/oracle_anti_reclaim_status.json.

It burns CPU and RAM on purpose: install it only on Oracle Always Free instances
(`sudo bash deploy/oracle_master_setup.sh --with-anti-reclaim`). ANTI_RECLAIM_CPU_PERCENT=0 disables
the CPU workers, ANTI_RECLAIM_RAM_PERCENT=0 the memory reservation.
==============================================================================
"""

import json
import logging
import multiprocessing
import os
import signal
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger("AntiReclaim")

STATUS_FILE = os.path.join(tempfile.gettempdir(), "oracle_anti_reclaim_status.json")
UZB_TZ = timezone(timedelta(hours=5))
HEARTBEAT_INTERVAL_SECONDS = 60
PAGE_SIZE = 4096


def _env_fraction(name: str, default: float) -> float:
    """Reads a percentage (0-90) from the environment and returns it as a fraction."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw) / 100.0
    except ValueError:
        logger.warning(f"Invalid {name}={raw!r}; using {default * 100:.0f}%")
        return default
    return min(max(value, 0.0), 0.9)


def cpu_burn_worker(duty_cycle: float, stop_event) -> None:
    """Runs in a separate process: busy for `duty_cycle` of every second, asleep for the rest."""
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    period = 1.0
    busy = period * duty_cycle
    counter = 0
    while not stop_event.is_set():
        start = time.monotonic()
        while time.monotonic() - start < busy:
            counter = (counter * 1103515245 + 12345) % 2147483648
        stop_event.wait(max(0.01, period - (time.monotonic() - start)))


def write_status_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".anti_reclaim_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


class OracleAntiReclaimDaemon:
    def __init__(self, target_ram_pct: Optional[float] = None, target_cpu_pct: Optional[float] = None):
        self.target_ram_pct = _env_fraction("ANTI_RECLAIM_RAM_PERCENT", 0.24) if target_ram_pct is None else target_ram_pct
        self.target_cpu_pct = _env_fraction("ANTI_RECLAIM_CPU_PERCENT", 0.23) if target_cpu_pct is None else target_cpu_pct
        self.health_url = os.getenv("ANTI_RECLAIM_HEALTH_URL", "http://127.0.0.1:8080/health")
        self.is_running = True
        self.total_ram_mb = self._get_total_memory_mb()
        if self.total_ram_mb < 2048:
            # Small VMs (1 GB AMD micro): never starve the bot
            self.allocated_ram_mb = min(120, int(self.total_ram_mb * 0.12))
        else:
            self.allocated_ram_mb = int(self.total_ram_mb * self.target_ram_pct)
        if self.target_ram_pct <= 0:
            self.allocated_ram_mb = 0
        self.allocated_buffer: Optional[bytearray] = None
        self.heartbeat_count = 0
        self.cpu_cores = os.cpu_count() or 1
        self._stop_event = None
        self._workers: List[multiprocessing.Process] = []

    @staticmethod
    def _get_total_memory_mb() -> int:
        """Total physical memory from /proc/meminfo, psutil as a fallback."""
        if os.path.exists("/proc/meminfo"):
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            return int(line.split()[1]) // 1024
            except (OSError, ValueError):
                pass
        try:
            import psutil
            return psutil.virtual_memory().total // (1024 * 1024)
        except Exception:
            return 2048

    def start_memory_keeper(self) -> None:
        """Allocates the resident block (always leaving 150 MB of the available RAM for the bot)."""
        target_mb = self.allocated_ram_mb
        try:
            import psutil
            available_mb = psutil.virtual_memory().available // (1024 * 1024)
            target_mb = min(target_mb, max(0, available_mb - 150))
        except Exception:
            pass
        if target_mb <= 0:
            logger.info("Memory keeper: nothing reserved (disabled or low available RAM)")
            self.allocated_ram_mb = 0
            return
        self.allocated_buffer = bytearray(target_mb * 1024 * 1024)
        self.allocated_ram_mb = target_mb
        self.touch_memory()
        logger.info(f"Memory keeper: {target_mb} MB reserved ({target_mb / max(1, self.total_ram_mb) * 100:.1f}% of {self.total_ram_mb} MB)")

    def touch_memory(self) -> None:
        """Writes one byte per page so the kernel keeps the whole block resident."""
        buf = self.allocated_buffer
        if buf:
            pages = len(range(0, len(buf), PAGE_SIZE))
            buf[::PAGE_SIZE] = b"\x01" * pages

    def start_cpu_workers(self) -> None:
        if self.target_cpu_pct <= 0:
            logger.info("CPU workers disabled (ANTI_RECLAIM_CPU_PERCENT=0)")
            return
        self._stop_event = multiprocessing.Event()
        for index in range(self.cpu_cores):
            worker = multiprocessing.Process(
                target=cpu_burn_worker,
                args=(self.target_cpu_pct, self._stop_event),
                name=f"anti-reclaim-cpu-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        logger.info(f"CPU workers: {len(self._workers)} processes at {self.target_cpu_pct * 100:.0f}% duty cycle (nice 19)")

    def stop(self) -> None:
        self.is_running = False
        if self._stop_event is not None:
            self._stop_event.set()
        for worker in self._workers:
            worker.join(timeout=3)
            if worker.is_alive():
                worker.terminate()
        self._workers = []
        try:
            os.remove(STATUS_FILE)
        except OSError:
            pass

    def status_payload(self) -> dict:
        return {
            "status": "active",
            "total_ram_mb": self.total_ram_mb,
            "allocated_ram_mb": self.allocated_ram_mb,
            "ram_percent": round((self.allocated_ram_mb / max(1, self.total_ram_mb)) * 100, 1),
            "cpu_cores": self.cpu_cores,
            "cpu_workers": len(self._workers),
            "target_cpu_percent": round(self.target_cpu_pct * 100, 1),
            "network_heartbeats": self.heartbeat_count,
            "last_heartbeat": datetime.now(UZB_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def heartbeat_once(self) -> None:
        self.heartbeat_count += 1
        for name, url in (("Bot health", self.health_url), ("Telegram API", "https://api.telegram.org"),
                          ("Cloudflare DNS", "https://1.1.1.1")):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Oracle-Anti-Reclaim/3.0"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    response.read(128)
            except Exception as e:
                logger.debug(f"Heartbeat {name} skipped: {e}")
        self.touch_memory()
        try:
            write_status_atomic(STATUS_FILE, self.status_payload())
        except OSError as e:
            logger.warning(f"Status write failed: {e}")

    def run(self) -> None:
        try:
            os.nice(19)
        except (AttributeError, OSError):
            pass
        logger.info(f"Starting anti-reclaim helper (cores: {self.cpu_cores}, RAM: {self.total_ram_mb} MB)")
        self.start_memory_keeper()
        self.start_cpu_workers()

        stop_requested = threading.Event()

        def _on_signal(signum, frame):
            logger.info(f"Signal {signum} received; stopping")
            stop_requested.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        try:
            while not stop_requested.is_set():
                self.heartbeat_once()
                stop_requested.wait(HEARTBEAT_INTERVAL_SECONDS)
        finally:
            self.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    OracleAntiReclaimDaemon().run()
    sys.exit(0)
