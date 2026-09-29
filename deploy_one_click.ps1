# ==============================================================================
# Telegram Channel Cloner - deploy a release tag to the VPS (Windows entry point)
#
# Thin wrapper around scripts\deploy_remote.py, the single deployment implementation:
#   * the server holds a git clone; a release tag is checked out there (git fetch + checkout)
#   * NOTHING secret is uploaded: .env, the database, the vault key and Cloudflare credentials
#     stay where they are (create .env on the server yourself)
#   * SSH host keys are verified (known_hosts); -AcceptNewHostKey trusts a new server once
#   * every step is checked; a failure stops the deployment with a non-zero exit code
#
# First deployment:
#   .\deploy_one_click.ps1 -VpsIp 203.0.113.10 -User ubuntu -KeyPath $HOME\.ssh\oracle_arm_key `
#       -Ref v1.4.0 -Repo https://github.com/<owner>/<repo>.git -Bootstrap -AcceptNewHostKey `
#       -Domain app.example.com -Email me@example.com
# Update:
#   .\deploy_one_click.ps1 -VpsIp 203.0.113.10 -User ubuntu -KeyPath $HOME\.ssh\oracle_arm_key -Ref v1.4.1
#
# SSH key (once, PowerShell 7 and 5.1):  ssh-keygen -t ed25519 -f "$HOME\.ssh\oracle_arm_key"
# ==============================================================================
param (
    [Parameter(Mandatory = $true)] [string] $VpsIp,
    [Parameter(Mandatory = $true)] [string] $Ref,
    [string] $User = "ubuntu",
    [string] $KeyPath = "$HOME\.ssh\oracle_arm_key",
    [string] $Repo,
    [string] $AppDir = "~/channelcloner",
    [string] $Domain,
    [string] $Email,
    [switch] $Bootstrap,
    [switch] $WithAntiReclaim,
    [switch] $AcceptNewHostKey
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $KeyPath)) {
    Write-Host "[deploy] SSH kalit topilmadi: $KeyPath" -ForegroundColor Red
    Write-Host "         Yaratish: ssh-keygen -t ed25519 -f `"$KeyPath`" va .pub faylini serverga qo'shing." -ForegroundColor Yellow
    exit 1
}

$python = (Get-Command python -ErrorAction Stop).Source
$deployArgs = @("--host", $VpsIp, "--user", $User, "--key", $KeyPath, "--ref", $Ref, "--app-dir", $AppDir)
if ($Repo) { $deployArgs += @("--repo", $Repo) }
if ($Bootstrap) { $deployArgs += "--bootstrap" }
if ($Domain) { $deployArgs += @("--domain", $Domain) }
if ($Email) { $deployArgs += @("--email", $Email) }
if ($WithAntiReclaim) { $deployArgs += "--with-anti-reclaim" }
if ($AcceptNewHostKey) { $deployArgs += "--accept-new-host-key" }

& $python (Join-Path $PSScriptRoot "scripts\deploy_remote.py") @deployArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "[deploy] XATO: joylashtirish muvaffaqiyatsiz tugadi (exit code $code)." -ForegroundColor Red
} else {
    Write-Host "[deploy] Joylashtirish muvaffaqiyatli yakunlandi." -ForegroundColor Green
}
exit $code
