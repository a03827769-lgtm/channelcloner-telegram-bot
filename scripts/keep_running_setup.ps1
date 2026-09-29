# ==============================================================================
# Telegram Channel Cloner - 24/7 quvvat sozlamalari (faqat tarmoqqa ulanganda / AC)
#
# configure_power_24_7.ps1 ning takrori edi va batareyada (DC) ham uyqu va qopqoq
# yopilishini o'chirib qo'yardi (qizib ketish va batareya tugashi xavfi). Endi faqat
# o'sha AC sozlamalarini qo'llaydi; batareya sozlamalariga tegmaydi.
# ==============================================================================
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "configure_power_24_7.ps1")
Write-Host "Batareya (DC) rejimi sozlamalari o'zgartirilmadi: noutbuk tarmoqqa ulangan holda 24/7 ishlaydi." -ForegroundColor Yellow
