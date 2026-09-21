import winreg
import os
import sys
import subprocess
import logging
logger = logging.getLogger(__name__)

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

def setup_registry_autostart():
    pythonw_path = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "windows_keepalive_watchdog.py"))
    
    cmd = f'"{pythonw_path}" "{script_path}"'
    
    # 1. Registry HKCU Run
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE
    )
    winreg.SetValueEx(key, "TelegramChannelClonerWatchdog", 0, winreg.REG_SZ, cmd)
    winreg.CloseKey(key)
    print(f"[SUCCESS] Registered in Registry Run -> {cmd}")

    # 2. Windows Scheduled Task for high-priority autostart on logon
    task_name = "ChannelCloner_24_7_Watchdog"
    task_cmd = f'"{pythonw_path}" "{script_path}"'
    schtasks_args = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", task_cmd,
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F"
    ]
    try:
        res = subprocess.run(schtasks_args, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            print(f"[SUCCESS] Registered Windows Scheduled Task -> {task_name}")
        else:
            print(f"[INFO] Scheduled Task Notice: {res.stderr.strip() or res.stdout.strip()}")
    except Exception as e:
        print(f"[WARN] Could not create Scheduled Task: {e}")

if __name__ == "__main__":
    setup_registry_autostart()
