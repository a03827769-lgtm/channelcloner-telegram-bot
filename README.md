# 🤖 Telegram Kanal & Guruh Kloner (Telethon + Aiogram 3) va Ko'chmas Mulk Story Platformasi

Begona ochiq va yopiq Telegram kanallari/guruhlaridagi postlarni real vaqtda ushlab, reklama va havolalardan tozalab,
tarjima va imzo bilan o'z kanalingizga joylaydi. Qo'shimcha: VIP ko'chmas mulk Story kloni, alohida admin bot,
Telegram Stars (XTR) to'lovlari, Telegram Mini App (React) va shifrlangan zaxira nusxalar.

---

## 🌟 Imkoniyatlar

1. ⚡️ **Real vaqtda klonlash** — Telethon MTProto listener yangi postni soniyalar ichida ushlaydi.
2. 🎯 **Barcha formatlar** — matn, rasm, video, albomlar, ovozli va dumaloq video, hujjat, audio, stiker, GIF, so'rovnoma.
3. 🧹 **Tozalash va filtrlar** — `@username`/havolalarni tozalash, qora ro'yxat, so'z almashtirish, imzo, suv belgisi.
4. 🔄 **Tarixni ko'chirish** — manba kanaldagi eski postlarni tartib bilan ko'chirish.
5. 🏢 **VIP Story kloni** — e'lonlardan 1080×1920 Story kartochkalari va video slayd-shoular (Pillow, ixtiyoriy Playwright).
6. ⭐️ **Telegram Stars obunalari** — PRO/VIP tariflar, idempotent to'lovlar.
7. 📱 **Mini App** — `webapp/` (React + Vite), bot jarayonining o'zi `/`, `/app` va `/api/*` da tarqatadi.
8. 🛡 **Admin bot va xavfsizlik** — alohida admin bot (`ADMIN_BOT_TOKEN`), sessiyalar va zaxiralar Fernet bilan shifrlangan.

---

## 🏗 Arxitektura

Bitta jarayon (`run.py`) hammasini ishga tushiradi:

- **Aiogram 3** — ommaviy bot (`bot/`) va admin bot (`admin_bot/`, `AdminStrictAuthMiddleware`).
- **Telethon** — MTProto listener va klonlash yadrosi (`services/telethon_listener.py`, `services/cloner_engine.py`).
- **aiohttp** — `/health`, `/ready`, Mini App SPA va REST API (`services/api_routes.py`).
- **SQLite (aiosqlite, WAL)** — `database/db_manager.py`, `database/models.py`.
- **Story** — `services/story_cloner_service.py`, `services/story_renderer.py`, `services/story_video_generator.py` (FFmpeg).
- **Xavfsizlik** — `services/security_vault.py` (Fernet; `ENCRYPTION_KEY` yoki mashina kaliti).

**Muhim:** bot faqat **bitta nusxada** ishlashi kerak (Windows host YOKI Docker YOKI server). Batafsil: [DEPLOYMENT.md](DEPLOYMENT.md).

---

## 🚀 Tez boshlash (lokal / Windows host)

```bash
pip install -r requirements.txt          # Python 3.10+ (3.11 tavsiya etiladi)
python setup_wizard.py                   # .env ni yaratadi/yangilaydi (mavjud qiymatlar saqlanadi)
cd webapp && npm ci && npm run build     # Mini App (ixtiyoriy)
python run.py                            # yoki 24/7: python scripts/setup_autostart.py install --start
```

Barcha sozlamalar: [.env.example](.env.example) va [DEPLOYMENT.md](DEPLOYMENT.md#2-sozlamalar-env).
`setup_wizard.py` `BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `TELEGRAM_API_ID/HASH`, `PRIMARY_SUPER_ADMIN_ID`, `ADMIN_IDS`,
`WEBAPP_URL` va Telethon sessiyasini so'raydi, `ENCRYPTION_KEY` bo'lmasa yaratadi; Enter bosilsa mavjud qiymat qoladi,
maxfiy qiymatlar ekranga chiqmaydi. `ENABLE_PLAYWRIGHT` standart holatda `false` (Pillow renderi).

---

## 🧪 Testlar

```bash
pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider --timeout=120
```

Testlar vaqtinchalik baza va soxta tokenlar bilan ishlaydi (`tests/conftest.py`): production `.env` o'qilmaydi
(`CLONER_ENV_FILE=""`), Telegram va tashqi tarmoqqa so'rovlar bloklanadi, har bir testdan keyin `settings` tiklanadi.
Jonli infratuzilma testlari faqat `RUN_LIVE_TESTS=1` bilan ishga tushadi. CI: `.github/workflows/ci.yml`
(pytest, ruff, Mini App build, Docker build).

---

## 🔒 Zaxira va xavfsizlik

- Admin bot → **"💾 Baza Nusxasi"** shifrlangan `.zip.enc` beradi. Ochish:
  `python scripts/decrypt_backup.py backup.zip.enc --extract` (yoki `--key "<kalit>"`).
- `.env`, `database/.vault_key`, `database/*.db`, `data/`, `.cloudflared/` hech qachon git'ga yoki Docker obrazga tushmaydi.
- Kalit/token oshkor bo'lsa: @BotFather'da tokenni `/revoke` qiling, yangi `ENCRYPTION_KEY` o'rnatishdan oldin eski
  kalit bilan shifrlangan ma'lumotlarni hisobga oling (eski vault kaliti fallback sifatida o'qiladi).

---

## 📂 Fayllar tuzilmasi

```
channelcloner/
├── run.py                    # Kirish nuqtasi: botlar, listener, HTTP server, fon xizmatlari
├── setup_wizard.py           # Interaktiv .env ustasi
├── config/                   # settings.py (pydantic-settings), plans.py, limits.py
├── database/                 # db_manager.py, models.py, fsm_storage.py
├── bot/                      # Ommaviy bot: handlers, keyboards, middlewares, states
├── admin_bot/                # Admin bot: handlers, keyboards, middlewares, permissions.py
├── services/                 # Klonlash, listener, media, story, API, xavfsizlik va boshqalar
├── webapp/                   # Telegram Mini App (React + Vite)
├── assets/                   # Story fonlari, audio, logo
├── scripts/                  # Windows watchdog/autostart, deploy_remote.py, repair_db.py, decrypt_backup.py
├── deploy/                   # VPS: oracle_master_setup.sh, nginx, systemd, webhook, anti_reclaim.py
├── tests/                    # pytest testlari
├── Dockerfile, docker-compose.yml, render.yaml
└── requirements.txt, requirements-dev.txt
```
