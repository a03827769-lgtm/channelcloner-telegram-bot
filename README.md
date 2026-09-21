# 🤖 Telegram Kanal & Guruh Kloner Boti va Ko'chmas Mulk Story Platformasi (Hybrid Telethon + Aiogram 3)

Ushbu platforma begona ochiq va yopiq Telegram kanallari hamda guruhlaridagi barcha turdagi xabarlarni real vaqtda ushlab, reklama va havolalardan tozalab, tarjima va shaxsiy imzo bilan o'zingizning maqsadli kanalingizga qayta joylaydi. Qo'shimcha ravishda, platformada **VIP Ko'chmas Mulk Story Kloni**, **Dedicated Admin Bot**, **Telegram Stars (XTR) to'lov tizimi** va **xavfsiz shifrlangan zaxira tizimi** mavjud.

---

## 🌟 Asosiy Imkoniyatlar

1. ⚡️ **Real-Vaqtda Klonlash (Real-Time MTProto Listener)**:
   - Manba kanalda yangi post e'lon qilinishi bilanoq soniyalar ichida ushlanadi va maqsadli kanalga uzatiladi.
2. 🎯 **Barcha Media Formatlarini 100% Qo'llab-quvvatlash**:
   - Oddiy matnli xabarlar va formatlangan HTML/Markdown postlar.
   - Rasmlar (Photos) va Videolar (HD formatda).
   - **Albomlar (Media-Group)**: 2 dan 10 tagacha rasm/videolarni bitta post sifatida tartibli birlashtirish.
   - **Ovozli xabarlar (Voice notes)** va **Dumaloq videolar (Video notes)** asl formatida.
   - Hujjatlar va fayllar (PDF, ZIP, APK, Word va h.k.).
   - Audio / Musiqa fayllari, Stikerlar va GIF animatsiyalar.
   - So'rovnomalar (Polls & Quizzes).
3. 🧹 **Aqlli Reklama va Havolalarni Tozalash (Smart Cleaner)**:
   - Manba kanaldagi begona `@username`, `t.me/...`, `https://...` havolalari avtomatik tozalanadi.
4. ✍️ **Shaxsiy Imzo va Suv Belgisi (Custom Watermark & Signatures)**:
   - Har bir yangi post ostiga o'z kanalingiz havolasi va imzosini avtomatik qo'shish imkoniyati.
5. 🚫 **Qora Ro'yxat (Blacklist)**:
   - Keraksiz kalit so'zlar (masalan: `reklama, 1xbet, aksiya`) qatnashgan xabarlarni filtrlash va o'tkazmaslik.
6. 🔄 **Tarixni Ko'chirish (History Backfill)**:
   - Manba kanaldagi eski postlarni (10, 30, 50, 100 ta yoki barchasini) tartib bilan ko'chirib olish.
7. 🏢 **VIP Ko'chmas Mulk Story Kloni (Real Estate Story Platform)**:
   - Ko'chmas mulk kanallaridagi e'lonlarni tahlil qilib, 1080x1920 (9:16) formatdagi Telegram Story vizual kartochkalarini va 25 soniyali 4K video-slayd-shoularini tayyorlaydi.
   - **Gibrid Render**: Yuqori resursli muhitda Playwright (Chromium headless) bilan 1:1 Telegram aniqligi, kam resursli (<=512MB RAM) konteynerlarda esa tezkor va yengil Pillow (PIL) dvigateli.
8. ⭐️ **Telegram Stars (XTR) Obuna va To'lovlar**:
   - In-app Telegram Stars orqali obuna sotib olish, idempotent charge ID tekshiruvi va avtomatik tariflarni faollashtirish.
9. 🛡 **Dedicated Admin Bot va Shifrlangan Zaxira (Security Vault)**:
   - Oddiy foydalanuvchilar oqimidan ajratilgan maxsus Administrator Boti (`ADMIN_BOT_TOKEN`).
   - Barcha MTProto sessiyalari AES-128 Fernet shifri ostida saqlanadi.
   - Admin bot orqali olinadigan ma'lumotlar bazasi zaxira nusxalari (`.zip.enc`) avtomatik shifrlanadi.

---

## 🏗 Arxitektura

Tizim modulli va ko'p qatlamli arxitekturaga asoslangan:
- **Telethon (MTProto Client)**: Begona kanallardan xabarlarni tutib olish va tarixni yuklab olish (`services/telethon_listener.py`, `services/multi_session_manager.py`).
- **Aiogram 3 (Bot API)**: Foydalanuvchi interfeysi, inline klaviaturalar va maqsadli kanallarga toza postlarni yuklash.
- **Dedicated Admin Bot**: Mustaqil router va `AdminStrictAuthMiddleware` orqali himoyalangan boshqaruv paneli (`admin_bot/`).
- **Aiosqlite (SQLite + WAL)**: 15 ta jadvaldan iborat yuqori yuklamaga moslashtirilgan asinxron ma'lumotlar bazasi (`database/db_manager.py`).
- **Story Renderer**: HTML/CSS shablonlari va Pillow orqali 4K ko'chmas mulk posterlari hamda FFmpeg video generatsiyasi.
- **Security Vault**: AES-128 Fernet master kaliti orqali sessiyalar va ma'lumotlar bazasi zaxiralarini himoyalash (`services/security_vault.py`).

---

## 🚀 O'rnatish va Ishga Tushirish

### 1. Muhitni tayyorlash
Tizimda **Python 3.10+** yoki **3.11** o'rnatilgan bo'lishi kerak.

```bash
# Kerakli kutubxonalarni o'rnatish
pip install -r requirements.txt
```

### 2. Sozlash Ustasini Ishga Tushirish (Setup Wizard)
Barcha kalitlarni tez va qulay sozlash uchun:

```bash
python setup_wizard.py
```

Ushbu interaktiv skript `.env` faylini quyidagi parametrlar bilan shakllantiradi:
- `BOT_TOKEN`: [@BotFather](https://t.me/BotFather) dan olingan asosiy bot tokeni.
- `ADMIN_BOT_TOKEN`: Administrator paneli uchun alohida bot tokeni (ixtiyoriy, lekin tavsiya etiladi).
- `TELEGRAM_API_ID` va `TELEGRAM_API_HASH`: [my.telegram.org](https://my.telegram.org) saytidan olingan API kalitlar.
- `ADMIN_IDS`: Bot administratorlarining Telegram ID raqamlari (vergul bilan).
- `ENCRYPTION_KEY`: Sessiyalar va zaxira nusxalarni shifrlash uchun master kalit (ixtiyoriy, berilmasa avtomatik generatsiya qilinadi).
- `ENABLE_PLAYWRIGHT`: `true` (standart) yoki `false` (512MB RAM Koyeb/Render uchun).

### 3. Botni Ishga Tushirish

```bash
python run.py
```

---

## 🔒 Xavfsizlik va Zaxira Nusxani Qayta Tiklash

Admin botdagi **"💾 Baza Nusxasi"** buyrug'i orqali olingan `.zip.enc` fayli Telegram tarmog'i orqali xavfsiz o'tishi uchun Fernet shifrlangan bo'ladi.

Zaxira nusxani o'z kompyuteringizda ochish (dekript qilish):

```bash
# Oddiy dekript (.zip hosil qiladi):
python scripts/decrypt_backup.py cloner_backup_20260918_120000.db.zip.enc

# Dekript qilib, ichidagi cloner.db ni to'g'ridan-to'g'ri chiqarib olish:
python scripts/decrypt_backup.py cloner_backup_20260918_120000.db.zip.enc --extract

# Maxsus kalit bilan ochish:
python scripts/decrypt_backup.py backup.zip.enc --key "maxfiy_shifrlash_kaliti"
```

---

## 🧪 Testlarni Ishga Tushirish

Loyihadagi 150+ birlik va integratsion testlarni tekshirish uchun:

```bash
python -m unittest discover tests
```

---

## 📂 Fayllar Strukturasi

```
channelcloner/
├── config/
│   ├── __init__.py
│   └── settings.py               # Pydantic sozlamalar va .env validatsiyasi
├── database/
│   ├── __init__.py
│   ├── models.py                 # Ma'lumotlar bazasi sxemalari (15 ta jadval)
│   └── db_manager.py             # Asinxron SQLite WAL menejeri
├── services/
│   ├── __init__.py
│   ├── cloner_engine.py          # Postlarni qayta ishlash va jo'natish yadrosi
│   ├── telethon_listener.py      # Telethon MTProto eshituvchisi
│   ├── multi_session_manager.py  # Ko'p foydalanuvchili MTProto sessiyalari
│   ├── text_processor.py         # Reklama tozalash, imzo va blacklist filtrlari
│   ├── media_handler.py          # Albomlar, rasmlar, videolar va fayllar boshqaruvi
│   ├── security_vault.py         # AES Fernet shifrlash va efemer muhit nazorati
│   ├── story_cloner_service.py   # Ko'chmas mulk e'lonlarini tahlil qilish va story generatsiyasi
│   └── story_renderer.py         # Playwright Chromium & Pillow gibrid 4K rendereri
├── bot/                          # Public Bot (Aiogram 3)
│   ├── bot_instance.py           # Bot va Dispatcher
│   ├── handlers/                 # /start, cloner, sozlamalar, to'lovlar (Stars)
│   ├── keyboards/                # Inline klaviaturalar
│   └── states/                   # FSM holatlari
├── admin_bot/                    # Dedicated Admin Bot (Aiogram 3)
│   ├── admin_bot_instance.py     # Admin bot kirish nuqtasi
│   ├── middlewares/              # Qat'iy avtorizatsiya filtri (AdminStrictAuthMiddleware)
│   └── handlers/                 # Statistika, foydalanuvchilar, broadcast, zaxira (backup)
├── deploy/                       # Deploy konfiguratsiyalari va skriptlar
│   ├── webhook/deploy_webhook.py # GitHub HMAC SHA-256 webhook auto-deployer
│   └── anti_reclaim.py           # VPS anti-reclaim yordamchisi
├── scripts/                      # Yordamchi ma'muriy skriptlar
│   ├── decrypt_backup.py         # Zaxira nusxani dekript qilish CLI dasturi
│   └── repair_db.py              # Baza diagnostikasi
├── tests/                        # 150+ ta avtomatlashtirilgan unit va integratsion testlar
├── Dockerfile                    # Ko'p bosqichli xavfsiz Docker konteyneri (jemalloc, non-root)
├── koyeb.yaml                    # Koyeb PaaS konfiguratsiyasi
├── setup_wizard.py               # Interaktiv o'rnatish ustasi
├── run.py                        # Asosiy ishga tushirish fayli
└── requirements.txt              # Python bog'liqliklari
```
