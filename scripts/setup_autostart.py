"""
Registers the 24/7 watchdog to start at Windows logon — exactly ONE, non-elevated mechanism:
a shortcut in the user's Startup folder that runs `pythonw.exe scripts\\windows_keepalive_watchdog.py`.

    python scripts\\setup_autostart.py install       (re)create the shortcut, remove legacy registrations
    python scripts\\setup_autostart.py install --start   ... and start the watchdog now
    python scripts\\setup_autostart.py uninstall     remove the shortcut and every legacy registration
    python scripts\\setup_autostart.py status        show what is registered

Legacy registrations of older versions are removed by both install and uninstall (idempotent):
  * HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run value "TelegramChannelClonerWatchdog"
  * scheduled task "ChannelCloner_24_7_Watchdog" (created with /RL HIGHEST, so deleting it may need an
    elevated prompt: schtasks /Delete /TN ChannelCloner_24_7_Watchdog /F)
  * scheduled task "ChannelCloner_DockerWatchdog" and the Startup item "ChannelCloner_DockerWatchdog.vbs"
    (they started Docker Desktop at logon, which could bring back a second bot container)
"""

import argparse
import ctypes
import os
import subprocess
import sys
from typing import Callable, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
WATCHDOG_SCRIPT = os.path.join(SCRIPTS_DIR, "windows_keepalive_watchdog.py")

SHORTCUT_NAME = "ChannelCloner_24_7_Watchdog.lnk"
SHORTCUT_DESCRIPTION = "Telegram Channel Cloner 24/7 watchdog (keeps the bot and the Mini App tunnel running)"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_RUN_VALUES = ("TelegramChannelClonerWatchdog",)
LEGACY_TASK_NAMES = ("ChannelCloner_24_7_Watchdog", "ChannelCloner_DockerWatchdog")
LEGACY_STARTUP_FILES = ("ChannelCloner_DockerWatchdog.vbs",)

CREATE_NO_WINDOW = 0x08000000


def pythonw_executable() -> str:
    """pythonw.exe of the running interpreter (a venv interpreter keeps its packages)."""
    folder, name = os.path.split(sys.executable)
    if name.lower() == "pythonw.exe":
        return sys.executable
    candidate = os.path.join(folder, "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


class WindowsAutostartBackend:
    """Every side effect on the Windows user profile: Startup folder, HKCU Run key and Task Scheduler."""

    def startup_dir(self) -> str:
        buf = ctypes.create_unicode_buffer(260)
        CSIDL_STARTUP = 0x0007
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf) == 0 and buf.value:
            return buf.value
        return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")

    def file_exists(self, path: str) -> bool:
        return os.path.exists(path)

    def remove_file(self, path: str) -> None:
        os.remove(path)

    def create_shortcut(self, path: str, target: str, arguments: str, working_dir: str, description: str) -> None:
        # Values travel through environment variables, so paths with quotes/spaces need no escaping
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CC_LNK); "
            "$s.TargetPath = $env:CC_TARGET; $s.Arguments = $env:CC_ARGS; "
            "$s.WorkingDirectory = $env:CC_CWD; $s.Description = $env:CC_DESC; $s.WindowStyle = 7; $s.Save()"
        )
        env = dict(os.environ, CC_LNK=path, CC_TARGET=target, CC_ARGS=arguments, CC_CWD=working_dir, CC_DESC=description)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env, check=True, capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW,
        )

    def run_value_exists(self, name: str) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
                winreg.QueryValueEx(key, name)
            return True
        except FileNotFoundError:
            return False

    def delete_run_value(self, name: str) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)

    def task_exists(self, name: str) -> bool:
        result = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, text=True,
                                timeout=30, creationflags=CREATE_NO_WINDOW)
        return result.returncode == 0

    def delete_task(self, name: str) -> Optional[str]:
        """None on success, otherwise the error text (e.g. access denied for an elevated task)."""
        result = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, text=True,
                                timeout=30, creationflags=CREATE_NO_WINDOW)
        if result.returncode == 0:
            return None
        return (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"

    def start_watchdog(self, target: str, arguments: List[str], working_dir: str) -> int:
        DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200
        proc = subprocess.Popen([target] + arguments, cwd=working_dir, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        return proc.pid


def remove_legacy_registrations(backend, report: Callable[[str], None]) -> List[str]:
    """Removes the registrations of older versions. Returns the ones that could not be removed."""
    problems: List[str] = []
    for value in LEGACY_RUN_VALUES:
        try:
            if backend.run_value_exists(value):
                backend.delete_run_value(value)
                report(f"[OK] Removed legacy HKCU Run entry '{value}'")
        except OSError as e:
            problems.append(f"HKCU Run entry '{value}': {e}")
    for task in LEGACY_TASK_NAMES:
        try:
            if backend.task_exists(task):
                error = backend.delete_task(task)
                if error is None:
                    report(f"[OK] Removed legacy scheduled task '{task}'")
                else:
                    problems.append(
                        f"scheduled task '{task}': {error} — run once in an elevated prompt: "
                        f"schtasks /Delete /TN {task} /F"
                    )
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"scheduled task '{task}': {e}")
    startup_dir = backend.startup_dir()
    for name in LEGACY_STARTUP_FILES:
        path = os.path.join(startup_dir, name)
        try:
            if backend.file_exists(path):
                backend.remove_file(path)
                report(f"[OK] Removed legacy Startup item '{name}'")
        except OSError as e:
            problems.append(f"Startup item '{name}': {e}")
    return problems


def shortcut_path(backend) -> str:
    return os.path.join(backend.startup_dir(), SHORTCUT_NAME)


def install(backend, report: Callable[[str], None] = print, start: bool = False) -> int:
    problems = remove_legacy_registrations(backend, report)
    target = pythonw_executable()
    lnk = shortcut_path(backend)
    try:
        backend.create_shortcut(lnk, target, f'"{WATCHDOG_SCRIPT}"', PROJECT_ROOT, SHORTCUT_DESCRIPTION)
    except (OSError, subprocess.SubprocessError) as e:
        report(f"[ERROR] Could not create the Startup shortcut {lnk}: {e}")
        return 1
    report(f"[OK] Autostart shortcut: {lnk}")
    report(f"     -> {target} \"{WATCHDOG_SCRIPT}\"")
    if start:
        pid = backend.start_watchdog(target, [WATCHDOG_SCRIPT], PROJECT_ROOT)
        report(f"[OK] Watchdog started in the background (PID {pid}); an already running one keeps running.")
    for problem in problems:
        report(f"[WARN] Not removed: {problem}")
    return 0 if not problems else 2


def uninstall(backend, report: Callable[[str], None] = print) -> int:
    problems = remove_legacy_registrations(backend, report)
    lnk = shortcut_path(backend)
    try:
        if backend.file_exists(lnk):
            backend.remove_file(lnk)
            report(f"[OK] Removed autostart shortcut {lnk}")
        else:
            report("[OK] Autostart shortcut was not present")
    except OSError as e:
        problems.append(f"shortcut {lnk}: {e}")
    for problem in problems:
        report(f"[WARN] Not removed: {problem}")
    report("The running watchdog (if any) keeps running: python scripts\\windows_keepalive_watchdog.py --stop")
    return 0 if not problems else 2


def status(backend, report: Callable[[str], None] = print) -> int:
    lnk = shortcut_path(backend)
    report(f"Startup shortcut ({SHORTCUT_NAME}): {'present' if backend.file_exists(lnk) else 'absent'}")
    for value in LEGACY_RUN_VALUES:
        report(f"Legacy HKCU Run '{value}': {'PRESENT' if backend.run_value_exists(value) else 'absent'}")
    for task in LEGACY_TASK_NAMES:
        report(f"Legacy scheduled task '{task}': {'PRESENT' if backend.task_exists(task) else 'absent'}")
    startup_dir = backend.startup_dir()
    for name in LEGACY_STARTUP_FILES:
        present = backend.file_exists(os.path.join(startup_dir, name))
        report(f"Legacy Startup item '{name}': {'PRESENT' if present else 'absent'}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Windows logon autostart for the 24/7 watchdog")
    parser.add_argument("command", nargs="?", choices=["install", "uninstall", "status"], default="install")
    parser.add_argument("--start", action="store_true", help="with install: start the watchdog right away")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("Autostart registration is only available on Windows.")
        return 1
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    backend = WindowsAutostartBackend()
    if args.command == "uninstall":
        return uninstall(backend)
    if args.command == "status":
        return status(backend)
    return install(backend, start=args.start)


if __name__ == "__main__":
    sys.exit(main())
