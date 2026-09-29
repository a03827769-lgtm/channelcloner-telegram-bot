# Telegram Channel Cloner — ishga tushirish va joylashtirish (deployment)

## 1. Asosiy qoida: faqat BITTA nusxa

Bitta `BOT_TOKEN` / Telethon sessiyasi bir vaqtda faqat **bitta jarayonda** ishlashi mumkin. Ikkinchi nusxa
(Windows + Docker, ikkita server, "zero-downtime" deploy) quyidagilarga olib keladi:

* `TelegramConflictError` (ikki poller), postlarning takrorlanishi;
* `AUTH_KEY_DUPLICATED` — Telegram Telethon sessiyasini bekor qiladi, qayta login kerak bo'ladi;
* SQLite bazasining ikki joyda bo'linib ketishi (to'lovlar va sozlamalar yo'qoladi).

Shuning uchun quyidagi yo'llardan **faqat bittasini** tanlang:

| Yo'l | Qachon | Holat (state) qayerda |
|---|---|---|
| **A. Windows host** (watchdog + `python run.py`) | hozirgi production | `database/cloner.db`, `database/.vault_key`, `data/`, `logs/` |
| **B. VPS / Linux: Docker** (`docker-bot` profili) | alohida server | Docker volume `channelcloner_bot_data` → `/app/data` |
| **C. Render** (pullik tarif + disk) | PaaS kerak bo'lsa | Render disk → `/app/data` |

Bepul PaaS tariflari (Render Free, Koyeb Free va h.k.) **qo'llab-quvvatlanmaydi**: doimiy disk yo'q (baza,
to'lovlar va vault kaliti har deployda yo'qoladi), servis uxlab qoladi va rolling deploy ikki nusxani
bir vaqtda ishga tushiradi.

## 2. Sozlamalar (`.env`)

`cp .env.example .env` yoki `python setup_wizard.py`. `.env` doim loyiha ildizidan o'qiladi (ish katalogidan qat'i nazar)
va hech qachon git'ga qo'shilmaydi.

| O'zgaruvchi | Majburiy | Standart | Tavsif |
|---|---|---|---|
| `BOT_TOKEN` | **Ha** | — | Asosiy bot tokeni (@BotFather) |
| `ADMIN_BOT_TOKEN` | Tavsiya | — | Alohida admin bot tokeni (`BOT_TOKEN` dan farqli bo'lsin) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | **Ha** | — | my.telegram.org dan olinadi |
| `TELETHON_SESSION` | Yo'q | — | StringSession; bo'lmasa admin bot (MTProto bo'limi) orqali OTP bilan ulanadi |
| `PRIMARY_SUPER_ADMIN_ID` | Tavsiya | `0` | Asosiy super admin Telegram ID |
| `ADMIN_IDS` | Yo'q | — | Qo'shimcha super adminlar (vergul bilan) |
| `ENCRYPTION_KEY` | Tavsiya | — | Sessiya va zaxiralar uchun master kalit. Bo'sh bo'lsa `VAULT_KEY_PATH` fayli yaratiladi |
| `VAULT_KEY_PATH` | Yo'q | `database/.vault_key` | Mashina kaliti fayli (Docker/Render: `/app/data/.vault_key`) |
| `WEBAPP_URL` | Mini App uchun | `http://localhost:8080` | Mini App'ning doimiy HTTPS manzili (nomli tunnel domeni) |
| `SUPPORT_USERNAME` | Yo'q | `admin` | Private rejimda ko'rsatiladigan admin @username |
| `PROXY_URL` | Yo'q | — | Bot API uchun HTTP/SOCKS proksi |
| `GEMINI_API_KEY` | Yo'q | — | AI parafraz (Google Gemini) |
| `DB_PATH` | Yo'q | `database/cloner.db` | SQLite fayli (Docker/Render: `/app/data/cloner.db`) |
| `TEMP_DOWNLOAD_DIR` | Yo'q | `temp_media` | Vaqtinchalik media |
| `PORT` | Yo'q | `8080` | HTTP server (health, Mini App, API). Jarayon muhitidagi `PORT` `.env` dan ustun |
| `ENABLE_PLAYWRIGHT` | Yo'q | `false` | Chromium renderi (ko'p RAM); o'chiq bo'lsa Pillow ishlatiladi |
| `DROP_PENDING_UPDATES` | Yo'q | `false` | Ishga tushganda kutilayotgan yangilanishlarni tashlash |
| `AUTO_RELOAD` | Yo'q | `false` | Kod o'zgarsa jarayonni qayta ishga tushirish (faqat ishlab chiqishda) |

Infratuzilma o'zgaruvchilari (ixtiyoriy): `CLOUDFLARED_TUNNEL_NAME` (watchdog kuzatadigan tunnel, standart `miniapp`),
`MINIAPP_WEB_PORT` (8081), `BOT_HTTP_PORT` (8080, `docker-bot` loopback porti), `INSTALL_PLAYWRIGHT` (Docker build arg),
`CLONER_ENV_FILE` (o'qiladigan `.env` yo'li; standart — loyiha ildizidagi `.env`, bo'sh qiymat faylni o'qimaydi).

**Kalitlar haqida:** `ENCRYPTION_KEY` yoki vault kaliti yo'qolsa, shifrlangan sessiyalar va zaxiralarni ochib bo'lmaydi.
Bazani boshqa joyga ko'chirsangiz, kalitni ham ko'chiring (yoki bir xil `ENCRYPTION_KEY` bering).

## 3. HTTP endpointlar

* `GET /health` — liveness: jarayon ishlayotgan bo'lsa **har doim 200**; tanasida `status` (`ok` yoki ishga tushish /
  polling uzilishi paytida `degraded`), `pid`, `uptime`, `db_ready`, `polling_alive`, `telethon_connected`.
* `GET /ready` — readiness: baza tayyor va bot polling qilayotgan bo'lsa 200, aks holda 503. PaaS / deploy tekshiruvlari
  shundan foydalanadi.
* `/` va `/app` — Mini App (React/Vite, `webapp/dist`), `/api/*` — Mini App REST API.

## 4. Yo'l A — Windows host (hozirgi production)

1. Python 3.11, keyin: `pip install -r requirements.txt`
2. `python setup_wizard.py` (yoki `.env` ni qo'lda to'ldiring)
3. Mini App: `cd webapp && npm ci && npm run build` (natija `webapp/dist`, bot o'zi tarqatadi)
4. Nomli Cloudflare tunnel (bir marta): `cloudflared tunnel login`, `cloudflared tunnel create miniapp`,
   `cloudflared tunnel route dns miniapp app.example.com`; ingress `http://127.0.0.1:8080` ga yo'naltiriladi
   (namuna: `.cloudflared/config.yml.example`). `.env` da `WEBAPP_URL=https://app.example.com`.
5. Avtomatik ishga tushish (bitta, admin huquqisiz mexanizm — Startup papkasidagi yorliq):
   `python scripts/setup_autostart.py install --start`
   (eski Run-kalit / Task Scheduler / Docker-watchdog yozuvlarini ham olib tashlaydi; `uninstall` — teskarisi)
6. Quvvat sozlamalari (faqat tarmoqqa ulanganda uxlamaslik): `powershell -File scripts\configure_power_24_7.ps1`

Watchdog (`scripts/windows_keepalive_watchdog.py`) Windows'ni uyg'oq ushlaydi, `run.py` ni yashirin konsol jarayoni
sifatida boshqaradi va `cloudflared tunnel run <nom>` ni kuzatadi:

```powershell
python scripts\windows_keepalive_watchdog.py --status          # holat
python scripts\windows_keepalive_watchdog.py --reload          # faqat botni ohista qayta ishga tushirish
python scripts\windows_keepalive_watchdog.py --restart         # watchdog'ni qayta ishga tushirish (fon rejimida)
python scripts\windows_keepalive_watchdog.py --stop            # to'xtatish (bot va tunnel ham)
python scripts\windows_keepalive_watchdog.py --stop --force    # javob bermayotgan (eski) watchdog uchun
```

* Bot `CTRL_BREAK` bilan ohista to'xtatiladi, 30 soniyadan keyin jarayon daraxti majburan yopiladi.
* Qayta ishga tushirish faqat uzoq davom etgan nosozlikda: kamida 4 ta muvaffaqiyatsiz tekshiruv, 60+ soniya davomida,
  ishga tushgandan keyingi 240 soniyada emas. `degraded` (HTTP 200) qayta ishga tushirishga sabab bo'lmaydi.
* `telegram_channel_cloner` Docker konteyneri ishlayotgan bo'lsa, host bot ishga tushirilmaydi; host bot ishlayotganda
  konteyner paydo bo'lsa, u to'xtatiladi (`--keep-conflicting-container` — faqat log).
* Loglar: `logs/watchdog.log`, `logs/bot_stdout.log` (10 MB gacha), `logs/cloudflared.log`, `data/app.log`.

## 5. Yo'l B — VPS / Linux (Docker, `docker-bot` profili)

Kod serverga **git orqali** keladi (release tegi); maxfiy fayllar hech qachon avtomatik yuborilmaydi.

```bash
# serverda (Ubuntu/Debian yoki Oracle Linux, x86_64/ARM64)
git clone --branch v1.4.0 https://github.com/<owner>/<repo>.git ~/channelcloner
cd ~/channelcloner && cp .env.example .env && nano .env
sudo bash deploy/oracle_master_setup.sh --domain app.example.com --email admin@example.com
docker compose --profile docker-bot up -d --build telegram-cloner
curl -fsS http://127.0.0.1:8080/ready
```

`deploy/oracle_master_setup.sh` (idempotent): Docker + compose, nginx, certbot; firewall faqat 22/80/443 (8080 va 9000
tashqariga ochilmaydi, Docker soketi `chmod 666` qilinmaydi); nginx `127.0.0.1:8080` ga proksi qiladi; `--domain` bilan
certbot sertifikatni oladi va HTTPS (443) ni o'zi qo'shadi. Oracle Cloud'da VCN Security List'da ham 80/443 ni oching.
Ixtiyoriy: `--with-anti-reclaim` (Oracle Always Free: CPU/RAM/tarmoq chegarasini ushlab turadi, ataylab resurs sarflaydi),
`--with-webhook` (GitHub'da `v*` teg push qilinganda avtomatik deploy; `127.0.0.1:9000`, nginx orqali
`https://<domen>/deploy-webhook`, sirli kalit `/etc/channelcloner/webhook.env` da).

Yangilash (Windows'dan):

```powershell
.\deploy_one_click.ps1 -VpsIp 203.0.113.10 -User ubuntu -KeyPath $HOME\.ssh\oracle_arm_key -Ref v1.4.1
# birinchi marta: -Repo https://github.com/<owner>/<repo>.git -Bootstrap -AcceptNewHostKey -Domain ... -Email ...
```

Bu `scripts/deploy_remote.py` ni chaqiradi: SSH host kaliti tekshiriladi, serverda teg checkout qilinadi, konteyner qayta
yig'iladi va `/ready` kutiladi; har qanday xato — nol bo'lmagan exit code. SSH kalit: `ssh-keygen -t ed25519 -f ~/.ssh/oracle_arm_key`.

Mavjud bazani volume'ga ko'chirish (avval eski nusxani to'xtating, shunda WAL bazaga birlashadi):

```bash
docker compose --profile docker-bot run --rm --no-deps --user root -v "$PWD/database:/import:ro" \
  --entrypoint sh telegram-cloner -c "cp /import/cloner.db /import/.vault_key /app/data/ && chown appuser:appuser /app/data/cloner.db /app/data/.vault_key"
```

`docker compose up -d` (profilsiz) faqat edge xizmatlarini (`miniapp_web` nginx + `cloudflared`) ishga tushiradi — ular
Windows hostdagi bot uchun; `docker-bot` bilan birga kerak emas.

## 6. Yo'l C — Render

`render.yaml` Blueprint: `starter` (pullik) tarif, `/app/data` ga ulangan disk (disk bo'lganda Render eski instansiyani
yangisidan oldin to'xtatadi — ikki nusxa bo'lmaydi), `autoDeploy: false`, `healthCheckPath: /ready`, bitta instansiya.
Maxfiy qiymatlarni (`BOT_TOKEN`, `ENCRYPTION_KEY`, ...) panelda kiriting. Obraz `appuser` (uid 1000) bilan ishlaydi:
disk shu foydalanuvchi uchun yoziladigan bo'lishi kerak. `koyeb.yaml` — Koyeb uchun xuddi shu talablarning ma'lumotnomasi
(volume, `immediate` deploy strategiyasi, bitta instansiya).

## 7. Zaxira nusxa va tiklash

* Admin bot → "Baza Nusxasi": shifrlangan `.zip.enc` (Fernet). Ochish:
  `python scripts/decrypt_backup.py backup.zip.enc --extract` (kalit `.env`/vault faylidan yoki `--key`).
* Bazani tekshirish/tiklash (bot to'xtatilgan bo'lishi shart): `python scripts/repair_db.py` va `--rebuild`
  (avval SQLite backup API bilan to'liq nusxa oladi; qatorlar soni mos kelmasa almashtirmaydi).

## 8. Nosozliklar

* **`TelegramConflictError` / postlar takrorlanmoqda** — ikkinchi nusxa ishlayapti. Tekshiring:
  `python scripts\windows_keepalive_watchdog.py --status`, `docker ps -a` (eski `telegram_channel_cloner` konteynerini
  `docker compose --profile docker-bot down` yoki `docker rm -f telegram_channel_cloner` bilan olib tashlang).
* **`AUTH_KEY_DUPLICATED`** — sessiya ikki joyda ishlatilgan; bitta nusxa qoldirib, admin bot orqali qayta login qiling.
* **`/ready` 503** — baza yoki polling tayyor emas: `data/app.log` va `logs/bot_stdout.log` ni ko'ring.
