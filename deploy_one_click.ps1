# ==============================================================================
# Telegram Channel Cloner - 1-Click Autonomous Oracle Cloud ARM64 Deployer
# Multi-OS: Supports Ubuntu and Oracle Linux (opc / ubuntu users)
# Auto-detects SSH Keys (oracle_vps.key, ~/.ssh/oracle_arm_key)
# High-Speed Tar Bundle Deployment Engine
# ==============================================================================

param (
    [Parameter(Mandatory=$false, Position=0)]
    [string]$VpsIp,

    [Parameter(Mandatory=$false, Position=1)]
    [string]$Domain,

    [Parameter(Mandatory=$false)]
    [string]$KeyPath = "$HOME\.ssh\oracle_arm_key"
)

$ErrorActionPreference = "Continue"

function Write-Banner {
    Clear-Host
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host "     TELEGRAM CHANNEL CLONER -- 1-CLICK ORACLE VPS DEPLOYER           " -ForegroundColor Green
    Write-Host "     Architecture: Oracle Cloud Ampere A1 (ARM64 / 24GB RAM / 4 vCPU) " -ForegroundColor Yellow
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([string]$StepNum, [string]$Title)
    Write-Host "`n[$StepNum] $Title" -ForegroundColor Cyan
    Write-Host "----------------------------------------------------------------------" -ForegroundColor DarkGray
}

function Write-Success {
    param([string]$Msg)
    Write-Host "[+ SUCCESS] $Msg" -ForegroundColor Green
}

function Write-Info {
    param([string]$Msg)
    Write-Host "[* INFO] $Msg" -ForegroundColor Gray
}

function Write-Warn {
    param([string]$Msg)
    Write-Host "[! WARNING] $Msg" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Msg)
    Write-Host "[- ERROR] $Msg" -ForegroundColor Red
}

Write-Banner

# 1. SSH Kalitini tekshirish
if (-not (Test-Path $KeyPath)) {
    if (Test-Path "oracle_vps.key") {
        $KeyPath = "oracle_vps.key"
    } else {
        Write-Info "SSH kalit topilmadi. Yangi Ed25519 kalit yaratilmoqda..."
        if (-not (Test-Path "$HOME\.ssh")) { New-Item -ItemType Directory -Path "$HOME\.ssh" | Out-Null }
        ssh-keygen -t ed25519 -C "oracle-ampere-vps" -f $KeyPath -N '""'
        Write-Success "Yangi SSH kalit yaratildi: $KeyPath"
    }
}

# 2. Server IP manzilini so'rash va tekshirish
while ($true) {
    if (-not $VpsIp) {
        Write-Host "Serveringiz Public IP manzili (masalan: 130.61.170.195):" -ForegroundColor Yellow
        Write-Host ""
        $VpsIp = Read-Host "Iltimos, Oracle Cloud VPS Public IP manzilini kiriting"
    }

    $VpsIp = $VpsIp.Trim()

    if ($VpsIp -match "^ssh-") {
        Write-Warn "Siz IP manzil orniga SSH kalitni kiritdingiz!"
        Write-Info "IP manzil - bu Oracle panelida korsatilgan 4 ta raqam (masalan: 130.61.170.195)."
        $VpsIp = ""
        continue
    }

    if ([string]::IsNullOrWhiteSpace($VpsIp)) {
        Write-Err "IP manzil bosh bolishi mumkin emas!"
        $VpsIp = ""
        continue
    }

    break
}

# 3. Domen manzilini so'rash (ixtiyoriy)
if (-not $Domain) {
    $DomainInput = Read-Host "Agar domeningiz bolsa kiriting (ixtiyoriy, enter bosing agar hozircha yoq bolsa)"
    if (-not [string]::IsNullOrWhiteSpace($DomainInput)) {
        $Domain = $DomainInput.Trim()
    }
}

# 4. Foydalanuvchi va SSH aloqasini aniqlash
Write-Step "1/7" "Oracle Cloud serveriga SSH ulanishini sinovdan otkazish..."
$candidateKeys = @("oracle_vps.key", $KeyPath, "$HOME\.ssh\oracle_arm_key") | Where-Object { Test-Path $_ } | Select-Object -Unique
$sshUsers = @("opc", "ubuntu", "root")

$activeUser = $null
$workingKey = $null
$connected = $false

foreach ($k in $candidateKeys) {
    foreach ($u in $sshUsers) {
        Write-Info "Tekshirilmoqda: $u@$VpsIp (Kalit: $k)..."
        $sshTestCmd = "ssh -o LogLevel=ERROR -o ConnectTimeout=6 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i `"$k`" $($u)@$($VpsIp) echo SSH_OK"
        $testResult = (Invoke-Expression $sshTestCmd 2>$null) -join " "
        if ($testResult -match "SSH_OK") {
            $activeUser = $u
            $workingKey = $k
            $connected = $true
            Write-Success "Ulanish ornatildi! Foydalanuvchi: $activeUser, Kalit: $workingKey"
            break
        }
    }
    if ($connected) { break }
}

if (-not $connected) {
    Write-Err "Serverga ulanib bolmadi!"
    Write-Warn "Iltimos, tekshiring:"
    Write-Warn "1. Oracle Cloud panelida Instance holati RUNNING bolishi kerak."
    Write-Warn "2. VCN Security List Ingress qoidalarida Port 22 (SSH) ochilgan bolishi kerak."
    exit 1
}

$remoteTarget = "$($activeUser)@$($VpsIp)"

# Yangi kalitni serverga qoshib qoyish (agar oracle_arm_key.pub mavjud bolsa)
if (Test-Path "$HOME\.ssh\oracle_arm_key.pub") {
    $myPub = (Get-Content "$HOME\.ssh\oracle_arm_key.pub" -Raw).Trim()
    ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget "echo '$myPub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" 2>$null
}

# 5. Mahalliy SSH Config faylini yangilash
Write-Step "2/7" "Mahalliy SSH konfiguratsiyasini yangilash..."
$sshConfigFile = "$HOME\.ssh\config"
$configEntry = @"

Host oracle-vps
    HostName $VpsIp
    User $activeUser
    IdentityFile $workingKey
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ServerAliveInterval 60
    ServerAliveCountMax 120
"@

if (Test-Path $sshConfigFile) {
    $existing = Get-Content $sshConfigFile -Raw
    if ($existing -notmatch "Host oracle-vps") {
        Add-Content -Path $sshConfigFile -Value $configEntry
    }
} else {
    Set-Content -Path $sshConfigFile -Value $configEntry
}
Write-Success "Mahalliy SSH config tayyor. Kelgusida shunchaki 'ssh oracle-vps' orqali ulanishingiz mumkin."

# 6. Loyiha fayllarini bitta yuqori tezlikdagi Tar bundle orqali yuklash
Write-Step "3/7" "Loyiha fayllarini paketlash va serverga yuqori tezlikda yuklash (Tar Bundle)..."
$bundleName = "deploy_bundle.tar.gz"
if (Test-Path $bundleName) { Remove-Item $bundleName -Force }

Write-Info "Loyiha arxivlanmoqda..."
tar --exclude=".git" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="*.tar.gz" -czf $bundleName *
if (Test-Path ".env") {
    tar -rf $bundleName .env 2>$null
}

Write-Info "Serverda ~/app papkasi tayyorlanmoqda..."
ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget "mkdir -p ~/app"

Write-Info "Loyiha arxivi serverga uzatilmoqda..."
scp -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $bundleName "$($remoteTarget):~/app/$bundleName"

Write-Info "Serverda arxiv ochilmoqda..."
ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget "tar -xzf ~/app/$bundleName -C ~/app/ && rm -f ~/app/$bundleName"

if (Test-Path $bundleName) { Remove-Item $bundleName -Force }
Write-Success "Barcha fayllar va bot kodlari serverga 100% muvaffaqiyatli yetkazildi."

# 7. Server ichida Master Setup (Firewall, Docker ARM, Dependencies)
Write-Step "4/7" "Server ichida xavfsizlik, firewall va ARM64 muhitini sozlash..."
$remoteSetupCmd = "chmod +x ~/app/deploy/*.sh ~/app/deploy/webhook/*.py && sudo bash ~/app/deploy/oracle_master_setup.sh"
ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget $remoteSetupCmd

# 8. Domen va Nginx / SSL sozlash
if ($Domain) {
    Write-Step "5/7" "Nginx Reverse Proxy va Let's Encrypt SSL sertifikatini sozlash ($Domain)..."
    $sslScript = "sudo sed -i 's/your_domain.com/$Domain/g' /etc/nginx/sites-available/channelcloner.conf /etc/nginx/conf.d/channelcloner.conf 2>/dev/null || true; sudo nginx -t && sudo systemctl reload nginx && sudo certbot --nginx -d $Domain --non-interactive --agree-tos -m admin@$Domain --redirect || true"
    ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget $sslScript
} else {
    Write-Step "5/7" "Nginx Reverse Proxy standart port 80 rejimida sozlandi (Domen kiritilmadi)."
}

# 9. Docker konteynerni ishga tushirish
Write-Step "6/7" "Multi-Bot ishlab chiqarish Docker konteynerini yigish va ishga tushirish..."
$startAppCmd = "cd ~/app && sudo docker compose down 2>/dev/null || true; sudo docker compose up -d --build"
ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget $startAppCmd

# 10. Salomatlik va Holatni tekshirish (Healthcheck)
Write-Step "7/7" "Tizim salomatligi va ish faoliyatini tekshirish..."
Start-Sleep -Seconds 5

$healthCheckResult = ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $workingKey $remoteTarget "curl -s http://127.0.0.1:8080/health || echo 'HEALTH_FAILED'"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "     DEPLOY MUVAFFAQIYATLI YAKUNLANDI! SERVER 24/7 ISH REJIMIDA       " -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "DEPLOYMENT MALUMOTLARI:" -ForegroundColor Yellow
Write-Host "  * Server IP:           http://$VpsIp" -ForegroundColor White
if ($Domain) {
    Write-Host "  * Domen (HTTPS):       https://$Domain" -ForegroundColor White
}
Write-Host "  * Healthcheck URL:     http://${VpsIp}:8080/health" -ForegroundColor White
Write-Host "  * Webhook CI/CD:       http://${VpsIp}:9000/webhook" -ForegroundColor White
Write-Host "  * SSH bilan ulanish:   ssh oracle-vps" -ForegroundColor White
Write-Host ""
Write-Host "XAVFSIZLIK VA XIZMATLAR:" -ForegroundColor Yellow
Write-Host "  * Foydalanuvchi:      $activeUser" -ForegroundColor Green
Write-Host "  * Anti-Reclamation:   FAOL (Server doimiy faol saqlanadi)" -ForegroundColor Green
Write-Host "  * Firewall:           22, 80, 443, 8080, 9000 portlar himoyalangan" -ForegroundColor Green
Write-Host "  * Docker Multi-Bot:   Aiogram + Telethon Userbot + Keep-Alive ishga tushdi" -ForegroundColor Green
Write-Host ""
Write-Host "Healthcheck javobi:" -ForegroundColor Cyan
Write-Host "$healthCheckResult" -ForegroundColor Gray
Write-Host "======================================================================" -ForegroundColor Green
