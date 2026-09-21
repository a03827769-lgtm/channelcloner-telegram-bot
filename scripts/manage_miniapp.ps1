# ============================================================
# ChannelCloner Pro — Production Docker Management Script (PowerShell)
# PHP 8.3-FPM + MySQL 8.0 + Nginx + Cloudflare + Telegram Cloner
# ============================================================

[CmdletBinding()]
param (
    [Parameter(Position=0, Mandatory=$false)]
    [string]$Action = "status",

    [Parameter(Position=1, Mandatory=$false)]
    [string]$Stack = "all"
)

if ($args.Length -gt 0 -and $Action -eq "status") {
    $Action = $args[0]
}

$ComposeFile = if ($Stack.ToLower() -eq "miniapp") { "docker-compose.miniapp.yml" } else { "docker-compose.yml" }

switch ($Action.ToLower()) {
    "start" {
        Write-Host "🚀 Starting ChannelCloner Production Stack ($ComposeFile)..." -ForegroundColor Cyan
        docker compose -f $ComposeFile up -d
        Start-Sleep -Seconds 3
        docker compose -f $ComposeFile ps
    }
    "stop" {
        Write-Host "🛑 Stopping ChannelCloner Stack ($ComposeFile)..." -ForegroundColor Yellow
        docker compose -f $ComposeFile down
    }
    "restart" {
        Write-Host "🔄 Restarting ChannelCloner Stack ($ComposeFile)..." -ForegroundColor Cyan
        docker compose -f $ComposeFile restart
        Start-Sleep -Seconds 2
        docker compose -f $ComposeFile ps
    }
    "logs" {
        Write-Host "📋 Viewing ChannelCloner Live Container Logs..." -ForegroundColor Green
        docker compose -f $ComposeFile logs -f --tail 50
    }
    "status" {
        Write-Host "📊 ChannelCloner Production Containers Status:" -ForegroundColor Green
        docker compose -f $ComposeFile ps
    }
    "sync" {
        Write-Host "🔄 Synchronizing active Cloudflare URL to MySQL & Bot..." -ForegroundColor Cyan
        python scripts/sync_active_url.py
    }
    default {
        Write-Host "Usage: .\scripts\manage_miniapp.ps1 [start|stop|restart|logs|status|sync] [all|miniapp]" -ForegroundColor Yellow
    }
}
