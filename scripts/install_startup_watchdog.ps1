# ==============================================================================
# Telegram Channel Cloner - register the 24/7 watchdog autostart (Windows logon)
# Thin wrapper: the single implementation is scripts\setup_autostart.py
# (Startup-folder shortcut, non-elevated; removes legacy Run-key / scheduled-task entries).
#   .\scripts\install_startup_watchdog.ps1           register
#   .\scripts\install_startup_watchdog.ps1 --start   register and start the watchdog now
# ==============================================================================
$ErrorActionPreference = "Stop"
$python = (Get-Command python -ErrorAction Stop).Source
& $python (Join-Path $PSScriptRoot "setup_autostart.py") install @args
exit $LASTEXITCODE
