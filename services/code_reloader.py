"""
Code Reloader Service for Telegram Channel Cloner
Monitors Python source files and application configuration for changes.
Supports in-process zero-downtime hot-reloading and external notification hooks.
"""
import os
import sys
import time
import asyncio
import logging
from typing import Set, Dict, Callable, Optional, List

logger = logging.getLogger("CodeReloader")


class SourceCodeWatcher:
    """
    Asynchronous file watcher that checks modification timestamps of project source files.
    Designed for development & live hot-reloading without requiring native C-inotify wrappers.
    """
    def __init__(
        self,
        base_dir: str,
        watched_dirs: Optional[List[str]] = None,
        watched_files: Optional[List[str]] = None,
        watched_extensions: Optional[Set[str]] = None,
        ignored_dirs: Optional[Set[str]] = None,
        poll_interval: float = 1.2,
        debounce_seconds: float = 1.5,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.watched_dirs = watched_dirs or ["bot", "services", "admin_bot", "config", "database"]
        self.watched_files = watched_files or ["run.py", ".env"]
        self.watched_extensions = watched_extensions or {".py", ".env", ".json", ".yml", ".yaml"}
        self.ignored_dirs = ignored_dirs or {
            "__pycache__", ".git", ".pytest_cache", "logs", "data", "temp_media",
            "tests", ".specify", "venv", ".venv", "node_modules", ".gemini", "brain",
            ".idea", ".vscode"
        }
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds

        self._snapshots: Dict[str, float] = {}
        self._changed_files: Set[str] = set()
        self._pending_change_time: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._is_running = False
        self._callbacks: List[Callable[[List[str]], None]] = []

    def add_reload_callback(self, callback: Callable[[List[str]], None]):
        self._callbacks.append(callback)

    def scan_mtimes(self) -> Dict[str, float]:
        """Collects {absolute_path: st_mtime} for all watched source files."""
        current: Dict[str, float] = {}
        
        # 1. Single root files
        for fname in self.watched_files:
            fpath = os.path.join(self.base_dir, fname)
            if os.path.isfile(fpath):
                try:
                    current[fpath] = os.path.getmtime(fpath)
                except OSError:
                    pass

        # 2. Subdirectories
        for dname in self.watched_dirs:
            dir_path = os.path.join(self.base_dir, dname)
            if not os.path.isdir(dir_path):
                continue
            for root, dirs, files in os.walk(dir_path):
                # Prune ignored dirs in place to avoid walking them
                dirs[:] = [d for d in dirs if d not in self.ignored_dirs and not d.startswith(".")]
                for file in files:
                    if file.startswith(".") or file.endswith(("-wal", "-shm", ".tmp", ".swp")):
                        continue
                    ext = os.path.splitext(file)[1].lower()
                    if ext in self.watched_extensions:
                        full_p = os.path.join(root, file)
                        try:
                            current[full_p] = os.path.getmtime(full_p)
                        except OSError:
                            pass
        return current

    def start(self):
        """Starts the asynchronous file watcher task."""
        if self._is_running:
            return
        self._snapshots = self.scan_mtimes()
        self._is_running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info(f"👀 Live Code Reloader active ({len(self._snapshots)} source files tracked in {self.watched_dirs})")

    def stop(self):
        """Stops the watcher task."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _watch_loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(self.poll_interval)
                # Run file system scan in thread to prevent event loop blocking
                current_files = await asyncio.to_thread(self.scan_mtimes)
                now = time.time()
                detected = []

                # Modifications and additions
                for fpath, mtime in current_files.items():
                    old_mtime = self._snapshots.get(fpath)
                    if old_mtime is None:
                        rel = os.path.relpath(fpath, self.base_dir)
                        detected.append(f"+ {rel}")
                    elif mtime > old_mtime + 0.001:
                        rel = os.path.relpath(fpath, self.base_dir)
                        detected.append(f"~ {rel}")

                # Deletions
                for fpath in list(self._snapshots.keys()):
                    if fpath not in current_files:
                        rel = os.path.relpath(fpath, self.base_dir)
                        detected.append(f"- {rel}")

                if detected:
                    self._changed_files.update(detected)
                    self._pending_change_time = now
                    self._snapshots = current_files

                # If changes are pending and debounce duration has settled
                if self._pending_change_time and (now - self._pending_change_time >= self.debounce_seconds):
                    changes_list = sorted(list(self._changed_files))
                    self._changed_files.clear()
                    self._pending_change_time = None

                    logger.info(f"🔄 [HOT-RELOAD] Source changes settled: {changes_list}")
                    for cb in self._callbacks:
                        try:
                            if asyncio.iscoroutinefunction(cb):
                                await cb(changes_list)
                            else:
                                cb(changes_list)
                        except Exception as cb_err:
                            logger.error(f"Error in reload callback: {cb_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"SourceCodeWatcher scan error: {e}")


code_reloader = SourceCodeWatcher(base_dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
