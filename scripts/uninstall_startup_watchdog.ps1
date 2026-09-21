# ==============================================================================
# Telegram Channel Cloner - Remove 24/7 Watchdog from Windows Startup
# ==============================================================================

$startupFolder = [System.Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder "ChannelCloner_24_7_Watchdog.lnk"

if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "[OK] Startup shortcut removed from: $shortcutPath" -ForegroundColor Green
} else {
    Write-Host "Startup shortcut was not present." -ForegroundColor Yellow
}
