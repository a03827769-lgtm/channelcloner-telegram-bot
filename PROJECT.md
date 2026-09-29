# Loyiha xaritasi: Telegram Channel Cloner

## Komponentlar (bitta jarayon — `run.py`)
- **Ommaviy bot** (`bot/`, Aiogram 3 long-polling): kanal juftliklari, filtrlar, tariflar (Telegram Stars), Story menyusi.
- **Admin bot** (`admin_bot/`): super adminlar (`PRIMARY_SUPER_ADMIN_ID`, `ADMIN_IDS`) va vakolat berilgan adminlar;
  broadcast, zaxira, MTProto login, tizim holati.
- **MTProto listener** (`services/telethon_listener.py`) va klonlash yadrosi (`services/cloner_engine.py`).
- **HTTP server** (aiohttp): `/health` (liveness, doim 200: `ok`/`degraded` + `pid`), `/ready` (503 → 200),
  Mini App SPA (`webapp/dist`) va `/api/*` (`services/api_routes.py`).
- **Holat**: SQLite WAL (`DB_PATH`), vault kaliti (`VAULT_KEY_PATH`) yoki `ENCRYPTION_KEY`, loglar `data/app.log`.

## Ishga tushirish topologiyalari (faqat bittasi!)
| Topologiya | Boshqaruvchi | Tashqi kirish |
|---|---|---|
| Windows host | `scripts/windows_keepalive_watchdog.py` (+ `scripts/setup_autostart.py`) | nomli Cloudflare tunnel (`cloudflared tunnel run miniapp`) |
| Docker (VPS/Linux) | `docker compose --profile docker-bot` (`restart: unless-stopped`) | host nginx + certbot (`deploy/`) |
| Render | `render.yaml` (pullik tarif + disk, `autoDeploy: false`) | Render HTTPS |

Invariantlar:
- Bitta token/sessiya — bitta jarayon. Watchdog konteyner ishlayotganda host botni ishga tushirmaydi va host bot
  ishlayotganda paydo bo'lgan konteynerni to'xtatadi; `run.py` `app.lock` orqali ikkinchi nusxani rad etadi.
- Konteynerdagi holat faqat nomli volume/diskda (`/app/data`), hech qachon Windows bind-mount'da emas.
- Maxfiy fayllar (`.env`, `*.db`, `.vault_key`, `.cloudflared/`) git'ga ham, Docker obrazga ham tushmaydi
  (`.gitignore`, `.dockerignore`), deploy vositalari ularni yubormaydi.

## Infratuzilma fayllari
- `Dockerfile` — 3 bosqich: Mini App build (node:20), Python wheel'lar, runtime (python:3.11-slim, FFmpeg, tini,
  jemalloc, non-root `appuser`); Playwright Chromium faqat `--build-arg INSTALL_PLAYWRIGHT=true` bilan.
- `docker-compose.yml` — standart: `miniapp_web` (nginx edge) + `cloudflared`; `docker-bot` profili: botning o'zi.
- `render.yaml`, `koyeb.yaml` — bitta instansiya, doimiy disk, `/ready` tekshiruvi.
- `Procfile` — `web: python run.py` (bitta jarayon turi).
- `deploy/` — `oracle_master_setup.sh` (server tayyorlash), `nginx/channelcloner.conf`, `systemd/` va `webhook/`
  shablonlari, `anti_reclaim.py` (ixtiyoriy, Oracle Always Free).
- `scripts/` — `windows_keepalive_watchdog.py`, `setup_autostart.py`, `deploy_remote.py` (+ `deploy_one_click.ps1`),
  `repair_db.py`, `decrypt_backup.py`, `check_session.py`, `configure_power_24_7.ps1`.

## Sifat nazorati
- Testlar: `python -m pytest -q -p no:cacheprovider --timeout=120` (`requirements-dev.txt`); `tests/conftest.py`
  bazani, vault kalitini, tokenlarni va `.env`ni izolyatsiya qiladi, tarmoqni bloklaydi va yopilmay qolgan
  `db_manager` patchlarini xato sifatida ko'rsatadi.
- CI (`.github/workflows/ci.yml`): ruff (E9, F63, F7, F82), pytest (Python 3.10 va 3.11), `import run` smoke testi,
  Mini App `tsc` + `vite build`, `docker compose config` va `docker build`.
