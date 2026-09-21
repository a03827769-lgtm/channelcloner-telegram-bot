param (
    [Parameter(Mandatory=$false)]
    [string]$VpsIp,
    
    [Parameter(Mandatory=$false)]
    [string]$KeyPath
)

if (-not $VpsIp) {
    $VpsIp = Read-Host "Enter Oracle VPS Public IP Address"
}

if (-not $KeyPath) {
    $KeyPath = Read-Host "Enter Path to SSH Private Key (.key / .pem)"
}

python scripts\deploy_remote.py $VpsIp $KeyPath
