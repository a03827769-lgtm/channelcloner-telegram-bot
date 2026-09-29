"""
Tests for the infrastructure / deployment fixes (area F6).

Safety: every watchdog test redirects the watchdog's paths to tmp_path and only ever inspects or stops
child processes started by the test itself — the production watchdog, bot and tunnel are never touched.
Autostart tests use an in-memory backend (no registry / Task Scheduler / Startup folder access), deploy
tests never open SSH connections, and webhook tests never run git or docker.
"""
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

import scripts.windows_keepalive_watchdog as watchdog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only (console control events)")


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------

def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class _Children:
    """Starts helper processes and guarantees they are gone after the test."""

    def __init__(self):
        self.procs = []

    def spawn(self, args, **kwargs) -> subprocess.Popen:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        kwargs.setdefault("stderr", subprocess.DEVNULL)
        proc = subprocess.Popen(args, **kwargs)
        self.procs.append(proc)
        return proc

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass


@pytest.fixture
def children():
    c = _Children()
    yield c
    c.cleanup()


_SLEEPER = "import time\nwhile True:\n    time.sleep(0.2)\n"


def _visible(pid: int, script: Path) -> bool:
    return str(script) in " ".join(psutil.Process(pid).cmdline())


# ---------------------------------------------------------------------------------------------------
# Watchdog: process identification (H6)
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmdline,cwd,expected", [
    (["pythonw.exe", "{script}"], None, True),
    (["python.exe", "-u", "{script}", "--auto-reload"], None, True),
    (["python.exe", "scripts/windows_keepalive_watchdog.py"], "{root}", True),
    (["python.exe", "-m", "pytest", "tests/test_windows_keepalive_watchdog.py"], "{root}", False),
    (["python.exe", "-c", "import scripts.windows_keepalive_watchdog"], "{root}", False),
    (["python.exe", "other.py", "{script}"], "{root}", False),
    (["python.exe", "scripts/windows_keepalive_watchdog.py"], None, False),
])
def test_cmdline_runs_script_matches_the_exact_script_only(cmdline, cwd, expected):
    script = str(PROJECT_ROOT / "scripts" / "windows_keepalive_watchdog.py")
    cmdline = [part.replace("{script}", script) for part in cmdline]
    cwd = cwd.replace("{root}", str(PROJECT_ROOT)) if cwd else None
    assert watchdog.cmdline_runs_script(cmdline, script, cwd) is expected


def test_get_running_watchdog_pid_ignores_lookalikes(tmp_path, monkeypatch, children):
    fake_watchdog = tmp_path / "windows_keepalive_watchdog.py"
    fake_watchdog.write_text(_SLEEPER, encoding="utf-8")
    monkeypatch.setattr(watchdog, "WATCHDOG_SCRIPT", str(fake_watchdog))
    monkeypatch.setattr(watchdog, "PID_FILE", str(tmp_path / "watchdog.pid"))

    lookalike = children.spawn([sys.executable, "-c", "import time; time.sleep(60)",
                                str(tmp_path / "test_windows_keepalive_watchdog.py")])
    status_cli = children.spawn([sys.executable, str(fake_watchdog), "--status"])
    assert _wait_for(lambda: _visible(status_cli.pid, fake_watchdog))
    assert _wait_for(lambda: psutil.Process(lookalike.pid).cmdline())
    assert watchdog.get_running_watchdog_pid() is None

    (tmp_path / "watchdog.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert watchdog.get_running_watchdog_pid() is None  # never the calling process itself

    real = children.spawn([sys.executable, str(fake_watchdog)])
    assert _wait_for(lambda: _visible(real.pid, fake_watchdog))
    assert watchdog.get_running_watchdog_pid() == real.pid


def test_find_verified_bot_process_checks_cmdline_and_start_time(tmp_path, monkeypatch, children):
    fake_run = tmp_path / "run.py"
    fake_run.write_text(_SLEEPER, encoding="utf-8")
    app_pid = tmp_path / "app.pid"
    monkeypatch.setattr(watchdog, "RUN_SCRIPT", str(fake_run))
    monkeypatch.setattr(watchdog, "APP_PID_FILE", str(app_pid))
    assert watchdog.find_verified_bot_process() is None  # no app.pid

    bot = children.spawn([sys.executable, str(fake_run)])
    other = children.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    assert _wait_for(lambda: _visible(bot.pid, fake_run))

    app_pid.write_text(f"{bot.pid}\n", encoding="utf-8")
    found = watchdog.find_verified_bot_process()
    assert found is not None and found.pid == bot.pid

    # app.pid written long before this process started: the PID was recycled -> never trusted/killed
    stale = psutil.Process(bot.pid).create_time() - 3600
    os.utime(app_pid, (stale, stale))
    assert watchdog.find_verified_bot_process() is None

    app_pid.write_text(str(other.pid), encoding="utf-8")  # alive, but not run.py
    assert watchdog.find_verified_bot_process() is None
    app_pid.write_text(str(os.getpid()), encoding="utf-8")  # the caller itself
    assert watchdog.find_verified_bot_process() is None


# ---------------------------------------------------------------------------------------------------
# Watchdog: graceful stop (M2)
# ---------------------------------------------------------------------------------------------------

_GRACEFUL_CHILD = """
import signal, sys, time
marker = sys.argv[1]
def on_break(signum, frame):
    with open(marker, "w") as f:
        f.write("graceful")
    sys.exit(0)
signal.signal(signal.SIGBREAK, on_break)
open(marker + ".ready", "w").close()
while True:
    time.sleep(0.1)
"""

_STUBBORN_CHILD = """
import signal, sys, time
signal.signal(signal.SIGBREAK, signal.SIG_IGN)
open(sys.argv[1] + ".ready", "w").close()
while True:
    time.sleep(0.1)
"""


def _spawn_console_child(children, script: Path, marker: Path) -> subprocess.Popen:
    return children.spawn(
        [watchdog.console_python_executable(), str(script), str(marker)],
        creationflags=watchdog.CREATE_NO_WINDOW | watchdog.CREATE_NEW_PROCESS_GROUP,
    )


@windows_only
def test_stop_process_tree_sends_ctrl_break_before_killing(tmp_path, children):
    script = tmp_path / "graceful_child.py"
    script.write_text(_GRACEFUL_CHILD, encoding="utf-8")
    marker = tmp_path / "stopped.txt"
    proc = _spawn_console_child(children, script, marker)
    assert _wait_for(lambda: Path(str(marker) + ".ready").exists())

    assert watchdog.stop_process_tree(proc.pid, grace_seconds=20) is True
    assert proc.wait(timeout=10) == 0
    assert marker.read_text(encoding="utf-8") == "graceful"


@windows_only
def test_stop_process_tree_kills_after_grace_period(tmp_path, children):
    script = tmp_path / "stubborn_child.py"
    script.write_text(_STUBBORN_CHILD, encoding="utf-8")
    marker = tmp_path / "stubborn"
    proc = _spawn_console_child(children, script, marker)
    assert _wait_for(lambda: Path(str(marker) + ".ready").exists())

    started = time.time()
    assert watchdog.stop_process_tree(proc.pid, grace_seconds=1) is True
    assert proc.wait(timeout=10) != 0
    assert time.time() - started < 15


def test_stop_process_tree_of_missing_process_is_a_no_op():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    assert watchdog.stop_process_tree(dead.pid, grace_seconds=1) is True


def test_bot_is_spawned_as_console_process_in_its_own_group(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "BOT_STDOUT_LOG", str(tmp_path / "bot_stdout.log"))
    popen = MagicMock()
    popen.return_value.pid = 4321
    monkeypatch.setattr(watchdog.subprocess, "Popen", popen)
    supervisor = watchdog.NativeProcessSupervisor()
    try:
        assert supervisor._spawn() is True
        args, kwargs = popen.call_args
        assert args[0] == [watchdog.console_python_executable(), watchdog.RUN_SCRIPT]
        assert not args[0][0].lower().endswith("pythonw.exe")
        if sys.platform == "win32":
            assert kwargs["creationflags"] & watchdog.CREATE_NEW_PROCESS_GROUP
            assert kwargs["creationflags"] & watchdog.CREATE_NO_WINDOW
        assert kwargs["stdout"] is supervisor._log_handle  # not DEVNULL: output goes to logs/bot_stdout.log
        assert kwargs["stderr"] == subprocess.STDOUT
    finally:
        supervisor._close_log()


# ---------------------------------------------------------------------------------------------------
# Watchdog: --stop / --reload never terminate the watchdog (M2)
# ---------------------------------------------------------------------------------------------------

_FAKE_WATCHDOG = """
import os, sys, time
stop_file = sys.argv[1]
deadline = time.time() + 60
while time.time() < deadline:
    if os.path.exists(stop_file):
        os.remove(stop_file)
        sys.exit(0)
    time.sleep(0.05)
sys.exit(1)
"""


def test_stop_watchdog_requests_a_clean_shutdown(tmp_path, monkeypatch, children):
    fake = tmp_path / "wd.py"
    fake.write_text(_FAKE_WATCHDOG, encoding="utf-8")
    stop_file = tmp_path / "watchdog.stop"
    monkeypatch.setattr(watchdog, "WATCHDOG_SCRIPT", str(fake))
    monkeypatch.setattr(watchdog, "PID_FILE", str(tmp_path / "watchdog.pid"))
    monkeypatch.setattr(watchdog, "STOP_REQUEST_FILE", str(stop_file))
    proc = children.spawn([sys.executable, str(fake), str(stop_file)])
    assert _wait_for(lambda: watchdog.get_running_watchdog_pid() == proc.pid)

    with patch.object(psutil.Process, "terminate") as terminate, patch.object(psutil.Process, "kill") as kill:
        assert watchdog.stop_watchdog(timeout=30) is True
        terminate.assert_not_called()
        kill.assert_not_called()
    assert proc.wait(timeout=10) == 0  # left through its own shutdown path
    assert not stop_file.exists()


def test_control_requests_are_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STOP_REQUEST_FILE", str(tmp_path / "watchdog.stop"))
    monkeypatch.setattr(watchdog, "RELOAD_REQUEST_FILE", str(tmp_path / "watchdog.reload"))
    monkeypatch.setattr(watchdog, "_is_running", True)
    native = MagicMock()

    watchdog.handle_control_requests(native)
    native.restart.assert_not_called()

    watchdog._write_request(watchdog.RELOAD_REQUEST_FILE)
    watchdog.handle_control_requests(native)
    native.restart.assert_called_once()
    assert not os.path.exists(watchdog.RELOAD_REQUEST_FILE)
    assert watchdog._is_running is True

    watchdog._write_request(watchdog.STOP_REQUEST_FILE)
    watchdog.handle_control_requests(native)
    assert watchdog._is_running is False
    assert not os.path.exists(watchdog.STOP_REQUEST_FILE)


# ---------------------------------------------------------------------------------------------------
# Watchdog: one bot instance only (C1)
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("outcome,expected", [
    ((0, "f00dbabe1234\n"), True),
    ((0, ""), False),
    ((1, ""), False),                      # Docker Desktop not running
    (FileNotFoundError("docker"), False),  # docker CLI not installed
    (subprocess.TimeoutExpired("docker", 5), False),
])
def test_docker_bot_container_detection(outcome, expected, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["docker", "ps"]
        assert f"name={watchdog.CONTAINER_NAME}" in cmd and "status=running" in cmd
        assert kwargs["timeout"] <= 10
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(cmd, outcome[0], stdout=outcome[1], stderr="")

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    assert watchdog.docker_bot_container_running() is expected


def test_native_bot_is_not_started_while_container_runs(monkeypatch):
    supervisor = watchdog.NativeProcessSupervisor()
    monkeypatch.setattr(watchdog, "find_verified_bot_process", lambda: None)
    monkeypatch.setattr(watchdog, "check_http_health", lambda: False)
    monkeypatch.setattr(watchdog, "docker_bot_container_running", lambda timeout=5: True)
    spawn = MagicMock(return_value=True)
    monkeypatch.setattr(supervisor, "_spawn", spawn)

    assert supervisor.start() is False
    spawn.assert_not_called()

    monkeypatch.setattr(watchdog, "docker_bot_container_running", lambda timeout=5: False)
    assert supervisor.start() is True
    spawn.assert_called_once()


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_health_200_is_alive_even_when_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(watchdog, "BASE_DIR", str(tmp_path))
    body = json.dumps({"status": "degraded", "pid": 1234, "polling_alive": False}).encode()
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", lambda req, timeout=5: _FakeResponse(200, body))
    assert watchdog.check_http_health() is True
    assert watchdog.check_http_health_detailed()["status"] == "degraded"

    def refused(req, timeout=5):
        raise ConnectionRefusedError()

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", refused)
    assert watchdog.check_http_health() is False
    assert watchdog.check_http_health_detailed() == {}


def test_health_pid_is_matched_against_the_supervised_process_tree(children):
    assert watchdog.health_pid_matches({"pid": os.getpid()}, os.getpid()) is True
    assert watchdog.health_pid_matches({}, os.getpid()) is None
    assert watchdog.health_pid_matches({"pid": 1}, None) is None
    launcher = children.spawn([sys.executable, "-c",
                               "import subprocess, sys, time\n"
                               "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                               "time.sleep(60)\n"])
    assert _wait_for(lambda: psutil.Process(launcher.pid).children())
    grandchild = psutil.Process(launcher.pid).children()[0]
    try:
        assert watchdog.health_pid_matches({"pid": grandchild.pid}, launcher.pid) is True  # e.g. venv launcher
        assert watchdog.health_pid_matches({"pid": os.getpid()}, launcher.pid) is False
    finally:
        grandchild.kill()


def test_watchdog_never_starts_docker_containers():
    source = (PROJECT_ROOT / "scripts" / "windows_keepalive_watchdog.py").read_text(encoding="utf-8")
    assert '"compose", "up"' not in source and '"start", CONTAINER_NAME' not in source
    assert '"restart", CONTAINER_NAME' not in source
    ps1 = (PROJECT_ROOT / "scripts" / "watchdog.ps1").read_text(encoding="utf-8")
    assert "docker start" not in ps1 and "docker restart" not in ps1


# ---------------------------------------------------------------------------------------------------
# Watchdog: restart serialisation (M3), failure threshold (M2), crash-loop backoff
# ---------------------------------------------------------------------------------------------------

def test_concurrent_restarts_are_serialized(monkeypatch):
    supervisor = watchdog.NativeProcessSupervisor()
    monkeypatch.setattr(watchdog, "RESTART_PAUSE_SECONDS", 0)
    counter_lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def critical_section(*args, **kwargs):
        with counter_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        with counter_lock:
            state["active"] -= 1

    monkeypatch.setattr(supervisor, "stop", critical_section)
    monkeypatch.setattr(supervisor, "start", critical_section)
    threads = [threading.Thread(target=supervisor.restart, kwargs={"reason": f"thread {i}"}) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert state["max_active"] == 1


def test_health_failures_must_be_sustained_before_restart():
    assert watchdog.MAX_CONSECUTIVE_FAILURES >= 4
    assert watchdog.MIN_FAILURE_WINDOW_SECONDS >= 60
    assert watchdog.STARTUP_GRACE_SECONDS >= 90
    tracker = watchdog.HealthFailureTracker(max_failures=4, min_window=60)
    assert [tracker.record_failure(now=t) for t in (0, 15, 30, 45)] == [False, False, False, False]
    assert tracker.record_failure(now=60) is True
    tracker.reset()
    assert tracker.consecutive_failures == 0
    assert tracker.record_failure(now=100) is False


def test_quick_exits_back_off(monkeypatch):
    supervisor = watchdog.NativeProcessSupervisor()
    for expected_min_delay in (watchdog.CHECK_INTERVAL_SECONDS, 2 * watchdog.CHECK_INTERVAL_SECONDS):
        dead = MagicMock()
        dead.poll.return_value = 1
        dead.returncode = 1
        supervisor.process = dead
        supervisor.last_spawn_time = time.time()
        supervisor.handle_exit()
        assert supervisor.process is None
        assert supervisor._next_start_at - time.time() >= expected_min_delay - 1
    monkeypatch.setattr(watchdog, "find_verified_bot_process", lambda: None)
    monkeypatch.setattr(watchdog, "check_http_health", lambda: False)
    spawn = MagicMock(return_value=True)
    monkeypatch.setattr(supervisor, "_spawn", spawn)
    assert supervisor.start() is False  # still backing off
    spawn.assert_not_called()


# ---------------------------------------------------------------------------------------------------
# Watchdog: child logs (M2) and the named tunnel (M12)
# ---------------------------------------------------------------------------------------------------

def test_child_log_is_capped(tmp_path):
    log = tmp_path / "bot_stdout.log"
    handle = watchdog.open_child_log(str(log), "run.py")
    try:
        handle.write(b"x" * 4096)
        assert watchdog.cap_child_log(handle, str(log), max_bytes=1024) is True
        assert log.stat().st_size == 0
        assert (tmp_path / "bot_stdout.log.1").stat().st_size > 4096
        assert watchdog.cap_child_log(handle, str(log), max_bytes=1024) is False
    finally:
        handle.close()


@windows_only
def test_child_keeps_writing_at_the_start_after_cap(tmp_path, children):
    log = tmp_path / "child.log"
    handle = watchdog.open_child_log(str(log), "writer")
    try:
        writer = children.spawn(
            [sys.executable, "-u", "-c",
             "import sys, time\nfor i in range(300):\n    sys.stdout.write('line %03d\\n' % i)\n    time.sleep(0.01)\n"],
            stdout=handle, stderr=subprocess.STDOUT, creationflags=watchdog.CREATE_NO_WINDOW,
        )
        assert _wait_for(lambda: log.stat().st_size > 1200)
        assert watchdog.cap_child_log(handle, str(log), max_bytes=100) is True
        assert writer.wait(timeout=60) == 0
    finally:
        handle.close()
    data = log.read_bytes()
    assert b"\x00" not in data  # no sparse gap: the child continued at offset 0
    assert data.rstrip().endswith(b"line 299")
    assert len(data) < 300 * len(b"line 000\n")


@pytest.mark.parametrize("cmdline,expected", [
    (["C:/cf/cloudflared.exe", "tunnel", "run", "miniapp"], True),
    (["cloudflared", "tunnel", "--config", "config.yml", "run", "miniapp"], True),
    (["cloudflared", "tunnel", "run", "--token", "secret"], False),
    (["cloudflared", "tunnel", "--url", "http://127.0.0.1:8089"], False),  # quick tunnel
    (["cloudflared", "tunnel", "run", "another-tunnel"], False),
    (["cloudflared", "service", "install"], False),
])
def test_named_tunnel_command_line(cmdline, expected):
    assert watchdog.is_named_tunnel_cmdline(cmdline, "miniapp") is expected


def test_tunnel_supervisor_only_counts_the_named_tunnel(monkeypatch):
    class FakeProc:
        def __init__(self, pid, name, cmdline):
            self.pid = pid
            self.info = {"pid": pid, "name": name, "cmdline": cmdline}

    procs = [FakeProc(11, "cloudflared.exe", ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8089"]),
             FakeProc(12, "python.exe", ["python", "run.py", "tunnel", "run", "miniapp"])]
    monkeypatch.setattr(watchdog.psutil, "process_iter", lambda attrs=None: iter(procs))
    supervisor = watchdog.CloudflaredTunnelSupervisor("miniapp")
    assert supervisor.is_running() is False
    procs.append(FakeProc(13, "cloudflared.exe", ["cloudflared", "tunnel", "run", "miniapp"]))
    assert supervisor.is_running() is True
    assert supervisor.find_tunnel_process().pid == 13


def test_tunnel_name_comes_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "BASE_DIR", str(tmp_path))
    monkeypatch.delenv("CLOUDFLARED_TUNNEL_NAME", raising=False)
    assert watchdog.default_tunnel_name() == "miniapp"
    (tmp_path / ".env").write_text("CLOUDFLARED_TUNNEL_NAME=cloner-edge\n", encoding="utf-8")
    assert watchdog.default_tunnel_name() == "cloner-edge"
    monkeypatch.setenv("CLOUDFLARED_TUNNEL_NAME", "from-env")
    assert watchdog.default_tunnel_name() == "from-env"


def test_importing_the_watchdog_has_no_logging_side_effects():
    assert not any(isinstance(h, watchdog.RotatingFileHandler) for h in watchdog.logger.handlers) \
        or watchdog._logging_configured


# ---------------------------------------------------------------------------------------------------
# Autostart: exactly one non-elevated mechanism, symmetric and idempotent (H7)
# ---------------------------------------------------------------------------------------------------

import scripts.setup_autostart as setup_autostart  # noqa: E402


class FakeAutostartBackend:
    def __init__(self, tmp_path, run_values=(), tasks=(), startup_files=(), denied_tasks=()):
        self.startup = tmp_path / "Startup"
        self.startup.mkdir()
        self.run_values = set(run_values)
        self.tasks = set(tasks)
        self.denied = set(denied_tasks)
        for name in startup_files:
            (self.startup / name).write_text("legacy", encoding="utf-8")
        self.shortcuts = {}
        self.started = []

    def startup_dir(self):
        return str(self.startup)

    def file_exists(self, path):
        return os.path.exists(path)

    def remove_file(self, path):
        os.remove(path)

    def create_shortcut(self, path, target, arguments, working_dir, description):
        Path(path).write_text("lnk", encoding="utf-8")
        self.shortcuts[path] = (target, arguments, working_dir)

    def run_value_exists(self, name):
        return name in self.run_values

    def delete_run_value(self, name):
        self.run_values.discard(name)

    def task_exists(self, name):
        return name in self.tasks

    def delete_task(self, name):
        if name in self.denied:
            return "ERROR: Access is denied."
        self.tasks.discard(name)
        return None

    def start_watchdog(self, target, arguments, working_dir):
        self.started.append((target, arguments, working_dir))
        return 4242


def _all_legacy(tmp_path, **kwargs):
    return FakeAutostartBackend(
        tmp_path,
        run_values=setup_autostart.LEGACY_RUN_VALUES,
        tasks=setup_autostart.LEGACY_TASK_NAMES,
        startup_files=setup_autostart.LEGACY_STARTUP_FILES,
        **kwargs,
    )


def test_autostart_install_leaves_exactly_one_registration(tmp_path):
    backend = _all_legacy(tmp_path)
    assert setup_autostart.install(backend, report=lambda msg: None) == 0
    assert backend.run_values == set() and backend.tasks == set()
    assert sorted(os.listdir(backend.startup)) == [setup_autostart.SHORTCUT_NAME]
    (lnk, (target, arguments, working_dir)), = backend.shortcuts.items()
    assert target.lower().endswith(("pythonw.exe", "python.exe", "python3", "python"))
    assert setup_autostart.WATCHDOG_SCRIPT in arguments
    assert working_dir == setup_autostart.PROJECT_ROOT
    assert backend.started == []
    # idempotent
    assert setup_autostart.install(backend, report=lambda msg: None) == 0
    assert sorted(os.listdir(backend.startup)) == [setup_autostart.SHORTCUT_NAME]


def test_autostart_uninstall_is_symmetric_and_idempotent(tmp_path):
    backend = _all_legacy(tmp_path)
    assert setup_autostart.install(backend, report=lambda msg: None) == 0
    backend.run_values.update(setup_autostart.LEGACY_RUN_VALUES)  # re-added by an old version
    assert setup_autostart.uninstall(backend, report=lambda msg: None) == 0
    assert os.listdir(backend.startup) == []
    assert backend.run_values == set() and backend.tasks == set()
    assert setup_autostart.uninstall(backend, report=lambda msg: None) == 0


def test_autostart_reports_tasks_needing_elevation(tmp_path):
    messages = []
    backend = _all_legacy(tmp_path, denied_tasks={"ChannelCloner_24_7_Watchdog"})
    assert setup_autostart.install(backend, report=messages.append) == 2
    assert any("elevated prompt" in m and "ChannelCloner_24_7_Watchdog" in m for m in messages)
    assert os.path.exists(os.path.join(backend.startup, setup_autostart.SHORTCUT_NAME))


def test_autostart_install_can_start_the_watchdog(tmp_path):
    backend = FakeAutostartBackend(tmp_path)
    assert setup_autostart.install(backend, report=lambda msg: None, start=True) == 0
    assert backend.started and backend.started[0][1] == [setup_autostart.WATCHDOG_SCRIPT]


# ---------------------------------------------------------------------------------------------------
# setup_wizard: keeps values, atomic write, generated key never printed (L6)
# ---------------------------------------------------------------------------------------------------

import setup_wizard  # noqa: E402


def test_env_merge_keeps_comments_order_and_other_keys():
    lines = ["# header\n", "BOT_TOKEN=old\n", "\n", "# DB_PATH=database/cloner.db\n", "ADMIN_IDS=1,2\n",
             "BOT_TOKEN=duplicate\n", "CUSTOM=keep"]
    merged = setup_wizard.merge_env_lines(lines, {"BOT_TOKEN": "new", "WEBAPP_URL": "https://app.example.com"})
    assert merged[:5] == ["# header\n", "BOT_TOKEN=new\n", "\n", "# DB_PATH=database/cloner.db\n", "ADMIN_IDS=1,2\n"]
    assert "BOT_TOKEN=duplicate\n" not in merged
    assert "CUSTOM=keep\n" in merged
    assert merged[-1] == "WEBAPP_URL=https://app.example.com\n"
    parsed = setup_wizard.parse_env_lines(merged)
    assert parsed["BOT_TOKEN"] == "new" and parsed["ADMIN_IDS"] == "1,2"


def test_env_parse_handles_quotes_export_and_comments():
    parsed = setup_wizard.parse_env_lines(['export A="x y"\n', "# B=1\n", "C='z'\n", "D=\n"])
    assert parsed == {"A": "x y", "C": "z", "D": ""}
    assert setup_wizard.usable("YOUR_BOT_TOKEN_HERE") is None
    assert setup_wizard.usable("  ") is None


def test_env_write_is_atomic(tmp_path):
    target = tmp_path / ".env"
    target.write_text("OLD=1\n", encoding="utf-8")
    setup_wizard.write_env_atomic(target, ["NEW=2\n"])
    assert target.read_text(encoding="utf-8") == "NEW=2\n"
    assert os.listdir(tmp_path) == [".env"]


def test_generated_encryption_key_format():
    key = setup_wizard.generate_encryption_key()
    assert len(base64.urlsafe_b64decode(key)) == 32


def _answer_with(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0) if queue else "")
    monkeypatch.setattr(setup_wizard.getpass, "getpass", lambda prompt="": queue.pop(0) if queue else "")


async def test_wizard_keeps_existing_values_when_prompts_are_skipped(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    original = (
        "# my settings\n"
        "BOT_TOKEN=1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n"
        "TELEGRAM_API_ID=123456\n"
        "TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n"
        "ADMIN_IDS=111,222\n"
        "TELETHON_SESSION=1Aexisting-session\n"
        "ENCRYPTION_KEY=existing-master-key\n"
    )
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "ENV_PATH", env)
    monkeypatch.setattr(setup_wizard, "create_telethon_session",
                        MagicMock(side_effect=AssertionError("no Telegram login expected")))
    _answer_with(monkeypatch, [])  # Enter everywhere
    await setup_wizard.main()
    assert setup_wizard.parse_env_lines(env.read_text(encoding="utf-8").splitlines(True)) == \
        setup_wizard.parse_env_lines(original.splitlines(True))
    assert env.read_text(encoding="utf-8").startswith("# my settings\n")
    out = capsys.readouterr().out
    assert "existing-master-key" not in out and "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" not in out


async def test_wizard_generates_missing_encryption_key_without_printing_it(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\nTELEGRAM_API_ID=123456\n"
                   "TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "ENV_PATH", env)
    # No stored session: the login step is offered and skipped (no Telegram connection in tests)
    monkeypatch.setattr(setup_wizard, "create_telethon_session", _async_return(None))
    _answer_with(monkeypatch, [])
    await setup_wizard.main()
    values = setup_wizard.parse_env_lines(env.read_text(encoding="utf-8").splitlines(True))
    assert len(base64.urlsafe_b64decode(values["ENCRYPTION_KEY"])) == 32
    assert values["ENCRYPTION_KEY"] not in capsys.readouterr().out
    assert "ADMIN_IDS" not in values  # skipped optional prompt: nothing invented, nothing wiped


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


# ---------------------------------------------------------------------------------------------------
# deploy_remote: git-based, host keys verified, no secrets shipped (H3 / M6 / L8)
# ---------------------------------------------------------------------------------------------------

import scripts.deploy_remote as deploy_remote  # noqa: E402


def _deploy_args(*extra):
    return ["--host", "203.0.113.10", "--user", "ubuntu", "--ref", "v1.4.0", *extra]


def test_deploy_remote_dry_run_builds_safe_script(monkeypatch, capsys):
    monkeypatch.setattr(deploy_remote.subprocess, "run", MagicMock(side_effect=AssertionError("no ssh in dry-run")))
    assert deploy_remote.main(_deploy_args("--repo", "https://github.com/example/repo.git", "--dry-run")) == 0
    script = capsys.readouterr().out
    assert "set -euo pipefail" in script
    assert "git -C \"$APP_DIR\" checkout --detach \"$REF\"" in script
    assert "$DOCKER compose --profile docker-bot up -d --build telegram-cloner" in script
    assert "/ready" in script
    assert "scp" not in script and "vault_key" not in script and "cloner.db" not in script
    assert "exit 4" in script  # refuses to run without a server-side .env


@pytest.mark.parametrize("bad", [
    ["--ref", "v1.0; rm -rf /"],
    ["--ref", "../../etc"],
    ["--host", "evil host"],
    ["--domain", "example.com"],  # without --email
])
def test_deploy_remote_rejects_unsafe_arguments(bad, monkeypatch):
    monkeypatch.setattr(deploy_remote.subprocess, "run", MagicMock(side_effect=AssertionError("must not connect")))
    args = _deploy_args()
    for flag, value in zip(bad[::2], bad[1::2]):
        if flag in args:
            args[args.index(flag) + 1] = value
        else:
            args += [flag, value]
    assert deploy_remote.main(args + ["--dry-run"]) == 2


def test_deploy_remote_verifies_host_keys():
    ns = MagicMock(user="ubuntu", host="203.0.113.10", key=None, port_ssh=None, accept_new_host_key=False)
    cmd = deploy_remote.ssh_base_command(ns)
    assert "StrictHostKeyChecking=yes" in cmd
    assert not any("/dev/null" in part or part == "StrictHostKeyChecking=no" for part in cmd)
    ns.accept_new_host_key = True
    assert "StrictHostKeyChecking=accept-new" in deploy_remote.ssh_base_command(ns)


def test_deploy_remote_propagates_failures(monkeypatch):
    monkeypatch.setattr(deploy_remote.subprocess, "run",
                        MagicMock(return_value=subprocess.CompletedProcess(["ssh"], 5)))
    assert deploy_remote.main(_deploy_args()) == 5


# ---------------------------------------------------------------------------------------------------
# GitHub webhook deployer (L9)
# ---------------------------------------------------------------------------------------------------

from deploy.webhook import deploy_webhook as webhook  # noqa: E402

SECRET = "test-webhook-secret"


def _signed(body: bytes, secret: str = SECRET) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_and_tag_filter():
    body = b'{"ref": "refs/tags/v1.2.3"}'
    assert webhook.verify_signature(SECRET, body, _signed(body))
    assert not webhook.verify_signature(SECRET, body, _signed(body, "other"))
    assert not webhook.verify_signature("", body, _signed(body, ""))
    assert webhook.deploy_tag_from_push({"ref": "refs/tags/v1.2.3"}) == "v1.2.3"
    assert webhook.deploy_tag_from_push({"ref": "refs/heads/main"}) is None
    assert webhook.deploy_tag_from_push({"ref": "refs/tags/v1.2.3", "deleted": True}) is None
    assert webhook.deploy_tag_from_push({"ref": "refs/tags/v1.2.3;reboot"}) is None


def test_webhook_request_evaluation():
    cache = webhook.DeliveryCache()
    tag_body = json.dumps({"ref": "refs/tags/v2.0.0"}).encode()
    headers = {"X-Hub-Signature-256": _signed(tag_body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "d-1"}
    assert webhook.evaluate_request(headers, tag_body, "", cache)[0] == 503
    assert webhook.evaluate_request({**headers, "X-Hub-Signature-256": "sha256=00"}, tag_body, SECRET, cache)[0] == 403
    status, payload, tag = webhook.evaluate_request(headers, tag_body, SECRET, cache)
    assert (status, tag) == (202, "v2.0.0")
    cache.add("d-1")
    assert webhook.evaluate_request(headers, tag_body, SECRET, cache)[2] is None  # redelivery ignored
    branch_body = json.dumps({"ref": "refs/heads/main"}).encode()
    branch_headers = {"X-Hub-Signature-256": _signed(branch_body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "d-2"}
    assert webhook.evaluate_request(branch_headers, branch_body, SECRET, cache)[2] is None
    ping_headers = {"X-Hub-Signature-256": _signed(b"{}"), "X-GitHub-Event": "ping"}
    assert webhook.evaluate_request(ping_headers, b"{}", SECRET, cache)[0] == 200


def test_webhook_rejects_oversized_body_before_reading(monkeypatch):
    import http.client
    monkeypatch.setattr(webhook, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhook, "deploy", MagicMock(side_effect=AssertionError("no deploy")))
    server = webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.putrequest("POST", "/")
        conn.putheader("Content-Length", str(webhook.MAX_BODY_BYTES + 1))
        conn.putheader("X-GitHub-Event", "push")
        conn.endheaders()
        assert conn.getresponse().status == 413
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
    assert webhook.WEBHOOK_BIND == os.getenv("WEBHOOK_BIND", "127.0.0.1")


# ---------------------------------------------------------------------------------------------------
# Anti-reclaim helper (M13)
# ---------------------------------------------------------------------------------------------------

from deploy import anti_reclaim  # noqa: E402


def test_anti_reclaim_uses_worker_processes(monkeypatch):
    created = []

    class FakeProcess:
        def __init__(self, target, args, name, daemon):
            created.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(anti_reclaim.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(anti_reclaim.multiprocessing, "Event", threading.Event)
    daemon = anti_reclaim.OracleAntiReclaimDaemon(target_ram_pct=0, target_cpu_pct=0.23)
    daemon.start_cpu_workers()
    assert len(created) == daemon.cpu_cores
    assert all(target is anti_reclaim.cpu_burn_worker and args[0] == 0.23 for target, args in created)

    idle = anti_reclaim.OracleAntiReclaimDaemon(target_ram_pct=0, target_cpu_pct=0)
    created.clear()
    idle.start_cpu_workers()
    assert created == [] and idle.allocated_ram_mb == 0


def test_anti_reclaim_status_is_written_atomically(tmp_path):
    target = tmp_path / "status.json"
    daemon = anti_reclaim.OracleAntiReclaimDaemon(target_ram_pct=0, target_cpu_pct=0)
    anti_reclaim.write_status_atomic(str(target), daemon.status_payload())
    data = json.loads(target.read_text(encoding="utf-8"))
    assert {"ram_percent", "target_cpu_percent", "network_heartbeats"} <= set(data)  # read by the admin bot
    assert os.listdir(tmp_path) == ["status.json"]


def test_anti_reclaim_touches_every_page():
    daemon = anti_reclaim.OracleAntiReclaimDaemon(target_ram_pct=0, target_cpu_pct=0)
    daemon.allocated_buffer = bytearray(3 * anti_reclaim.PAGE_SIZE + 10)
    daemon.touch_memory()
    assert all(daemon.allocated_buffer[i] == 1 for i in range(0, len(daemon.allocated_buffer), anti_reclaim.PAGE_SIZE))


# ---------------------------------------------------------------------------------------------------
# repair_db: never while the bot runs, backup first, no silent row loss (M10)
# ---------------------------------------------------------------------------------------------------

import scripts.repair_db as repair_db  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO users (name) VALUES (?)", [(f"user{i}",) for i in range(50)])
    conn.commit()
    conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def test_repair_refuses_while_bot_is_running(tmp_path):
    db = tmp_path / "cloner.db"
    _make_db(db)
    before = db.read_bytes()
    code = repair_db.repair(str(db), rebuild=True, running_check=lambda: "the bot answers", report=lambda m: None)
    assert code == 3
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))


def test_repair_healthy_database_is_left_alone(tmp_path):
    db = tmp_path / "cloner.db"
    _make_db(db)
    assert repair_db.repair(str(db), running_check=lambda: None, report=lambda m: None) == 0
    assert not list(tmp_path.glob("*.bak"))


def test_repair_rebuild_backs_up_and_keeps_every_row(tmp_path):
    db = tmp_path / "cloner.db"
    _make_db(db)
    assert repair_db.repair(str(db), rebuild=True, running_check=lambda: None, report=lambda m: None) == 0
    backups = list(tmp_path.glob("cloner.db.pre-repair-*.bak"))
    assert len(backups) == 1 and _row_count(backups[0]) == 50
    assert _row_count(db) == 50


def test_repair_does_not_replace_when_rows_would_be_lost(tmp_path, monkeypatch):
    db = tmp_path / "cloner.db"
    _make_db(db)

    def lossy_rebuild(src, dst):
        conn = sqlite3.connect(dst)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('only one')")
        conn.commit()
        conn.close()
        return 2, ["malformed row skipped"]

    monkeypatch.setattr(repair_db, "rebuild_database", lossy_rebuild)
    assert repair_db.repair(str(db), rebuild=True, running_check=lambda: None, report=lambda m: None) == 2
    assert _row_count(db) == 50


# ---------------------------------------------------------------------------------------------------
# Deployment configuration (C1, C2, H1, H5, H9, M7, M8, M9, L2, L4)
# ---------------------------------------------------------------------------------------------------

yaml = pytest.importorskip("yaml")


def _load_yaml(name: str):
    return yaml.safe_load((PROJECT_ROOT / name).read_text(encoding="utf-8"))


def test_compose_docker_bot_uses_named_volume_and_is_optional():
    compose = _load_yaml("docker-compose.yml")
    services = compose["services"]
    assert not {"mysql", "miniapp_php"} & set(services)  # legacy PHP stack retired
    bot = services["telegram-cloner"]
    assert bot["profiles"] == ["docker-bot"]
    volumes = bot.get("volumes", [])
    assert not any(str(v).startswith(("./database", "./data", "./bot", "./services", "./run.py")) for v in volumes)
    assert "cloner_bot_data:/app/data" in volumes
    assert "cloner_bot_data" in compose["volumes"]
    env = bot["environment"]
    assert env["DB_PATH"].startswith("/app/data/") and env["VAULT_KEY_PATH"].startswith("/app/data/")
    assert all(str(p).startswith("127.0.0.1:") for p in bot.get("ports", []))
    assert "WEBAPP_URL" not in env  # comes from .env, no hardcoded domain
    tunnel = services["cloudflared"]
    assert not tunnel["image"].endswith(":latest") and ":" in tunnel["image"]
    assert any(str(v).endswith(":ro") and ".cloudflared" in str(v) for v in tunnel["volumes"])


def test_paas_manifests_are_single_instance_with_persistent_storage():
    render = _load_yaml("render.yaml")["services"][0]
    assert render["autoDeploy"] is False
    assert render["plan"] != "free"
    assert render["disk"]["mountPath"] == "/app/data"
    assert render["healthCheckPath"] == "/ready"
    assert render.get("numInstances", 1) == 1
    keys = {item["key"] for item in render["envVars"]}
    assert {"ENCRYPTION_KEY", "DB_PATH", "VAULT_KEY_PATH"} <= keys

    koyeb = _load_yaml("koyeb.yaml")
    assert koyeb["scalings"][0]["max"] == 1
    assert koyeb["instance_types"][0]["type"] != "free"
    assert koyeb["volumes"][0]["path"] == "/app/data"
    assert koyeb["health_checks"][0]["path"] == "/ready"
    assert "ENCRYPTION_KEY" in {item["key"] for item in koyeb["env"]}


def test_procfile_defines_exactly_one_process():
    lines = [l for l in (PROJECT_ROOT / "Procfile").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == ["web: python run.py"]


def test_dockerfile_and_dockerignore_keep_secrets_out():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "chmod -R 777" not in dockerfile
    assert "COPY --chown=appuser:appuser database/*.py ./database/" in dockerfile
    assert "COPY --chown=appuser:appuser database/ " not in dockerfile
    assert "ARG INSTALL_PLAYWRIGHT=false" in dockerfile
    assert "FROM node:20-alpine AS webapp" in dockerfile and "webapp/dist" in dockerfile
    assert "/health" in dockerfile and "rm -rf /var/lib/apt/lists/*" in dockerfile
    ignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("**/.env", "**/.vault_key", "**/*.db", "**/*.db-wal", "data", "logs", "backups",
                    "**/node_modules", ".cloudflared", "**/*.exe", "**/*.session"):
        assert pattern in ignore, pattern
    assert "!.env.example" not in ignore


def test_requirements_are_bounded_and_trimmed():
    names = []
    for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec = line.split(";", 1)[0].strip()
        name = re.split(r"[<>=!~\[ ]", spec, 1)[0].lower()
        names.append(name)
        assert "<" in spec, f"{name} has no upper bound"
        if name == "cryptography":
            lower = re.search(r">=\s*([\d.]+)", spec).group(1)
            assert tuple(int(p) for p in lower.split(".")) >= (43, 0, 1)
    assert len(names) == len(set(names)), "duplicate requirement"
    assert not {"aiomysql", "pymysql", "edge-tts", "requests"} & set(names)


def test_env_example_documents_every_setting_without_values():
    from config.settings import Settings
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "YOUR_" not in text
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))
    for name, field in Settings.model_fields.items():
        assert (field.alias or name) in documented, f"{field.alias or name} missing from .env.example"
    for key in ("BOT_TOKEN", "ADMIN_BOT_TOKEN", "TELEGRAM_API_HASH", "TELEGRAM_API_ID", "ENCRYPTION_KEY",
                "TELETHON_SESSION", "ADMIN_IDS", "PRIMARY_SUPER_ADMIN_ID"):
        assert re.search(rf"^{key}=$", text, flags=re.MULTILINE), f"{key} must be present with an empty value"


def test_ci_runs_pytest_lint_webapp_and_import_smoke():
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "unittest discover" not in ci
    assert "python -m pytest" in ci and "--timeout=120" in ci
    assert "ruff check --select E9,F63,F7,F82" in ci
    assert "npx tsc --noEmit" in ci and "npx vite build" in ci and "node-version: \"20\"" in ci
    assert 'python -c "import run"' in ci
    assert "requirements-dev.txt" in ci


def test_vps_bootstrap_keeps_internal_ports_closed():
    script = (PROJECT_ROOT / "deploy" / "oracle_master_setup.sh").read_text(encoding="utf-8")
    assert "chmod 666" not in script
    assert "8080/tcp" not in script and "9000/tcp" not in script
    assert "add-port=8080" not in script and "add-port=9000" not in script
    assert "rm -f /etc/nginx/conf.d/channelcloner.conf" in script  # never both conf.d and sites-enabled
    nginx = (PROJECT_ROOT / "deploy" / "nginx" / "channelcloner.conf").read_text(encoding="utf-8")
    assert "ssl_certificate" not in nginx  # certbot adds HTTPS after issuing the certificate
    assert "127.0.0.1:__BOT_PORT__" in nginx and "8087" not in nginx
    unit = (PROJECT_ROOT / "deploy" / "systemd" / "telegram-cloner.service").read_text(encoding="utf-8")
    reload_line = next(l for l in unit.splitlines() if l.startswith("ExecReload="))
    assert "&&" not in reload_line or reload_line.startswith("ExecReload=/bin/sh -c")


def test_gitignore_and_gitattributes():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("**/.vault_key", "backups/", "data/", "logs/", "*.log", "webapp/vite.config.ts.timestamp-*",
                    ".cloudflared/*", "!.cloudflared/config.yml.example", "docker-compose.override.yml"):
        assert pattern in ignore, pattern
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes and "*.ps1 text eol=crlf" in attributes
    assert (PROJECT_ROOT / ".cloudflared" / "config.yml.example").exists()
