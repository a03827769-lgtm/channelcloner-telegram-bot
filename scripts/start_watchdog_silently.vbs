' ==============================================================================
' Telegram Channel Cloner - Silent Background Watchdog Launcher
' Launches windows_keepalive_watchdog.py with no visible console window (0)
' ==============================================================================
Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
BaseDir = FSO.GetParentFolderName(ScriptDir)
WatchdogScript = ScriptDir & "\windows_keepalive_watchdog.py"

' Set working directory to project root
WshShell.CurrentDirectory = BaseDir

' Locate pythonw.exe
PythonwExe = "C:\Users\victus\AppData\Local\Programs\Python\Python311\pythonw.exe"
If Not FSO.FileExists(PythonwExe) Then
    PythonwExe = "pythonw.exe"
End If

' Run with window style 0 (completely hidden) and do not wait for return
Cmd = """" & PythonwExe & """ """ & WatchdogScript & """"
WshShell.Run Cmd, 0, False

Set WshShell = Nothing
Set FSO = Nothing
