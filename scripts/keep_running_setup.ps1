# ==============================================================================
# Telegram Channel Cloner - 24/7 Background & Lock Screen Keep-Awake Setup
# ==============================================================================

Write-Host "Noutbuk Lock holatida ham botni 24/7 ishlashini sozlash..." -ForegroundColor Cyan

# 1. Standby / Sleep vaqtini cheksiz (Never) ga o'rnatish
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0

# 2. Hibernate (Uyqu) vaqtini cheksiz (Never) ga o'rnatish
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0

# 3. Noutbuk qopqogi yopilganda ham o'chib qolmaslik (Lid Close -> Do Nothing)
powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0

# 4. Sozlamalarni faollashtirish
powercfg /setactive SCHEME_CURRENT

Write-Host "Barcha quvvat sozlamalari muvaffaqiyatli o'rnatildi!" -ForegroundColor Green
Write-Host "Endi siz noutbukni Win + L orqali qulflasangiz ham yoki qopqogini yopsangiz ham bot 24/7 fonda ishlayveradi." -ForegroundColor Yellow
