"""
Telegram Channel Cloner - 24/7 Windows keep-awake watchdog for the native host runtime.

* Keeps Windows awake (Away Mode: ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED | ES_CONTINUOUS) so the bot
  keeps running while the laptop is locked (Win+L) or the screen is off.
* Supervises `python run.py` - the ONE production bot instance on this host.
* Supervises the named Cloudflare tunnel of the Mini App (`cloudflared tunnel run <name>`).

Single-instance rule: a bot token and a Telethon session may be used by exactly one process at a time
(otherwise Telegram answers with TelegramConflictError / AUTH_KEY_DUPLICATED and posts are duplicated).
The watchdog therefore never starts run.py while the Docker container `telegram_channel_cloner`
(docker-compose profile "docker-bot") is running, and it stops that container when it shows up next to an
already running host bot (the first instance wins). Docker is an ALTERNATIVE runtime, never an addition.

Usage (from the project root):
  pythonw scripts\\windows_keepalive_watchdog.py           run the watchdog (this is the autostart entry)
  python scripts\\windows_keepalive_watchdog.py --status   show watchdog / bot / tunnel state
  python scripts\\windows_keepalive_watchdog.py --stop     graceful stop (bot and tunnel are stopped too)
  python scripts\\windows_keepalive_watchdog.py --stop --force
                                                          terminate a watchdog that ignores the stop request
                                                          (e.g. an older version); its bot and tunnel are
                                                          stopped afterwards
  python scripts\\windows_keepalive_watchdog.py --restart  stop the running watchdog and start a new one in
                                                          the background
  python scripts\\windows_keepalive_watchdog.py --reload   gracefully restart only the bot

Autostart is registered by `python scripts/setup_autostart.py install`.
"""

import argparse
import ctypes
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import psutil

IS_WINDOWS = sys.platform == "win32"

# Win32 power management constants
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040
ES_CONTINUOUS = 0x80000000

# Win32 error codes and process creation flags
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Single-instance mutex
MUTEX_NAME = "Local\\ChannelCloner_24_7_Watchdog_Mutex"
_mutex_handle = None
_KERNEL32 = None

# Paths (module attributes so tests can point them at a temporary directory)
WATCHDOG_SCRIPT = os.path.abspath(__file__)
BASE_DIR = os.path.dirname(os.path.dirname(WATCHDOG_SCRIPT))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")
PID_FILE = os.path.join(LOG_DIR, "watchdog.pid")
STATUS_FILE = os.path.join(LOG_DIR, "watchdog_status.json")
STOP_REQUEST_FILE = os.path.join(LOG_DIR, "watchdog.stop")
RELOAD_REQUEST_FILE = os.path.join(LOG_DIR, "watchdog.reload")
BOT_STDOUT_LOG = os.path.join(LOG_DIR, "bot_stdout.log")
TUNNEL_STDOUT_LOG = os.path.join(LOG_DIR, "cloudflared.log")
APP_PID_FILE = os.path.join(BASE_DIR, "app.pid")
RUN_SCRIPT = os.path.join(BASE_DIR, "run.py")

CONTAINER_NAME = "telegram_channel_cloner"
DEFAULT_TUNNEL_NAME = "miniapp"

CHECK_INTERVAL_SECONDS = 15
# A bot is restarted only after MAX_CONSECUTIVE_FAILURES failed probes spanning at least
# MIN_FAILURE_WINDOW_SECONDS, so a single slow response (GC pause, long DB write) never kills it.
MAX_CONSECUTIVE_FAILURES = 4
MIN_FAILURE_WINDOW_SECONDS = 60
# A freshly spawned bot needs time for imports, DB init and Telegram connections before /health answers
# (cold starts on battery / EcoQoS can take minutes).
STARTUP_GRACE_SECONDS = 240
# Graceful stop: CTRL_BREAK_EVENT first, the whole process tree is killed after this grace period
BOT_STOP_GRACE_SECONDS = 30
TUNNEL_STOP_GRACE_SECONDS = 10
# Pause between stopping and starting the bot on a restart (lets Telegram drop the old long-poll)
RESTART_PAUSE_SECONDS = 2
# A bot that dies within this time after start counts as a crash loop and is restarted with backoff
QUICK_EXIT_SECONDS = 60
MAX_RESPAWN_BACKOFF_SECONDS = 300
CONTAINER_CHECK_INTERVAL_SECONDS = 60
DOCKER_CLI_TIMEOUT_SECONDS = 5
# "degraded" (HTTP 200) is not restarted; it is only reported once it lasts this long
DEGRADED_WARN_SECONDS = 600
# How long --stop waits for the running watchdog to shut down the bot and the tunnel
STOP_WAIT_SECONDS = 90
RELOAD_WAIT_SECONDS = 20
# stdout/stderr logs of supervised children are capped at this size (+ one ".1" generation)
CHILD_LOG_MAX_BYTES = 10 * 1024 * 1024

# Watchdog invocations that only control another watchdog and never supervise anything themselves
_CONTROL_FLAGS = ("--status", "--stop", "--reload", "--restart")

logger = logging.getLogger("WindowsKeepAwakeWatchdog")
_logging_configured = False
_is_running = True


# ---------------------------------------------------------------------------------------------------
# Process-level setup
# ---------------------------------------------------------------------------------------------------

def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_logging() -> None:
    """Attaches the rotating watchdog log (and the console when there is one). Only the CLI calls this,
    so importing the module (tests, tooling) never writes into the production logs/watchdog.log."""
    global _logging_configured
    if _logging_configured:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    logger.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stdout is not None:  # pythonw has no console
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    logger.propagate = False
    _logging_configured = True


def _disable_windows_power_throttling():
    """Opts this process out of Windows 11 EcoQoS/"efficiency mode". Windowless background processes
    (pythonw, autostart launches) are otherwise CPU-throttled, especially on battery, which made cold
    starts take minutes and let the watchdog kill the bot before it became healthy."""
    if not IS_WINDOWS:
        return
    try:
        from ctypes import wintypes

        class _PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        execution_speed, ignore_timer_resolution = 0x1, 0x4
        state = _PowerThrottlingState(1, execution_speed | ignore_timer_resolution, 0)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))
    except Exception:
        pass


def _no_window_flags() -> int:
    """creationflags for helper processes (docker, cloudflared): no console window flashes from pythonw."""
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


# ---------------------------------------------------------------------------------------------------
# Single-instance mutex
# ---------------------------------------------------------------------------------------------------

def _kernel32():
    """kernel32 bound with use_last_error=True, so ctypes.get_last_error() reports the error of the call
    itself (GetLastError() through ctypes.windll can be clobbered by the interpreter in between)."""
    global _KERNEL32
    if _KERNEL32 is None:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    return _KERNEL32


def acquire_single_instance_mutex() -> bool:
    """Acquires the named mutex that guarantees a single watchdog per Windows session.

    Another instance is assumed when the mutex already exists (ERROR_ALREADY_EXISTS), when it exists but
    belongs to a process we may not open, e.g. an elevated watchdog (ERROR_ACCESS_DENIED / NULL handle).
    """
    global _mutex_handle
    if not IS_WINDOWS:
        return get_running_watchdog_pid() is None
    try:
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        last_error = ctypes.get_last_error()
    except Exception as e:
        logger.warning(f"Could not create the single-instance mutex ({e}); falling back to the process check")
        return get_running_watchdog_pid() is None
    if not handle:
        logger.info(f"Single-instance mutex is owned by another watchdog (CreateMutexW error {last_error})")
        return False
    if last_error in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED):
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def release_single_instance_mutex():
    """Releases and closes the named mutex."""
    global _mutex_handle
    if _mutex_handle:
        try:
            kernel32 = _kernel32()
            kernel32.ReleaseMutex(_mutex_handle)
            kernel32.CloseHandle(_mutex_handle)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        _mutex_handle = None


# ---------------------------------------------------------------------------------------------------
# PID files and process identification
# ---------------------------------------------------------------------------------------------------

def _atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug(f"Could not remove {path}", exc_info=True)


def _read_pid_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            match = re.search(r"\d+", f.read())
    except OSError:
        return None
    return int(match.group(0)) if match else None


def write_pid():
    try:
        _atomic_write_text(PID_FILE, str(os.getpid()))
    except OSError:
        logger.debug("Ignored exception", exc_info=True)


def remove_pid():
    """Removes the watchdog PID file, but only when it still names this process."""
    if _read_pid_file(PID_FILE) == os.getpid():
        _remove_file(PID_FILE)


_PYTHON_OPTIONS_WITH_VALUE = {"-W", "-X", "--check-hash-based-pycs"}


def _script_argument(cmdline: List[str]) -> Optional[str]:
    """The script a Python command line executes (first positional argument after interpreter options);
    None for `-c` / `-m` invocations."""
    args = list(cmdline or [])[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-c", "-m"):
            return None
        if arg in _PYTHON_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-") and len(arg) > 1:
            i += 1
            continue
        return arg
    return None


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def cmdline_runs_script(cmdline: List[str], script_path: str, cwd: Optional[str] = None) -> bool:
    """True when `cmdline` is a Python interpreter executing exactly `script_path`. A relative script
    argument is resolved against the process working directory. `pytest tests/test_x.py` or an editor
    that merely has the file open never matches."""
    script = _script_argument(cmdline)
    if not script:
        return False
    if not os.path.isabs(script):
        if not cwd:
            return False
        script = os.path.join(cwd, script)
    return _normalized_path(script) == _normalized_path(script_path)


def _process_runs_script(proc: "psutil.Process", script_path: str) -> bool:
    try:
        cmdline = proc.cmdline()
    except (psutil.Error, OSError):
        return False
    try:
        cwd = proc.cwd()
    except (psutil.Error, OSError):
        cwd = None
    return cmdline_runs_script(cmdline, script_path, cwd)


def _is_watchdog_process(proc: "psutil.Process") -> bool:
    if not _process_runs_script(proc, WATCHDOG_SCRIPT):
        return False
    try:
        cmdline = proc.cmdline()
    except (psutil.Error, OSError):
        return False
    return not any(flag in cmdline for flag in _CONTROL_FLAGS)


def get_running_watchdog_pid() -> Optional[int]:
    """PID of the supervising watchdog process running this exact script, if any (never this process)."""
    own_pid = os.getpid()
    recorded = _read_pid_file(PID_FILE)
    if recorded and recorded != own_pid:
        try:
            if _is_watchdog_process(psutil.Process(recorded)):
                return recorded
        except psutil.Error:
            pass
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.pid == own_pid:
                continue
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("py"):
                continue
            try:
                if _is_watchdog_process(proc):
                    return proc.pid
            except psutil.Error:
                continue
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    return None


# Tolerance between a process start and the moment it writes app.pid (clock granularity)
APP_PID_CREATE_TOLERANCE_SECONDS = 5.0


def find_verified_bot_process() -> Optional["psutil.Process"]:
    """The bot process recorded in app.pid - only if it really runs this project's run.py AND was started
    before app.pid was written. A recycled PID (an unrelated, newer process) is never returned, so it is
    never killed."""
    pid = _read_pid_file(APP_PID_FILE)
    if not pid or pid == os.getpid():
        return None
    try:
        pid_file_mtime = os.path.getmtime(APP_PID_FILE)
        proc = psutil.Process(pid)
        if not _process_runs_script(proc, RUN_SCRIPT):
            return None
        if proc.create_time() > pid_file_mtime + APP_PID_CREATE_TOLERANCE_SECONDS:
            return None
        return proc
    except (psutil.Error, OSError):
        return None


# ---------------------------------------------------------------------------------------------------
# Graceful process-tree stop
# ---------------------------------------------------------------------------------------------------

def console_python_executable() -> str:
    """python.exe next to the running interpreter. The bot must be a console process (with a hidden console,
    CREATE_NO_WINDOW) to receive CTRL_BREAK_EVENT; pythonw.exe has no console and could only be killed."""
    exe = sys.executable or "python"
    folder, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def background_python_executable() -> str:
    """pythonw.exe next to the running interpreter (windowless), falling back to the interpreter itself."""
    exe = sys.executable or "python"
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


# Runs in a short-lived helper process: it attaches to the target's (hidden) console and raises
# CTRL_BREAK_EVENT for the target's process group. The watchdog itself usually runs under pythonw
# without any console, and a process can only signal a console it is attached to.
_CTRL_BREAK_HELPER = (
    "import ctypes, sys\n"
    "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "pid = int(sys.argv[1])\n"
    "kernel32.FreeConsole()\n"
    "if not kernel32.AttachConsole(pid):\n"
    "    sys.exit(2)\n"
    "kernel32.SetConsoleCtrlHandler(None, True)\n"
    "sys.exit(0 if kernel32.GenerateConsoleCtrlEvent(1, pid) else 3)\n"
)


def send_ctrl_break(pid: int) -> bool:
    """Delivers CTRL_BREAK_EVENT (Python: SIGBREAK) to a console process that was started with
    CREATE_NEW_PROCESS_GROUP. Returns False when the target has no console (e.g. pythonw)."""
    if not IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            [console_python_executable(), "-c", _CTRL_BREAK_HELPER, str(pid)],
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        logger.debug("CTRL_BREAK helper failed", exc_info=True)
        return False


def stop_process_tree(pid: int, grace_seconds: float = BOT_STOP_GRACE_SECONDS, graceful: bool = True) -> bool:
    """Stops `pid` and all of its descendants: CTRL_BREAK_EVENT first (graceful shutdown of run.py), then a
    hard kill of whatever is still alive after `grace_seconds`. Returns True when nothing survived."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        logger.warning(f"Cannot inspect process {pid}; not stopping it")
        return False
    try:
        family = root.children(recursive=True)
    except psutil.Error:
        family = []

    if graceful and grace_seconds > 0:
        if send_ctrl_break(pid):
            logger.info(f"Sent CTRL_BREAK to PID {pid}; waiting up to {grace_seconds:.0f}s for a graceful exit")
            try:
                root.wait(timeout=grace_seconds)
            except psutil.TimeoutExpired:
                logger.warning(f"PID {pid} did not exit within {grace_seconds:.0f}s; killing its process tree")
            except psutil.Error:
                pass
        else:
            logger.info(f"PID {pid} cannot receive CTRL_BREAK (no console); stopping it forcefully")

    survivors = []
    for proc in [root] + family:
        try:
            if proc.is_running():
                proc.kill()
                survivors.append(proc)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            logger.debug(f"Could not kill PID {proc.pid}", exc_info=True)
    if not survivors:
        return True
    _, alive = psutil.wait_procs(survivors, timeout=10)
    if alive:
        logger.error(f"Processes still alive after kill: {[p.pid for p in alive]}")
    return not alive


# ---------------------------------------------------------------------------------------------------
# Child stdout/stderr logs (size-capped)
# ---------------------------------------------------------------------------------------------------

def open_child_log(path: str, label: str):
    """Opens the append-only stdout/stderr log of a supervised child; an oversized log is rotated first."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        if os.path.getsize(path) > CHILD_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    handle = open(path, "ab", buffering=0)
    stamp = datetime.now().isoformat(timespec="seconds")
    handle.write(f"\n===== {stamp} watchdog: starting {label} =====\n".encode("utf-8"))
    return handle


def cap_child_log(handle, path: str, max_bytes: Optional[int] = None) -> bool:
    """Keeps a child's log bounded while the child keeps writing to it. The child's inherited handle shares
    the file pointer with ours, so after the content is copied to <path>.1 and the file is truncated, the
    child's next write lands at the start of the (now empty) file."""
    limit = CHILD_LOG_MAX_BYTES if max_bytes is None else max_bytes
    if handle is None or handle.closed:
        return False
    try:
        if os.path.getsize(path) <= limit:
            return False
        shutil.copyfile(path, path + ".1")
        handle.seek(0)
        handle.truncate()
        handle.seek(0)
        return True
    except OSError:
        logger.debug(f"Could not cap {path}", exc_info=True)
        return False


# ---------------------------------------------------------------------------------------------------
# Docker "docker-bot" container (alternative runtime) - conflict detection
# ---------------------------------------------------------------------------------------------------

def docker_bot_container_running(timeout: float = DOCKER_CLI_TIMEOUT_SECONDS) -> bool:
    """True when the docker-compose `docker-bot` container is running. A missing docker CLI, a stopped
    Docker Desktop and a slow daemon all count as "not running"."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTAINER_NAME}", "--filter", "status=running", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def stop_conflicting_container(timeout: float = 90.0) -> bool:
    """Stops (does not remove) the docker-bot container. A manually stopped container is not restarted by
    its `unless-stopped` policy when Docker Desktop starts again."""
    try:
        result = subprocess.run(
            ["docker", "stop", "-t", "30", CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Could not stop container {CONTAINER_NAME}: {e}")
        return False
    if result.returncode != 0:
        logger.error(f"docker stop {CONTAINER_NAME} failed: {(result.stderr or '').strip()}")
    return result.returncode == 0


# ---------------------------------------------------------------------------------------------------
# Keep-awake, network, health
# ---------------------------------------------------------------------------------------------------

def enable_keep_awake():
    """Sets the Win32 thread execution state to Away Mode so CPU and network remain active while locked."""
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
    """Resets the Win32 thread execution state to the default."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logger.info("Keep-Awake deactivated. Default Windows power state restored.")
    except Exception as e:
        logger.warning(f"Could not reset Win32 execution state: {e}")


def keep_network_alive():
    """Performs a lightweight DNS lookup to prevent network adapter power-saving sleep."""
    try:
        socket.getaddrinfo("api.telegram.org", 443, socket.AF_INET, socket.SOCK_STREAM)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def signal_handler(signum, frame):
    global _is_running
    logger.info(f"Received signal {signum}. Shutting down Watchdog...")
    _is_running = False


def _read_env_file_value(name: str) -> Optional[str]:
    """Reads a plain KEY=VALUE entry from the project's .env (the bot resolves its settings the same way;
    .env is not exported into the process environment)."""
    env_path = os.path.join(BASE_DIR, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key.strip() == name:
                    value = value.strip().strip('"').strip("'")
                    return value or None
    except OSError:
        pass
    return None


def _read_env_file_port() -> Optional[int]:
    """Reads PORT from the project's .env."""
    value = _read_env_file_value("PORT")
    return int(value) if value and value.isdigit() else None


def default_tunnel_name() -> str:
    """CLOUDFLARED_TUNNEL_NAME from the environment or .env, else 'miniapp'."""
    return os.getenv("CLOUDFLARED_TUNNEL_NAME") or _read_env_file_value("CLOUDFLARED_TUNNEL_NAME") or DEFAULT_TUNNEL_NAME


def get_health_port() -> int:
    """Port priority: the port the running bot actually bound (logs/health_port.txt),
    then PORT from the environment, then PORT from .env, then 8080."""
    port_file = os.path.join(LOG_DIR, "health_port.txt")
    try:
        with open(port_file, "r", encoding="utf-8") as f:
            val = f.read().strip()
            if val.isdigit():
                return int(val)
    except OSError:
        pass
    env_port = os.getenv("PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    return _read_env_file_port() or 8080


def get_health_url() -> str:
    """Returns the canonical loopback healthcheck URL based on the resolved port."""
    return f"http://127.0.0.1:{get_health_port()}/health"


def check_http_health_detailed() -> Dict[str, Any]:
    """Body of GET /health when it answers HTTP 200 (the process is alive), {} otherwise.
    The bot reports status "ok", or "degraded" while it is still starting or its Telegram polling is down,
    plus the PID of the serving process."""
    port = get_health_port()
    # 127.0.0.1 first; localhost covers hosts where the server only listens on ::1
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{port}/health"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Watchdog/3.0"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    try:
                        data = json.loads(response.read().decode("utf-8"))
                    except ValueError:
                        data = None
                    return data if isinstance(data, dict) and data else {"status": "unknown"}
        except Exception:
            continue
    return {}


def check_http_health() -> bool:
    """Liveness: any HTTP 200 from /health, including status "degraded" (startup, polling outage)."""
    return bool(check_http_health_detailed())


def health_pid_matches(health: Dict[str, Any], supervised_pid: Optional[int]) -> Optional[bool]:
    """Whether /health was answered by the supervised bot (or a process it started, e.g. behind a venv
    launcher). None when either PID is unknown."""
    reported = health.get("pid")
    if not isinstance(reported, int) or not supervised_pid:
        return None
    if reported == supervised_pid:
        return True
    try:
        return reported in {child.pid for child in psutil.Process(supervised_pid).children(recursive=True)}
    except psutil.Error:
        return False


class HealthFailureTracker:
    """Decides when failed health probes justify a restart: at least MAX_CONSECUTIVE_FAILURES consecutive
    failures that span at least MIN_FAILURE_WINDOW_SECONDS."""

    def __init__(self, max_failures: int = MAX_CONSECUTIVE_FAILURES, min_window: float = MIN_FAILURE_WINDOW_SECONDS):
        self.max_failures = max_failures
        self.min_window = min_window
        self.consecutive_failures = 0
        self.first_failure_at: Optional[float] = None

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.first_failure_at = None

    def record_failure(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if self.first_failure_at is None:
            self.first_failure_at = now
        self.consecutive_failures += 1
        return (
            self.consecutive_failures >= self.max_failures
            and (now - self.first_failure_at) >= self.min_window
        )


# ---------------------------------------------------------------------------------------------------
# Supervisors
# ---------------------------------------------------------------------------------------------------

class NativeProcessSupervisor:
    """Supervises `python run.py` (the production bot) as a windowless child process.

    All state changes go through one re-entrant lock: the main loop, the --reload request and the optional
    code watcher thread can never start two bots at the same time.
    """

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.last_spawn_time = 0.0
        self._lock = threading.RLock()
        self._log_handle = None
        self._quick_exits = 0
        self._next_start_at = 0.0
        self._last_refusal_log = 0.0

    def in_startup_grace(self) -> bool:
        return (time.time() - self.last_spawn_time) < STARTUP_GRACE_SECONDS

    def owns_running_process(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def bot_process_running(self) -> bool:
        """Our own child, or a verified run.py recorded in app.pid (e.g. left by a previous watchdog)."""
        return self.owns_running_process() or find_verified_bot_process() is not None

    def supervised_pid(self) -> Optional[int]:
        """PID of the bot this watchdog is responsible for (own child or verified app.pid process)."""
        if self.owns_running_process():
            return self.process.pid
        proc = find_verified_bot_process()
        return proc.pid if proc is not None else None

    def is_running(self) -> bool:
        return self.bot_process_running() or check_http_health()

    def start(self) -> bool:
        """Starts run.py unless a bot is already running, a crash-loop backoff is active, or the docker-bot
        container is running (a second instance would break the running one). Returns True on spawn."""
        with self._lock:
            if self.is_running():
                return False
            now = time.time()
            if now < self._next_start_at:
                return False
            if docker_bot_container_running():
                if now - self._last_refusal_log >= 300:
                    logger.warning(
                        f"Docker container {CONTAINER_NAME} (profile docker-bot) is running; the host bot is NOT "
                        "started to avoid a second instance on the same bot token. Stop the container "
                        "(docker compose --profile docker-bot stop) to return to the native runtime."
                    )
                    self._last_refusal_log = now
                return False
            return self._spawn()

    def _spawn(self) -> bool:
        interpreter = console_python_executable()
        logger.info(f"Spawning native bot: {interpreter} {RUN_SCRIPT}")
        try:
            self._close_log()
            self._log_handle = open_child_log(BOT_STDOUT_LOG, "run.py")
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            creationflags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0
            self.process = subprocess.Popen(
                [interpreter, RUN_SCRIPT],
                cwd=BASE_DIR,
                env=env,
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            logger.error(f"Failed to spawn native bot process: {e}")
            self.process = None
            self._close_log()
            return False
        self.last_spawn_time = time.time()
        logger.info(f"Native bot process spawned with PID {self.process.pid} (output: {BOT_STDOUT_LOG})")
        return True

    def handle_exit(self) -> None:
        """Bookkeeping after our child exited on its own; a bot that keeps dying right after start is
        restarted with exponential backoff instead of every check interval."""
        with self._lock:
            if self.process is None or self.process.poll() is None:
                return
            code = self.process.returncode
            uptime = time.time() - self.last_spawn_time
            self._quick_exits = self._quick_exits + 1 if uptime < QUICK_EXIT_SECONDS else 0
            delay = 0
            if self._quick_exits:
                delay = min(MAX_RESPAWN_BACKOFF_SECONDS, CHECK_INTERVAL_SECONDS * 2 ** (self._quick_exits - 1))
            self._next_start_at = time.time() + delay
            logger.warning(f"Native bot exited (code {code}, uptime {uptime:.0f}s); next start in {delay}s")
            self.process = None
            self._close_log()

    def restart(self, reason: str = "") -> None:
        with self._lock:
            logger.warning(f"Restarting native bot process{' (' + reason + ')' if reason else ''}...")
            self.stop()
            self._next_start_at = 0.0
            time.sleep(RESTART_PAUSE_SECONDS)
            self.start()

    def stop(self) -> None:
        with self._lock:
            if self.owns_running_process():
                stop_process_tree(self.process.pid, BOT_STOP_GRACE_SECONDS)
                try:
                    self.process.wait(timeout=5)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            else:
                # A bot adopted from a previous watchdog run (verified through app.pid)
                proc = find_verified_bot_process()
                if proc is not None:
                    logger.info(f"Stopping bot process {proc.pid} recorded in app.pid")
                    stop_process_tree(proc.pid, BOT_STOP_GRACE_SECONDS)
            self.process = None
            self._close_log()

    def maintain_log(self) -> None:
        cap_child_log(self._log_handle, BOT_STDOUT_LOG)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


def is_named_tunnel_cmdline(cmdline: List[str], tunnel_name: str) -> bool:
    """True for `cloudflared ... tunnel [options] run [options] <tunnel_name>` command lines. Quick tunnels
    (`cloudflared tunnel --url ...`) and other named tunnels do not match."""
    args = list(cmdline or [])[1:]
    lowered = [a.lower() for a in args]
    if "tunnel" not in lowered or "run" not in lowered:
        return False
    run_index = lowered.index("run")
    if run_index < lowered.index("tunnel"):
        return False
    return tunnel_name in args[run_index + 1:]


class CloudflaredTunnelSupervisor:
    """Supervises the named Cloudflare tunnel (`cloudflared tunnel run <name>`) that publishes the Mini App.

    Only a cloudflared process serving exactly this named tunnel counts as running; the watchdog stops only
    the process it started itself.
    """

    def __init__(self, tunnel_name: str = DEFAULT_TUNNEL_NAME):
        self.tunnel_name = tunnel_name
        self.process: Optional[subprocess.Popen] = None
        self._log_handle = None
        self._missing_binary_logged = False
        self.cloudflared_bin = self._find_cloudflared()

    @staticmethod
    def _find_cloudflared() -> Optional[str]:
        candidates = [
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return shutil.which("cloudflared")

    def find_tunnel_process(self) -> Optional["psutil.Process"]:
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                name = (proc.info.get("name") or "").lower()
                if not name.startswith("cloudflared"):
                    continue
                if is_named_tunnel_cmdline(proc.info.get("cmdline") or [], self.tunnel_name):
                    return proc
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        return None

    def is_running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return self.find_tunnel_process() is not None

    def start(self) -> bool:
        if not self.cloudflared_bin:
            if not self._missing_binary_logged:
                logger.warning("cloudflared binary not found. Tunnel supervision paused.")
                self._missing_binary_logged = True
            return False
        if self.is_running():
            return False
        try:
            self._close_log()
            self._log_handle = open_child_log(TUNNEL_STDOUT_LOG, f"cloudflared tunnel run {self.tunnel_name}")
            self.process = subprocess.Popen(
                [self.cloudflared_bin, "tunnel", "run", self.tunnel_name],
                cwd=BASE_DIR,
                creationflags=_no_window_flags(),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
            logger.info(f"Cloudflare Tunnel ({self.tunnel_name}) spawned with PID {self.process.pid}")
            return True
        except Exception as e:
            logger.error(f"Failed to spawn Cloudflare Tunnel process: {e}")
            self.process = None
            self._close_log()
            return False

    def restart(self):
        self.stop()
        time.sleep(2)
        self.start()

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=TUNNEL_STOP_GRACE_SECONDS)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        self.process = None
        self._close_log()

    def maintain_log(self) -> None:
        cap_child_log(self._log_handle, TUNNEL_STDOUT_LOG)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


class CodeChangeWatcher:
    """
    Background code change watcher (development only, --auto-reload).
    Monitors bot/, services/, admin_bot/, config/, database/, run.py, .env and calls on_change_callback
    after a debounce so a burst of saves results in a single restart.
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


# ---------------------------------------------------------------------------------------------------
# CLI control (stop / reload / restart / status)
# ---------------------------------------------------------------------------------------------------

def _write_request(path: str) -> None:
    _atomic_write_text(path, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")


def _consume_request(path: str) -> bool:
    """True (and the request file is removed) when a control request is pending."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug(f"Could not consume {path}", exc_info=True)
        return False


def handle_control_requests(native: NativeProcessSupervisor) -> None:
    """Applies --stop / --reload requests written by another invocation of this script."""
    global _is_running
    if _consume_request(STOP_REQUEST_FILE):
        logger.info("Stop requested via CLI (--stop). Shutting down Watchdog...")
        _is_running = False
        return
    if _consume_request(RELOAD_REQUEST_FILE):
        logger.info("Bot reload requested via CLI (--reload).")
        native.restart(reason="manual reload")


def _force_stop_watchdog(proc: "psutil.Process") -> bool:
    """Terminates a watchdog that ignored the stop request, then does what its shutdown would have done:
    the bot it started is stopped gracefully, the tunnel and any other child afterwards."""
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except psutil.TimeoutExpired:
        proc.kill()
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as e:
        print(f"⚠️ Could not terminate Watchdog (PID {proc.pid}): {e}")
        return False

    for child in children:
        try:
            if child.is_running() and _process_runs_script(child, RUN_SCRIPT):
                print(f"⏳ Stopping bot process {child.pid} gracefully...")
                stop_process_tree(child.pid, BOT_STOP_GRACE_SECONDS)
        except psutil.Error:
            continue
    for child in children:
        try:
            if child.is_running():
                child.kill()
        except psutil.Error:
            continue
    orphan_bot = find_verified_bot_process()
    if orphan_bot is not None:
        print(f"⏳ Stopping bot process {orphan_bot.pid} recorded in app.pid...")
        stop_process_tree(orphan_bot.pid, BOT_STOP_GRACE_SECONDS)
    _remove_file(PID_FILE)
    print(f"✅ Watchdog (PID {proc.pid}) terminated; its bot and tunnel processes were stopped.")
    return True


def stop_watchdog(force: bool = False, timeout: float = STOP_WAIT_SECONDS) -> bool:
    """Asks the running watchdog to shut down (bot and tunnel included) and waits for it. The watchdog is
    never killed unless `force` is set, so its own cleanup always runs."""
    pid = get_running_watchdog_pid()
    if not pid:
        print("ℹ️ No active Watchdog process found.")
        return True
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    if force:
        logger.warning(f"Force-stopping Watchdog PID {pid} (requested via CLI)")
        return _force_stop_watchdog(proc)

    logger.info(f"Graceful stop of Watchdog PID {pid} requested via CLI")
    _write_request(STOP_REQUEST_FILE)
    print(f"⏳ Stop requested; waiting up to {timeout:.0f}s for Watchdog (PID {pid}) to stop the bot and tunnel...")
    try:
        proc.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        _remove_file(STOP_REQUEST_FILE)
        print("⚠️ The Watchdog did not answer the stop request (probably an older version).")
        print("   Run again with --force to terminate it; its bot and tunnel are stopped afterwards.")
        return False
    except psutil.NoSuchProcess:
        pass
    _remove_file(STOP_REQUEST_FILE)
    print(f"✅ Watchdog process (PID {pid}) stopped.")
    return True


def request_reload(timeout: float = RELOAD_WAIT_SECONDS) -> bool:
    """Asks the running watchdog to restart the bot gracefully (the watchdog itself keeps running)."""
    pid = get_running_watchdog_pid()
    if not pid:
        print("ℹ️ The Watchdog is not running; nothing to reload.")
        return False
    _write_request(RELOAD_REQUEST_FILE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(RELOAD_REQUEST_FILE):
            print(f"✅ Reload accepted by Watchdog (PID {pid}); the bot is restarting gracefully.")
            return True
        time.sleep(0.5)
    _remove_file(RELOAD_REQUEST_FILE)
    print("⚠️ The Watchdog did not pick up the reload request (probably an older version). Use --restart.")
    return False


# Runs Win32_Process.Create through CIM. The command line and working directory travel through
# environment variables, so paths with spaces or quotes need no escaping.
_WMI_CREATE_SCRIPT = (
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
    "-Arguments @{ CommandLine = $env:CC_SPAWN_CMD; CurrentDirectory = $env:CC_SPAWN_CWD }; "
    "if ($r.ReturnValue -ne 0) { exit [int]$r.ReturnValue }; [Console]::Out.Write($r.ProcessId)"
)


def _create_process_via_wmi(cmd: List[str], cwd: str) -> Optional[int]:
    """Lets the WMI service create the process. It then belongs to no job of the caller."""
    env = dict(os.environ, CC_SPAWN_CMD=subprocess.list2cmdline(cmd), CC_SPAWN_CWD=cwd)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _WMI_CREATE_SCRIPT],
            env=env, capture_output=True, text=True, timeout=60, creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"❌ WMI process creation failed: {e}")
        return None
    if result.returncode != 0:
        print(f"❌ WMI process creation failed (Win32_Process.Create returned {result.returncode})")
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def spawn_outside_job(cmd: List[str], cwd: str, label: str = "the Watchdog") -> Optional[int]:
    """Starts `cmd` in the background so that it survives the program that started it.

    Terminals, IDEs and agent sessions often run their commands inside a Windows job object that is killed
    as a whole when the tool exits, so a watchdog started from there would take the bot and the tunnel down
    with it. CREATE_BREAKAWAY_FROM_JOB leaves the caller's job; when the job forbids that, the WMI service
    creates the process instead."""
    creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, creationflags=creationflags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.pid
    except OSError as e:
        if getattr(e, "winerror", None) != ERROR_ACCESS_DENIED:
            print(f"❌ Could not start {label}: {e}")
            return None
    return _create_process_via_wmi(cmd, cwd)


def spawn_detached_watchdog(extra_args: Optional[List[str]] = None) -> Optional[int]:
    """Starts a new windowless watchdog in the background (used by --restart), outside the caller's job."""
    cmd = [background_python_executable(), WATCHDOG_SCRIPT] + list(extra_args or [])
    if not IS_WINDOWS:
        try:
            return subprocess.Popen(cmd, cwd=BASE_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True).pid
        except OSError as e:
            print(f"❌ Could not start the Watchdog: {e}")
            return None
    return spawn_outside_job(cmd, BASE_DIR)


def save_status(mode: str, healthy: bool, consecutive_failures: int, extra: Optional[Dict[str, Any]] = None):
    status_payload = {
        "pid": os.getpid(),
        "mode": mode,
        "last_check_utc": datetime.now(timezone.utc).isoformat(),
        "healthy": healthy,
        "consecutive_failures": consecutive_failures,
        "win32_away_mode": True
    }
    if extra:
        status_payload.update(extra)
    try:
        _atomic_write_text(STATUS_FILE, json.dumps(status_payload, indent=2))
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def show_status(tunnel_name: str = DEFAULT_TUNNEL_NAME):
    pid = get_running_watchdog_pid()
    is_alive = pid is not None
    health_data = check_http_health_detailed()
    bot_proc = find_verified_bot_process()
    tunnel = CloudflaredTunnelSupervisor(tunnel_name)
    tunnel_proc = tunnel.find_tunnel_process()
    container_running = docker_bot_container_running()

    print("=" * 65)
    print("   TELEGRAM CHANNEL CLONER — 24/7 WATCHDOG TELEMETRY")
    print("=" * 65)
    print(f"  Watchdog Supervisor : {'🟢 RUNNING (PID ' + str(pid) + ')' if is_alive else '🔴 STOPPED'}")
    print(f"  Win32 Away Mode     : {'🟢 Active (Lock & Lid-Close Protected)' if is_alive else '🔴 Inactive'}")
    print(f"  Bot Process (app.pid): {'🟢 PID ' + str(bot_proc.pid) if bot_proc else '⚪ not found'}")
    if not health_data:
        health_line = "🔴 NOT RESPONDING"
    elif health_data.get("status") == "ok":
        health_line = "🟢 200 OK"
    else:
        health_line = f"🟡 200 ({health_data.get('status')})"
    print(f"  HTTP Healthcheck    : {health_line}")
    if health_data:
        print(f"  Service Status      : {health_data.get('service', 'N/A')} ({health_data.get('bot', 'N/A')}, "
              f"PID {health_data.get('pid', 'N/A')})")
        print(f"  Telethon Account    : {'🟢 Connected' if health_data.get('telethon_connected') else '🔴 Disconnected'}")
        if "uptime" in health_data:
            print(f"  System Uptime       : {health_data.get('uptime')}")
    print(f"  Cloudflare Tunnel   : {'🟢 ' + tunnel_name + ' (PID ' + str(tunnel_proc.pid) + ')' if tunnel_proc else '🔴 ' + tunnel_name + ' not running'}")
    if container_running:
        print(f"  ⚠️ Docker container {CONTAINER_NAME} is RUNNING — only one bot instance may run!")
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


# ---------------------------------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------------------------------

def run_watchdog(args) -> int:
    global _is_running
    if not IS_WINDOWS:
        print("This watchdog supervises the Windows host runtime only. On Linux use Docker (profile docker-bot).")
        return 1

    _disable_windows_power_throttling()
    if not acquire_single_instance_mutex():
        print("⚠️ Another instance of 24/7 Watchdog is already running (Mutex held). Exiting cleanly.")
        logger.info("Watchdog invoked but an instance is already active. Exiting.")
        return 0

    _is_running = True
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal_handler)
            except (ValueError, OSError):
                logger.debug(f"Could not install {sig_name} handler", exc_info=True)

    # Requests left over from a previous run must not stop or reload this fresh instance
    _remove_file(STOP_REQUEST_FILE)
    _remove_file(RELOAD_REQUEST_FILE)
    write_pid()

    stop_container_on_conflict = not args.keep_conflicting_container
    logger.info("=" * 70)
    logger.info("🚀 24/7 Windows Keep-Awake & Auto-Recovery Watchdog started (native host runtime).")
    logger.info("Win32 Away Mode engaged: System will NOT sleep when laptop is locked (Win+L).")
    logger.info(f"Check interval: {args.interval}s | Tunnel: {'disabled' if args.no_tunnel else args.tunnel_name}")
    logger.info("=" * 70)

    native_supervisor = NativeProcessSupervisor()
    tunnel_supervisor = None if args.no_tunnel else CloudflaredTunnelSupervisor(args.tunnel_name)
    failures = HealthFailureTracker()
    code_watcher = None

    try:
        native_supervisor.start()
        if tunnel_supervisor:
            tunnel_supervisor.start()

        # Auto-Reload Code Watcher: opt-in only. Restarting production on every file save interrupts
        # in-flight clones and can boot half-edited code.
        if args.auto_reload and not args.no_reload:
            def on_code_change(changed_files):
                failures.reset()
                native_supervisor.restart(reason="source code changed")

            code_watcher = CodeChangeWatcher(base_dir=BASE_DIR, on_change_callback=on_code_change, debounce_seconds=1.5)
            code_watcher.start()

        cycle_counter = 0
        last_container_check = 0.0
        last_pid_mismatch_log = 0.0
        last_degraded_log = 0.0
        degraded_since: Optional[float] = None
        while _is_running:
            try:
                cycle_counter += 1

                # 1. Renew Win32 Away Mode execution state every cycle
                enable_keep_awake()

                # 2. Prevent network adapter idle sleep every 4 cycles (~60s)
                if cycle_counter % 4 == 0:
                    keep_network_alive()

                # 3. Bot process: restart after an unexpected exit (with crash-loop backoff)
                if native_supervisor.process is not None and native_supervisor.process.poll() is not None:
                    native_supervisor.handle_exit()
                    failures.reset()
                if not native_supervisor.is_running():
                    native_supervisor.start()

                # 3.5. A docker-bot container next to a running host bot is a second instance
                now = time.time()
                if now - last_container_check >= CONTAINER_CHECK_INTERVAL_SECONDS:
                    last_container_check = now
                    if native_supervisor.bot_process_running() and docker_bot_container_running():
                        logger.critical(
                            f"Second bot instance detected: Docker container {CONTAINER_NAME} runs next to the "
                            "host bot (same token -> TelegramConflictError, duplicate posts)."
                        )
                        if stop_container_on_conflict:
                            if stop_conflicting_container():
                                logger.warning(f"Container {CONTAINER_NAME} stopped; the host bot stays the only instance.")
                        else:
                            logger.critical(f"Stop it manually: docker stop {CONTAINER_NAME}")

                # 3.6. Named Cloudflare tunnel for the Mini App
                if tunnel_supervisor and not tunnel_supervisor.is_running():
                    logger.warning(f"⚠️ Cloudflare Tunnel ({tunnel_supervisor.tunnel_name}) is not running! Starting...")
                    tunnel_supervisor.start()

                # 4. HTTP health (liveness = HTTP 200, also while "degraded"); restart only after a sustained outage
                health = check_http_health_detailed()
                healthy = bool(health)
                if healthy:
                    if failures.consecutive_failures:
                        logger.info("✅ Health restored: Bot is running and responsive.")
                    failures.reset()
                    now = time.time()
                    if health_pid_matches(health, native_supervisor.supervised_pid()) is False \
                            and now - last_pid_mismatch_log >= 300:
                        last_pid_mismatch_log = now
                        logger.error(
                            f"/health on port {get_health_port()} is answered by PID {health.get('pid')}, not by the "
                            f"supervised bot (PID {native_supervisor.supervised_pid()}): another process holds the port."
                        )
                    if health.get("status") == "degraded":
                        degraded_since = degraded_since or now
                        if now - degraded_since >= DEGRADED_WARN_SECONDS and now - last_degraded_log >= 300:
                            last_degraded_log = now
                            logger.warning(
                                f"Bot reports status 'degraded' for {int(now - degraded_since)}s "
                                f"(db_ready={health.get('db_ready')}, polling_alive={health.get('polling_alive')})."
                            )
                    else:
                        degraded_since = None
                elif native_supervisor.in_startup_grace():
                    logger.info("⏳ Bot is still starting up; healthcheck failure ignored during grace period.")
                elif not native_supervisor.bot_process_running():
                    failures.reset()
                else:
                    should_restart = failures.record_failure()
                    logger.warning(f"⚠️ Healthcheck failed (Fail #{failures.consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if should_restart:
                        logger.error("Health checks failed for a sustained period. Restarting the bot.")
                        native_supervisor.restart(reason="health check failures")
                        failures.reset()

                save_status("native", healthy, failures.consecutive_failures)
                native_supervisor.maintain_log()
                if tunnel_supervisor:
                    tunnel_supervisor.maintain_log()

                # Periodic heartbeat log every 20 cycles (~5 minutes)
                if cycle_counter % 20 == 0:
                    telethon_st = "Connected" if health.get("telethon_connected") else "Disconnected"
                    health_st = str(health.get("status", "")).upper() if health else "DOWN"
                    logger.info(f"🛡️ Watchdog Heartbeat: Mode=NATIVE | Health={health_st} | Bot PID={health.get('pid', '-')} "
                                f"| Telethon={telethon_st} | Win32 Away Mode=Active")

            except Exception as loop_err:
                logger.error(f"Unexpected error in Watchdog loop: {loop_err}", exc_info=True)

            # Sleep in small chunks for responsive termination and CLI requests
            for _ in range(max(1, args.interval)):
                if not _is_running:
                    break
                handle_control_requests(native_supervisor)
                if not _is_running:
                    break
                time.sleep(1)

    finally:
        if code_watcher:
            code_watcher.stop()
        native_supervisor.stop()
        if tunnel_supervisor:
            tunnel_supervisor.stop()
        disable_keep_awake()
        release_single_instance_mutex()
        remove_pid()
        _remove_file(STOP_REQUEST_FILE)
        _remove_file(RELOAD_REQUEST_FILE)
        logger.info("Watchdog terminated cleanly.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram Channel Cloner 24/7 Windows Watchdog (native host runtime)")
    parser.add_argument("--status", action="store_true", help="Display current Watchdog, bot and tunnel status")
    parser.add_argument("--stop", action="store_true", help="Gracefully stop the running Watchdog (bot and tunnel too)")
    parser.add_argument("--restart", action="store_true", help="Stop the running Watchdog and start a new one in the background")
    parser.add_argument("--force", action="store_true",
                        help="With --stop/--restart: terminate a Watchdog that does not answer the stop request")
    parser.add_argument("--reload", action="store_true", help="Gracefully restart only the bot")
    parser.add_argument("--auto-reload", action="store_true",
                        help="Development only: restart the bot whenever source files change")
    parser.add_argument("--no-reload", action="store_true", help="(default) Do not hot-reload on code changes")
    parser.add_argument("--mode", choices=["native"], default="native",
                        help="Only the native host runtime is supervised. To run the bot in Docker use "
                             "`docker compose --profile docker-bot up -d` and do NOT run this watchdog.")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_SECONDS, help="Check interval in seconds")
    parser.add_argument("--tunnel-name", default=None,
                        help="Named Cloudflare tunnel to supervise (default: CLOUDFLARED_TUNNEL_NAME from the "
                             "environment or .env, else 'miniapp')")
    parser.add_argument("--no-tunnel", action="store_true", help="Do not supervise cloudflared")
    parser.add_argument("--keep-conflicting-container", action="store_true",
                        help="Only log (do not stop) a running docker-bot container that duplicates the host bot")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_args)
    if not args.tunnel_name:
        args.tunnel_name = default_tunnel_name()
    _reconfigure_stdio()

    if args.status:
        show_status(args.tunnel_name)
        return 0

    configure_logging()
    if args.reload:
        return 0 if request_reload() else 1
    if args.stop:
        return 0 if stop_watchdog(force=args.force) else 1
    if args.restart:
        if not stop_watchdog(force=args.force):
            return 1
        forwarded = [a for a in raw_args if a not in ("--restart", "--force")]
        new_pid = spawn_detached_watchdog(forwarded)
        if new_pid is None:
            return 1
        print(f"✅ Watchdog restarted in the background (PID {new_pid}).")
        return 0
    return run_watchdog(args)


if __name__ == "__main__":
    sys.exit(main())
