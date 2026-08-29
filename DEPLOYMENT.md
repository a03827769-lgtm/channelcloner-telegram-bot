# Telegram Channel Cloner — 24/7 Production Deployment Guide

Bu qo'llanma Telegram Channel Cloner tizimini (`@klonlabot`, `@klonlaadminbot`, Telethon MTProto Userbot) 24/7 to'xtovsiz, mutlaqo bepul (Zero-Cost PaaS), eng yuqori xavfsizlik va tezlikda bulutli platformalarga joylashtirish bo'yicha to'liq ko'rsatmalarni o'z ichiga oladi.

---

## Mundarija
1. [Arxitektura va Keep-Alive Tizimi](#1-arxitektura-va-keep-alive-tizimi)
2. [Atrof-muhit O'zgaruvchilari (Environment Variables)](#2-atrof-muhit-ozgaruvchilari-environment-variables)
3. [Koyeb PaaS orqali 24/7 Bepul Joylashtirish](#3-koyeb-paas-orqali-247-bepul-joylashtirish)
4. [Render PaaS orqali Joylashtirish](#4-render-paas-orqali-joylashtirish)
5. [Docker va Docker Compose orqali VPS/Serverda Ishga Tushirish](#5-docker-va-docker-compose-orqali-vpsserverda-ishga-tushirish)
6. [24/7 Doimiy Uyg'oq Tutish (Uptime Monitoring)](#6-247-doimiy-uygoq-tutish-uptime-monitoring)
7. [Telethon MTProto Sessiyasini Bulutga O'tkazish](#7-telethon-mtproto-sessiyasini-bulutga-otkazish)
8. [Nosozliklarni Bartaraf Qilish (Troubleshooting)](#8-nosozliklarni-bartaraf-qilish-troubleshooting)

---

## 1. Arxitektura va Keep-Alive Tizimi

Bot quyidagi komponentlardan tashkil topgan:
- **Aiogram 3 Dispatcher**: Ommaviy bot (`@klonlabot`) va Admin boshqaruv boti (`@klonlaadminbot`) uchun yuqori tezlikdagi asinxron polling.
- **Telethon MTProto Listener**: Xususiy/yopiq kanallardagi yangi xabarlarni real vaqt rejimida ushlab oluvchi userbot mexanizmi.
- **Embedded aiohttp Keep-Alive Server**: `0.0.0.0:${PORT:-8080}` portida ishlovchi yengil HTTP server (`/` va `/health` yo'llari). U tashqi monitoring so'rovlariga `<2ms` ichida `200 OK` JSON qaytaradi va bepul bulutli konteynerlarning uxlab qolishini (idle sleep) 100% oldini oladi.

```
                  ┌────────────────────────────────────────┐
                  │          Tashqi Pinger                 │
                  │   (UptimeRobot / Koyeb Healthcheck)    │
                  └───────────────────┬────────────────────┘
                                      │ HTTP GET /health
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   Docker Container (Port 8080)                           │
│                                                                          │
│  ┌──────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │ aiohttp Keep-Alive   │  │           Aiogram 3 Polling              │  │
│  │ Healthcheck Server   │  │   (@klonlabot & @klonlaadminbot)         │  │
│  └──────────────────────┘  └──────────────────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                 Telethon MTProto Userbot Listener                 │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─────────────────────────────┐    ┌─────────────────────────────────┐  │
│  │ SQLite WAL DB (cloner.db)   │    │ FFmpeg Video & Media Engine     │  │
│  └─────────────────────────────┘    └─────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Atrof-muhit O'zgaruvchilari (Environment Variables)

Barcha platformalar uchun quyidagi 9 ta o'zgaruvchi talab qilinadi:

| O'zgaruvchi | Majburiy | Standart | Tavsif |
|---|---|---|---|
| `BOT_TOKEN` | **Ha** | — | Telegram BotFather'dan olingan ommaviy bot tokeni (`@klonlabot`) |
| `ADMIN_BOT_TOKEN` | Yo'q (Tavsiya) | — | Admin boshqaruv boti tokeni (`@klonlaadminbot`) |
| `TELEGRAM_API_ID` | **Ha** | — | my.telegram.org'dan olingan raqamli App ID |
| `TELEGRAM_API_HASH` | **Ha** | — | my.telegram.org'dan olingan 32 belgili App Hash |
| `TELETHON_SESSION` | Yo'q (Tavsiya) | — | Bulut uchun shifrlangan/xom Telethon StringSession qatori |
| `ADMIN_IDS` | **Ha** | — | Super admin Telegram ID raqamlari (vergul bilan ajratilgan) |
| `DB_PATH` | Yo'q | `database/cloner.db` | SQLite ma'lumotlar bazasi fayli yo'li |
| `TEMP_DOWNLOAD_DIR` | Yo'q | `temp_media` | Vaqtinchalik media yuklab olish katalogi |
| `PORT` | Yo'q | `8080` | Keep-Alive HTTP healthcheck porti |

---

## 3. Koyeb PaaS orqali 24/7 Bepul Joylashtirish

[Koyeb](https://www.koyeb.com) — 512MB RAM, Frankfurt (Yevropa) hududi va yuqori tezlikdagi bepul konteynerlarni taqdim etuvchi zamonaviy PaaS.

### Bosqichma-bosqich qo'llanma:
1. **GitHub Repozitoriyasini ulash**:
   - Koyeb boshqaruv paneliga kiring ([app.koyeb.com](https://app.koyeb.com)).
   - **Create App** tugmasini bosing va **GitHub** manbasini tanlang.
   - `a03827769-lgtm/channelcloner-telegram-bot` shaxsiy repozitoriyasini tanlang (`main` branch).

2. **Builder & Konfiguratsiya**:
   - **Deployment method**: `Dockerfile` avtomatik aniqlanadi.
   - **Region**: `Frankfurt (fra)` tanlang (Telegram serverlariga eng yaqin va eng past ping).
   - **Instance type**: `Free` (Eco tier: 512MB RAM, 0.1 vCPU).
   - **Scaling**: `min: 1`, `max: 1` (Qat'iy 1 ta nusxa, aks holda MTProto sessiya to'qnashuvi yuz beradi).

3. **Port va Healthcheck**:
   - **Port**: `8080` (HTTP).
   - **Health check path**: `/health`.
   - **Initial delay**: `15` soniya.
   - **Timeout**: `5` soniya.

4. **Environment Variables**:
   Koyeb **Environment variables** bo'limida quyidagi o'zgaruvchilarni kiriting:
   - `BOT_TOKEN` (Secret sifatida saqlang)
   - `ADMIN_BOT_TOKEN` (Secret)
   - `TELEGRAM_API_ID` (Secret)
   - `TELEGRAM_API_HASH` (Secret)
   - `TELETHON_SESSION` (Secret — `cloud_env_ready.txt` dagi qiymat)
   - `ADMIN_IDS` (Secret)
   - `PORT`: `8080`
   - `DB_PATH`: `database/cloner.db`
   - `TEMP_DOWNLOAD_DIR`: `temp_media`

5. **Deploy**:
   - **Deploy** tugmasini bosing. Loyiha 60-90 soniya ichida avtomatik yig'iladi va ishga tushadi.

---

## 4. Render PaaS orqali Joylashtirish

[Render](https://render.com) — bepul Docker Web Service qo'llab-quvvatlaydigan bulutli platforma.

### Bosqichma-bosqich qo'llanma:
1. [dashboard.render.com](https://dashboard.render.com) ga kiring.
2. **New +** -> **Web Service** ni tanlang.
3. GitHub repozitoriyangizni (`channelcloner-telegram-bot`) ulang.
4. Parametrlarni quyidagicha belgilang:
   - **Name**: `channelcloner-telegram-bot`
   - **Region**: `Frankfurt (EU Central)`
   - **Branch**: `main`
   - **Runtime**: `Docker`
   - **Instance Type**: `Free`
   - **Health Check Path**: `/health`
5. **Environment Variables** bo'limida `.env.example` dagi 9 ta o'zgaruvchini kiriting.
6. **Create Web Service** tugmasini bosing.

*(Yoki to'g'ridan-to'g'ri `render.yaml` Blueprint orqali 1 ta tugma bilan deploy qiling).*

---

## 5. Docker va Docker Compose orqali VPS/Serverda Ishga Tushirish

Agar shaxsiy Linux VPS (Ubuntu/Debian) da ishlatmoqchi bo'lsangiz:

```bash
# 1. Repozitoriyani klonlash
git clone https://github.com/a03827769-lgtm/channelcloner-telegram-bot.git
cd channelcloner-telegram-bot

# 2. Atrof-muhit faylini yaratish
cp .env.example .env
nano .env  # O'z tokenlaringizni kiriting

# 3. Docker Compose orqali fonga ishga tushirish
docker-compose up -d --build

# 4. Holatni tekshirish
docker-compose ps
curl -i http://127.0.0.1:8080/health

# 5. Loglarni kuzatish
docker-compose logs -f --tail=100
```

---

## 6. 24/7 Doimiy Uyg'oq Tutish (Uptime Monitoring)

Koyeb va Render bepul tariflarida tashqi HTTP so'rov kelmasa, konteyner 15 daqiqada uxlab qolishi mumkin. Buni 100% bartaraf etish uchun:

1. [UptimeRobot.com](https://uptimerobot.com) saytida bepul ro'yxatdan o'ting.
2. **Add New Monitor** tugmasini bosing:
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: `Telegram Channel Cloner Healthcheck`
   - **URL (or IP)**: `https://<sizning-koyeb-yoki-render-app>.koyeb.app/health`
   - **Monitoring Interval**: `Every 5 minutes`
3. **Create Monitor** ni bosing.

Endi UptimeRobot har 5 daqiqada `/health` manziliga GET so'rov yuborib turadi. Natijada bot hech qachon uxlamaydi va 24/7 uzluksiz ishlaydi.

---

## 7. Telethon MTProto Sessiyasini Bulutga O'tkazish

Bulutli konteynerlar har qayta ishga tushganda diskdagi fayllar yangilanishi mumkin. Userbot sessiyasini yo'qotmaslik uchun:

1. Lokal kompyuterda yoki `@klonlaadminbot` orqali MTProto tizimiga OTP kodi bilan kiring.
2. Shifrlangan `TELETHON_SESSION` qatorini oling (`enc:gAAAAAB...` formati).
3. Ushbu qatorni Koyeb/Render panelida `TELETHON_SESSION` o'zgaruvchisiga kiriting.
4. Bot qayta yonganda ushbu sessiyani avtomatik o'qiydi va qayta telefon raqam yoki kod so'ramaydi.

---

## 8. Nosozliklarni Bartaraf Qilish (Troubleshooting)

### 1. `telethon_connected: false` holati
- Sababi: API_ID/API_HASH xato yoki TELETHON_SESSION kiritilmagan.
- Yechim: `@klonlaadminbot` orqali `/start` -> **MTProto Userbot** bo'limiga kirib qayta OTP login qiling yoki `TELETHON_SESSION` ni yangilang.

### 2. Video watermark qo'yishda sekinlik
- Sababi: Kam yadroli vCPU da og'ir 4K videolarni qayta ishlash.
- Yechim: `services/video_watermark_service.py` da `-preset veryfast` va `-threads 1` rejimlari sozlangan.

### 3. Port bandligi yoki HTTP xatosi
- Konteyner ichida port doimo `8080` ga bog'lanadi. Tashqi portni `PORT` o'zgaruvchisi orqali boshqarishingiz mumkin.
