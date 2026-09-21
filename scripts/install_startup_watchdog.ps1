# ==============================================================================
# Telegram Channel Cloner - Install 24/7 Watchdog to Windows Startup
# ==============================================================================

$baseDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$vbsPath = Join-Path $baseDir "scripts\start_watchdog_silently.vbs"
$startupFolder = [System.Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder "ChannelCloner_24_7_Watchdog.lnk"

Write-Host "Installing 24/7 Keep-Awake Watchdog to Windows Startup..." -ForegroundColor Cyan

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "wscript.exe"
$shortcut.Arguments = "`"$vbsPath`""
$shortcut.WorkingDirectory = $baseDir
$shortcut.Description = "Telegram Channel Cloner 24/7 Background Keep-Awake & Auto-Recovery Watchdog"
$shortcut.Save()

Write-Host "[SUCCESS] Shortcut installed to: $shortcutPath" -ForegroundColor Green
Write-Host "The Watchdog will now start automatically in background whenever you log into Windows." -ForegroundColor Yellow
