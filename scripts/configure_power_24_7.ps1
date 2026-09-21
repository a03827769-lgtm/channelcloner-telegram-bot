# ==============================================================================
# Telegram Channel Cloner - 24/7 Background & Lock Screen Power Configuration
# ==============================================================================

Write-Host "Configuring Windows power scheme for 24/7 lock-screen background operation..." -ForegroundColor Cyan

# 1. Disable Sleep / Standby on AC Power (Never sleep while plugged in)
powercfg /change standby-timeout-ac 0
Write-Host "[OK] Standby on AC set to NEVER (0 minutes)" -ForegroundColor Green

# 2. Disable Hibernate on AC Power
powercfg /change hibernate-timeout-ac 0
Write-Host "[OK] Hibernate on AC set to NEVER" -ForegroundColor Green

# 3. Disable Disk Turn Off on AC Power
powercfg /change disk-timeout-ac 0
Write-Host "[OK] Disk timeout on AC set to NEVER" -ForegroundColor Green

# 4. Set Display Turn Off to 5 minutes on AC (Saves screen life while CPU & Docker stay active)
powercfg /change monitor-timeout-ac 5
Write-Host "[OK] Display timeout on AC set to 5 minutes" -ForegroundColor Green

# 5. Set Lid Close Action to 'Do Nothing' on AC Power (LIDACTION GUID)
powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
Write-Host "[OK] Lid Close action on AC set to DO NOTHING" -ForegroundColor Green

# 6. Apply active power scheme
powercfg /setactive SCHEME_CURRENT
Write-Host "[SUCCESS] Windows power settings successfully updated for 24/7 background operation." -ForegroundColor Green
