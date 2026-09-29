# ==============================================================================
# Telegram Channel Cloner - health probe (read-only)
#
# Superseded by scripts\windows_keepalive_watchdog.py, which supervises the bot.
# This script only REPORTS state. It never starts or restarts the Docker container:
# the bot must run as exactly one instance (host runtime OR docker-bot profile), and
# starting the container next to the host bot causes TelegramConflictError and
# duplicate posts.
# ==============================================================================
$ErrorActionPreference = "Stop"
$python = (Get-Command python -ErrorAction Stop).Source
& $python (Join-Path $PSScriptRoot "windows_keepalive_watchdog.py") --status
exit $LASTEXITCODE
