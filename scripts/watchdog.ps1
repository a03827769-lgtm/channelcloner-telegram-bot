$containerName = "telegram_channel_cloner"
$healthUrl = "http://127.0.0.1:8080/health"

try {
    $res = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3 -ErrorAction Stop
    if ($res.status -eq "healthy" -or $res.status -eq "ok") {
        Write-Host "✅ [WATCHDOG] Container is healthy: Telethon=$($res.telethon_connected), Uptime=$($res.uptime)"
    } else {
        Write-Host "⚠️ [WATCHDOG] Container degraded, restarting..."
        docker restart $containerName
    }
} catch {
    Write-Host "⚠️ [WATCHDOG] Health endpoint unresponsive. Recovering..."
    $running = (docker ps -q -f "name=$containerName")
    if ($running) {
        docker restart $containerName
    } else {
        docker start $containerName
    }
}
