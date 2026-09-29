# ==============================================================================
# Telegram Channel Cloner - interactive VPS update (asks for the parameters)
# Thin wrapper around scripts\deploy_remote.py (git-based, no secrets shipped, host keys verified).
# ==============================================================================
param (
    [string] $VpsIp,
    [string] $KeyPath,
    [string] $User,
    [string] $Ref
)
$ErrorActionPreference = "Stop"

if (-not $VpsIp) { $VpsIp = Read-Host "Server IP address" }
if (-not $KeyPath) { $KeyPath = Read-Host "Path to the SSH private key" }
if (-not $User) { $User = Read-Host "SSH user (ubuntu / opc)" }
if (-not $Ref) { $Ref = Read-Host "Release tag to deploy (e.g. v1.4.0)" }

$python = (Get-Command python -ErrorAction Stop).Source
& $python (Join-Path $PSScriptRoot "deploy_remote.py") --host $VpsIp --user $User --key $KeyPath --ref $Ref
exit $LASTEXITCODE
