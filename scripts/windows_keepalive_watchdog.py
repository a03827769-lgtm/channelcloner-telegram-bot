"""
Telegram Channel Cloner - 24/7 Windows Keep-Awake & Dual-Mode Auto-Recovery Watchdog
Maintains Win32 Away Mode (ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED | ES_CONTINUOUS)
so that when the laptop is locked (Win+L), screen turns off, or lid is closed,
Windows kernel, CPU, WSL2, and network interfaces remain at 100% capacity.
Supervises Docker container or native Python process with automatic self-healing.
Includes Single-Instance Mutex, Healthcheck ping, Network sanity keepalive, and CLI controls.
"""

import sys
import os
import time
import signal
import ctypes
import logging
import subprocess
import urllib.request
import json
import socket
import argparse
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

# Win32 Power Management Constants
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040
ES_CONTINUOUS = 0x80000000

# Mutex Configuration
MUTEX_NAME = "Local\\ChannelCloner_24_7_Watchdog_Mutex"
_mutex_handle = None

# Directory setup
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")
PID_FILE = os.path.join(LOG_DIR, "watchdog.pid")
STATUS_FILE = os.path.join(LOG_DIR, "watchdog_status.json")

# Setup Rotating Logger
logger = logging.getLogger("WindowsKeepAwakeWatchdog")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

CONTAINER_NAME = "telegram_channel_cloner"
CHECK_INTERVAL_SECONDS = 15
MAX_CONSECUTIVE_FAILURES = 2

_is_running = True


def acquire_single_instance_mutex() -> bool:
    """Acquires a Win32 Named Mutex to prevent duplicate watchdog instances."""
    global _mutex_handle
    try:
        ERROR_ALREADY_EXISTS = 183
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
        last_error = ctypes.windll.kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception as e:
        logger.warning(f"Could not create Win32 Mutex: {e}")
        return True


def release_single_instance_mutex():
    """Releases and closes the Win32 Named Mutex."""
    global _mutex_handle
    if _mutex_handle:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        _mutex_handle = None


def write_pid():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def remove_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def get_running_watchdog_pid() -> Optional[int]:
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            import psutil
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    cmd_list = proc.cmdline() or []
                    cmd = " ".join(cmd_list)
                    if "windows_keepalive_watchdog.py" in cmd and "--status" not in cmd and "--stop" not in cmd:
                        return pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if p.pid == os.getpid():
                continue
            name = (p.info["name"] or "").lower()
            if not name.startswith("python"):
                continue
            try:
                cmd_list = p.info["cmdline"] or []
                cmd = " ".join(cmd_list)
                if "windows_keepalive_watchdog.py" in cmd and "--status" not in cmd and "--stop" not in cmd:
                    return p.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    return None


def stop_watchdog():
    pid = get_running_watchdog_pid()
    if not pid:
        print("ℹ️ No active Watchdog process found.")
        return
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        p.wait(timeout=5)
        print(f"✅ Watchdog process (PID {pid}) stopped successfully.")
    except Exception as e:
        print(f"⚠️ Failed to stop Watchdog (PID {pid}): {e}")
    remove_pid()


def enable_keep_awake():
    """Sets Win32 thread execution state to Away Mode so CPU and network remain active while locked."""
    try:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if result == 0:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            logger.debug("Keep-Awake active (Standard System Required).")
        else:
            logger.debug("Keep-Awake active (System Required + Away Mode).")
    except Exception as e:
        logger.warning(f"Could not set Win32 execution state: {e}")


def disable_keep_awake():
    """Resets Win32 thread execution state to normal default."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logger.info("Keep-Awake deactivated. Default Windows power state restored.")
    except Exception as e:
        logger.warning(f"Could not reset Win32 execution state: {e}")


def keep_network_alive():
    """Performs lightweight DNS lookup to prevent Windows network adapter power-saving sleep."""
    try:
        socket.getaddrinfo("api.telegram.org", 443, socket.AF_INET, socket.SOCK_STREAM)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def signal_handler(signum, frame):
    global _is_running
    logger.info(f"Received signal {signum}. Shutting down Watchdog...")
    _is_running = False


def get_health_port() -> int:
    port = 8080
    port_file = os.path.join(LOG_DIR, "health_port.txt")
    if os.path.exists(port_file):
        try:
            with open(port_file, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val.isdigit():
                    port = int(val)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    elif os.getenv("PORT"):
        try:
            port = int(os.getenv("PORT"))
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    return port


def get_health_url() -> str:
    """Returns the canonical loopback healthcheck URL based on resolved port."""
    return f"http://127.0.0.1:{get_health_port()}/health"


def check_http_health_detailed() -> Dict[str, Any]:
    port = get_health_port()
    # Try both 127.0.0.1 and localhost (handles Windows IPv4/IPv6 dual-stack Docker bindings)
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{port}/health"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Watchdog/2.0"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    return data
        except Exception:
            continue
    return {}


def check_http_health() -> bool:
    data = check_http_health_detailed()
    return data.get("status") == "ok"


def check_docker_daemon() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=8)
        return res.returncode == 0
    except Exception:
        return False


def is_docker_process_active() -> bool:
    try:
        res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Docker Desktop.exe"], capture_output=True, text=True, timeout=5)
        return "Docker Desktop.exe" in res.stdout
    except Exception:
        return False


def ensure_docker_daemon() -> bool:
    if check_docker_daemon():
        return True

    logger.warning("Docker daemon is not responding. Checking Docker Desktop process...")
    if is_docker_process_active():
        logger.info("Docker Desktop process is already running. Waiting for daemon to initialize...")
        for _ in range(30):
            time.sleep(2)
            if check_docker_daemon():
                logger.info("✅ Docker daemon is now online and healthy.")
                return True
        logger.warning("Docker Desktop process exists but daemon response timed out.")
        return False

    docker_desktop_path = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if os.path.exists(docker_desktop_path):
        try:
            CREATE_NO_WINDOW = 0x08000000
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                [docker_desktop_path],
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                close_fds=True
            )
            logger.info(f"Launched {docker_desktop_path}. Waiting for Docker daemon to become responsive...")
            for _ in range(40):
                time.sleep(2)
                if check_docker_daemon():
                    logger.info("✅ Docker daemon is now online and healthy.")
                    return True
        except Exception as e:
            logger.error(f"Failed to launch Docker backend: {e}")

    return check_docker_daemon()


def check_container_running() -> bool:
    try:
        res = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={CONTAINER_NAME}"],
            capture_output=True,
            text=True,
            timeout=8
        )
        return bool(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Docker CLI check error: {e}")
        return False


_recovery_attempts = 0
_last_recovery_time = 0.0


def recover_container():
    """Restarts or boots up the Docker container with exponential backoff and compose fallback."""
    global _recovery_attempts, _last_recovery_time
    now = time.time()
    # Exponential backoff: 15s, 30s, 60s, 120s, up to 300s
    backoff_delay = min(300, 15 * (2 ** min(_recovery_attempts, 5)))
    if _last_recovery_time and (now - _last_recovery_time < backoff_delay):
        rem = int(backoff_delay - (now - _last_recovery_time))
        logger.warning(f"⏳ Recovery backoff active: waiting {rem}s before next restart attempt (attempt #{_recovery_attempts})...")
        return

    _recovery_attempts += 1
    _last_recovery_time = now
    logger.warning(f"Attempting automatic container recovery (attempt #{_recovery_attempts}, backoff={backoff_delay}s)...")
    if not check_docker_daemon():
        if not ensure_docker_daemon():
            logger.error("Cannot recover container: Docker daemon failed to start.")
            return

    # Check if container is already in a Docker-managed restart loop
    try:
        inspect_res = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Restarting}} {{.State.ExitCode}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=5
        )
        if inspect_res.returncode == 0:
            parts = inspect_res.stdout.strip().split()
            is_restarting = (parts[0].lower() == "true") if len(parts) > 0 else False
            if is_restarting:
                logger.warning(f"⚠️ Container {CONTAINER_NAME} is already restarting internally. Backing off to prevent WSL2 thrashing...")
                return
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    is_running = check_container_running()
    try:
        if is_running:
            logger.info(f"Restarting existing container {CONTAINER_NAME}...")
            subprocess.run(["docker", "restart", CONTAINER_NAME], timeout=30, check=True)
        else:
            logger.info(f"Starting stopped container {CONTAINER_NAME}...")
            res = subprocess.run(
                ["docker", "compose", "up", "-d"],
                cwd=BASE_DIR,
                timeout=60,
                capture_output=True
            )
            if res.returncode != 0:
                subprocess.run(["docker", "start", CONTAINER_NAME], timeout=30, check=True)
        logger.info("Docker recovery command dispatched successfully.")
    except Exception as e:
        logger.error(f"Container recovery failed: {e}. Falling back to docker compose up -d...")
        try:
            subprocess.run(["docker", "compose", "up", "-d"], cwd=BASE_DIR, timeout=60, check=True)
            logger.info("docker compose up -d succeeded.")
        except Exception as e2:
            logger.critical(f"Critical error bringing up container: {e2}")


class NativeProcessSupervisor:
    """Supervises python run.py as a persistent native Windows background process."""
    def __init__(self):
        self.process = None

    def is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def start(self):
        if self.is_running():
            return
        run_script = os.path.join(BASE_DIR, "run.py")
        logger.info(f"Spawning native bot supervisor: {sys.executable} {run_script}")
        try:
            CREATE_NO_WINDOW = 0x08000000
            self.process = subprocess.Popen(
                [sys.executable, run_script],
                cwd=BASE_DIR,
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info(f"Native bot process spawned with PID {self.process.pid}")
        except Exception as e:
            logger.error(f"Failed to spawn native bot process: {e}")

    def restart(self):
        logger.warning("Restarting native bot process...")
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        self.process = None
        time.sleep(2)
        self.start()

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            self.process = None


class CodeChangeWatcher:
    """
    High-performance, background code change watcher.
    Continuously monitors bot/, services/, admin_bot/, config/, database/, run.py, .env.
    Applies a 1.5s debounce to batch file modifications, and executes on_change_callback.
    """
    def __init__(self, base_dir: str, on_change_callback, debounce_seconds: float = 1.5, poll_interval: float = 0.5):
        self.base_dir = os.path.abspath(base_dir)
        self.on_change_callback = on_change_callback
        self.debounce_seconds = debounce_seconds
        self.poll_interval = poll_interval
        self.watched_dirs = ["bot", "services", "admin_bot", "config", "database"]
        self.watched_files = ["run.py", ".env"]
        self.watched_extensions = {".py", ".env", ".json", ".yml", ".yaml"}
        self.ignored_dirs = {
            "__pycache__", ".git", ".pytest_cache", "logs", "data", "temp_media",
            "tests", ".specify", "venv", ".venv", "node_modules", ".gemini", "brain"
        }
        self._snapshots: Dict[str, float] = {}
        self._pending_change_time: Optional[float] = None
        self._changed_files = set()
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

    def _scan_files(self) -> Dict[str, float]:
        current = {}
        for fname in self.watched_files:
            fpath = os.path.join(self.base_dir, fname)
            if os.path.isfile(fpath):
                try:
                    current[fpath] = os.path.getmtime(fpath)
                except OSError:
                    pass

        for dname in self.watched_dirs:
            dir_path = os.path.join(self.base_dir, dname)
            if not os.path.isdir(dir_path):
                continue
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = [d for d in dirs if d not in self.ignored_dirs and not d.startswith(".")]
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in self.watched_extensions:
                        full_p = os.path.join(root, file)
                        try:
                            current[full_p] = os.path.getmtime(full_p)
                        except OSError:
                            pass
        return current

    def start(self):
        self._snapshots = self._scan_files()
        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, name="CodeChangeWatcherThread", daemon=True)
        self._thread.start()
        logger.info(f"👀 Live Code Auto-Reloader active. Tracking {len(self._snapshots)} source files across {self.watched_dirs}...")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _watch_loop(self):
        while self._running:
            try:
                current_files = self._scan_files()
                changes = []

                for fpath, mtime in current_files.items():
                    old_mtime = self._snapshots.get(fpath)
                    if old_mtime is None:
                        rel_path = os.path.relpath(fpath, self.base_dir)
                        changes.append((rel_path, "created"))
                    elif mtime > old_mtime + 0.001:
                        rel_path = os.path.relpath(fpath, self.base_dir)
                        changes.append((rel_path, "modified"))

                for fpath in list(self._snapshots.keys()):
                    if fpath not in current_files:
                        rel_path = os.path.relpath(fpath, self.base_dir)
                        changes.append((rel_path, "deleted"))

                now = time.time()
                with self._lock:
                    if changes:
                        for rel_p, action in changes:
                            self._changed_files.add(f"{rel_p} ({action})")
                        self._pending_change_time = now
                        self._snapshots = current_files

                    if self._pending_change_time and (now - self._pending_change_time >= self.debounce_seconds):
                        changed_list = sorted(list(self._changed_files))
                        self._changed_files.clear()
                        self._pending_change_time = None

                        logger.info(f"🔄 [AUTO-RELOAD] Detected code change in: {', '.join(changed_list[:4])}" +
                                    (f" and {len(changed_list) - 4} more" if len(changed_list) > 4 else ""))
                        try:
                            self.on_change_callback(changed_list)
                        except Exception as cb_err:
                            logger.error(f"Error executing auto-reload callback: {cb_err}")

            except Exception as e:
                logger.debug(f"Error in CodeChangeWatcher scan: {e}")

            for _ in range(int(max(1, self.poll_interval / 0.1))):
                if not self._running:
                    break
                time.sleep(0.1)


def trigger_cli_reload():
    """Manual reload triggered from CLI command."""
    print("🔄 Dispatching manual reload to bot...")
    is_docker = check_container_running()
    if is_docker:
        start_t = time.time()
        res = subprocess.run(["docker", "restart", CONTAINER_NAME], timeout=35, capture_output=True, text=True)
        elapsed = time.time() - start_t
        if res.returncode == 0:
            print(f"✅ Container {CONTAINER_NAME} reloaded successfully in {elapsed:.1f}s.")
            time.sleep(2)
            health = check_http_health_detailed()
            if health.get("status") == "ok":
                print("🟢 Healthcheck 200 OK — Bot is fully responsive.")
            else:
                print("⏳ Container restarting, healthcheck will settle shortly.")
        else:
            print(f"❌ Failed to restart container: {res.stderr}")
    else:
        pid = get_running_watchdog_pid()
        if pid:
            print(f"ℹ️ Watchdog running under PID {pid}. Restarting native bot supervisor...")
            stop_watchdog()
            time.sleep(1)
            subprocess.Popen([sys.executable, __file__], creationflags=0x08000000 | 0x00000008, close_fds=True)
            print("✅ Watchdog & Bot restarted.")
        else:
            print("ℹ️ Neither Docker container nor Watchdog supervisor is active.")


def save_status(mode: str, healthy: bool, consecutive_failures: int):
    status_payload = {
        "pid": os.getpid(),
        "mode": mode,
        "last_check_utc": datetime.now(timezone.utc).isoformat(),
        "healthy": healthy,
        "consecutive_failures": consecutive_failures,
        "win32_away_mode": True
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_payload, f, indent=2)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def show_status():
    pid = get_running_watchdog_pid()
    is_alive = pid is not None
    health_data = check_http_health_detailed()

    print("=" * 65)
    print("   TELEGRAM CHANNEL CLONER — 24/7 WATCHDOG TELEMETRY")
    print("=" * 65)
    print(f"  Watchdog Supervisor : {'🟢 RUNNING (PID ' + str(pid) + ')' if is_alive else '🔴 STOPPED'}")
    print(f"  Win32 Away Mode     : {'🟢 Active (Lock & Lid-Close Protected)' if is_alive else '🔴 Inactive'}")
    print(f"  Live Auto-Reload    : 🟢 Active (Hot-reloads on file edit/save)")
    print(f"  HTTP Healthcheck    : {'🟢 200 OK' if health_data.get('status') == 'ok' else '🔴 UNHEALTHY'}")
    if health_data:
        print(f"  Service Status      : {health_data.get('service', 'N/A')} ({health_data.get('bot', 'N/A')})")
        print(f"  Telethon Account    : {'🟢 Connected' if health_data.get('telethon_connected') else '🔴 Disconnected'}")
        if "uptime" in health_data:
            print(f"  System Uptime       : {health_data.get('uptime')}")
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
                print(f"  Execution Engine    : {st.get('mode', 'N/A')}")
                print(f"  Last Health Check   : {st.get('last_check_utc', 'N/A')}")
                print(f"  Consecutive Fails   : {st.get('consecutive_failures', 0)}")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Telegram Channel Cloner 24/7 Watchdog")
    parser.add_argument("--status", action="store_true", help="Display current Watchdog and Bot status")
    parser.add_argument("--stop", action="store_true", help="Stop running Watchdog process")
    parser.add_argument("--restart", action="store_true", help="Restart running Watchdog process")
    parser.add_argument("--reload", action="store_true", help="Trigger an immediate manual reload of the bot")
    parser.add_argument("--no-reload", action="store_true", help="Disable automatic code-change hot-reloading")
    parser.add_argument("--mode", choices=["auto", "docker", "native"], default="auto", help="Supervision mode")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_SECONDS, help="Check interval in seconds")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.reload:
        trigger_cli_reload()
        return

    if args.stop:
        stop_watchdog()
        return

    if args.restart:
        stop_watchdog()
        time.sleep(1)

    # Enforce Single-Instance Mutex
    if not acquire_single_instance_mutex():
        print("⚠️ Another instance of 24/7 Watchdog is already running (Mutex held). Exiting cleanly.")
        logger.info("Watchdog invoked but an instance is already active. Exiting.")
        return

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    global _recovery_attempts

    write_pid()

    # Determine execution mode
    resolved_mode = args.mode
    if resolved_mode == "auto":
        resolved_mode = "docker" if check_docker_daemon() else "native"

    logger.info("=" * 70)
    logger.info("🚀 24/7 Windows Keep-Awake & Auto-Recovery Watchdog started.")
    logger.info("Win32 Away Mode engaged: System will NOT sleep when laptop is locked (Win+L).")
    logger.info(f"Execution engine mode: {resolved_mode.upper()} | Check interval: {args.interval}s")
    logger.info("=" * 70)

    native_supervisor = NativeProcessSupervisor()
    if resolved_mode == "native":
        native_supervisor.start()
    else:
        ensure_docker_daemon()
        if not check_container_running() or not check_http_health():
            logger.info("⚡ Startup check: Container is not running or not healthy. Triggering immediate startup...")
            recover_container()

    consecutive_failures = 0
    cycle_counter = 0

    # Auto-Reload Code Watcher
    auto_reload_enabled = not args.no_reload
    code_watcher = None

    def on_code_change(changed_files):
        nonlocal consecutive_failures
        consecutive_failures = 0
        logger.info("🔄 [AUTO-RELOAD] Reloading Channel Cloner with updated source code...")
        if resolved_mode == "docker":
            try:
                t0 = time.time()
                res = subprocess.run(["docker", "restart", CONTAINER_NAME], timeout=35, capture_output=True, text=True)
                elapsed = time.time() - t0
                if res.returncode == 0:
                    logger.info(f"✅ [AUTO-RELOAD] Docker container reloaded successfully in {elapsed:.1f}s.")
                else:
                    logger.error(f"❌ [AUTO-RELOAD] Docker restart failed: {res.stderr}")
            except Exception as e:
                logger.error(f"❌ [AUTO-RELOAD] Failed to restart container: {e}")
        else:
            logger.info("🔄 [AUTO-RELOAD] Restarting native bot supervisor...")
            native_supervisor.restart()
            logger.info("✅ [AUTO-RELOAD] Native bot process restarted.")

    if auto_reload_enabled:
        code_watcher = CodeChangeWatcher(base_dir=BASE_DIR, on_change_callback=on_code_change, debounce_seconds=1.5)
        code_watcher.start()

    try:
        while _is_running:
            try:
                cycle_counter += 1

                # 1. Renew Win32 Away Mode execution state every cycle
                enable_keep_awake()

                # 2. Prevent network adapter idle sleep every 4 cycles (~60s)
                if cycle_counter % 4 == 0:
                    keep_network_alive()

                # 3. Supervise according to mode
                if resolved_mode == "docker":
                    if not check_container_running():
                        logger.warning(f"⚠️ Container {CONTAINER_NAME} is stopped! Triggering immediate recovery...")
                        recover_container()
                        consecutive_failures = 0
                        time.sleep(10)
                        continue
                elif resolved_mode == "native":
                    if not native_supervisor.is_running():
                        logger.warning("⚠️ Native bot process exited unexpectedly! Restarting...")
                        native_supervisor.restart()
                        consecutive_failures = 0
                        time.sleep(5)
                        continue

                # 4. Check HTTP Health
                healthy = check_http_health()
                save_status(resolved_mode, healthy, consecutive_failures)

                if healthy:
                    if consecutive_failures > 0 or _recovery_attempts > 0:
                        logger.info("✅ Health restored: Bot is running and responsive.")
                    consecutive_failures = 0
                    _recovery_attempts = 0
                else:
                    consecutive_failures += 1
                    logger.warning(f"⚠️ Healthcheck failed (Fail #{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error(f"Threshold exceeded ({MAX_CONSECUTIVE_FAILURES} consecutive failures). Triggering auto-recovery!")
                        if resolved_mode == "docker":
                            recover_container()
                        else:
                            native_supervisor.restart()
                        consecutive_failures = 0
                        time.sleep(10)

                # Periodic heartbeat log every 20 cycles (~5 minutes)
                if cycle_counter % 20 == 0:
                    health_info = check_http_health_detailed()
                    telethon_st = "Connected" if health_info.get("telethon_connected") else "Pending"
                    logger.info(f"🛡️ Watchdog Heartbeat: Mode={resolved_mode.upper()} | Health=OK | Telethon={telethon_st} | Win32 Away Mode=Active")

            except Exception as loop_err:
                logger.error(f"Unexpected error in Watchdog loop: {loop_err}")

            # Sleep interval in small chunks for responsive termination
            for _ in range(args.interval):
                if not _is_running:
                    break
                time.sleep(1)

    finally:
        if code_watcher:
            code_watcher.stop()
        if resolved_mode == "native":
            native_supervisor.stop()
        disable_keep_awake()
        release_single_instance_mutex()
        remove_pid()
        logger.info("Watchdog terminated cleanly.")


if __name__ == "__main__":
    main()
