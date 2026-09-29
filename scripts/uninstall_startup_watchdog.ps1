# ==============================================================================
# Telegram Channel Cloner - remove the 24/7 watchdog autostart (and all legacy registrations)
# Thin wrapper: the single implementation is scripts\setup_autostart.py
# The running watchdog keeps running; stop it with:
#   python scripts\windows_keepalive_watchdog.py --stop
# ==============================================================================
$ErrorActionPreference = "Stop"
$python = (Get-Command python -ErrorAction Stop).Source
& $python (Join-Path $PSScriptRoot "setup_autostart.py") uninstall @args
exit $LASTEXITCODE
