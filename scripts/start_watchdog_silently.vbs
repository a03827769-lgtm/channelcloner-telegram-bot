' ==============================================================================
' Telegram Channel Cloner - manual silent launcher for the 24/7 watchdog
' Starts windows_keepalive_watchdog.py with pythonw.exe (first one on PATH) and
' no console window. Autostart at logon is registered by:
'     python scripts\setup_autostart.py install
' (a second watchdog exits immediately: single-instance mutex)
' ==============================================================================
Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
BaseDir = FSO.GetParentFolderName(ScriptDir)
WatchdogScript = ScriptDir & "\windows_keepalive_watchdog.py"

' Set working directory to project root
WshShell.CurrentDirectory = BaseDir

' Run with window style 0 (hidden) and do not wait for return
Cmd = "pythonw.exe """ & WatchdogScript & """"
WshShell.Run Cmd, 0, False

Set WshShell = Nothing
Set FSO = Nothing
