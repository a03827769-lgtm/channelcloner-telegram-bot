# Original User Request

## Initial Request — 2026-08-29T17:31:32+05:00

Telegram Channel Cloner (`@klonlabot`, `@klonlaadminbot`, Telethon MTProto) loyihasini 24/7 to'xtovsiz, mutlaqo bepul (Zero-Cost PaaS), eng tez va xavfsiz serverga joylashtirish. GitHub'da shaxsiy (Private) `channelcloner-telegram-bot` repozitoriyasini ochib, aiohttp keep-alive healthcheck serveri orqali doimiy uyg'oq (never-sleep) holatda ishlashini ta'minlash.

Working directory: `c:\Users\victus\Desktop\channelcloner`
Integrity mode: development

## Requirements

### R1. Shaxsiy (Private) GitHub Repozitoriyasi va Xavfsizlik
Loyiha uchun `a03827769-lgtm` GitHub akkauntida `channelcloner-telegram-bot` nomli shaxsiy (private) repozitoriya yaratish. `.gitignore` fayliga `.env`, `*.session`, `cloner.db*`, `temp_media/`, `__pycache__/` kabi maxfiy va og'ir fayllarni kiritib, tokenlar xavfsizligini 100% kafolatlash.

### R2. 24/7 Keep-Alive HTTP Healthcheck Serveri
Koyeb, Render va boshqa PaaS bulutli platformalari botni uxlab qolishidan (idle sleep) saqlashi uchun `run.py` ga yengil asinxron HTTP server (`aiohttp` yoki `http.server`) qo'shish. Port `PORT` (default: 8080) da `/` va `/health` yo'llarida `200 OK` javob berish, botlar va MTProto bilan parallel ishlash.

### R3. Docker & Bulutga Tayyorlik (Cloud-Ready Deployment Files)
Loyihada `Dockerfile`, `docker-compose.yml`, va platforma sozlamalari (`koyeb.yaml` / `render.yaml`) ni yaratish/yangilash. Konteyner start berganda avtomatik portni tinglashi, barcha kerakli kutubxonalar (`ffmpeg`, `gcc` va Python paketlari) to'g'ri o'rnatilishi.

### R4. Atrof-muhit O'zgaruvchilari (Environment Variables) Shablonlari
Server boshqaruv paneliga kiritilishi lozim bo'lgan barcha parametrlar (`BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_IDS`, `TELETHON_SESSION`) uchun aniq, to'liq `.env.example` va ko'rsatmalar taqdim etish.

## Acceptance Criteria

### Repository & Security
- [ ] `channelcloner-telegram-bot` nomli GitHub shaxsiy (Private) repozitoriyasi yaratilgan va kod push qilingan.
- [ ] Maxfiy kalitlar (`.env`, `session`, `*.db`) repozitoriyaga chiqmaganligi tekshirilgan.

### Healthcheck & Keep-Alive
- [ ] `http://localhost:8080/health` yoki server IP:8080 da `{"status": "ok", "bot": "running"}` JSON javobi qaytadi.
- [ ] Uptime monitoring (UptimeRobot / CronJob) orqali konteyner 24/7 uyg'oq turishga tayyor holatga keltirilgan.

### Build & Run Verification
- [ ] Docker konteyneri lokal va bulutda xatosiz yig'iladi va ishga tushadi.
- [ ] Botlar (`@klonlabot`, `@klonlaadminbot`) hamda Telethon MTProto uzluksiz ishlaydi.
