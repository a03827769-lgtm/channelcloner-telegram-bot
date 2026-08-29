# 🤖 Telegram Kanal & Guruh Kloner Boti (Hybrid Telethon + Aiogram 3)

Ushbu loyiha begona ochiq/yopiq Telegram kanallari va guruhlaridagi barcha turdagi yangiliklar, xabarlar va medialarni real vaqtda avtomatik ravishda o'zingizning shaxsiy kanalingizga toza, professional ko'rinishda nusxalab (klonlab) beruvchi Telegram Bot platformasidir.

---

## 🌟 Asosiy Imkoniyatlar

1. ⚡️ **Real-Vaqtda Klonlash (Real-Time Listener)**:
   - Manba kanalda yangi post e'lon qilinishi bilanoq soniyalar ichida sizning kanalingizga yetib boradi.
2. 🎯 **Barcha Media Formatlarini 100% Qo'llab-quvvatlash**:
   - Oddiy matnli xabarlar
   - Rasmlar (Photos) va Videolar (HD formatda)
   - **Albomlar (Media-Group)**: 2 dan 10 tagacha rasm/videolarni bitta post sifatida birlashtirib o'tkazish
   - **Ovozli xabarlar (Voice notes)** va **Dumaloq videolar (Video notes)** asl formatida
   - Hujjatlar va fayllar (PDF, ZIP, APK, Word va h.k.)
   - Audio / Musiqa fayllari
   - Stikerlar va GIF animatsiyalar
   - So'rovnomalar (Polls & Quizzes)
3. 🧹 **Aqlli Reklama va Havolalarni Tozalash (Smart Cleaner)**:
   - Manba kanaldagi begona `@username`, `t.me/...`, `https://...` havolalari avtomatik tozalanadi.
4. ✍️ **Shaxsiy Imzo / Suv Belgisi (Custom Watermark)**:
   - Har bir yangi post ostiga o'z kanalingiz havolasi va imzosini avtomatik qo'shish imkoniyati.
5. 🚫 **Qora Ro'yxat (Blacklist)**:
   - Keraksiz kalit so'zlar (masalan: `reklama, 1xbet, aksiya`) qatnashgan xabarlarni filtrlash va o'tkazmaslik.
6. 🔄 **Tarixni Ko'chirish (History Backfill)**:
   - Manba kanaldagi eski postlarni (10, 30, 50, 100 ta yoki barchasini) tartib bilan ko'chirib olish.
7. 🔀 **Ko'p Kanalli Boshqaruv (Multi-Channel Routing)**:
   - Bir nechta manba va maqsadli kanallar juftligini mustaqil ravishda ulash va alohida sozlash.

---

## 🏗 Arxitektura

Tizim **Gibrid MTProto + Bot API** tamoyili asosida ishlaydi:
- **Telethon (MTProto Client)**: Begona ochiq kanallarni kuzatish va xabarlarni tutib olish uchun Telegram akkaunt orqali ulanadi.
- **Aiogram 3 (Bot API)**: Foydalanuvchi interfeysi (`/start`, inline tugmalar, sozlamalar) va maqsadli kanalga postlarni toza yangi post ko'rinishida yuklash uchun xizmat qiladi.
- **SQLite (Aiosqlite)**: Kanallar juftligi, sozlamalar va takroriy xabarlarni oldini olish uchun yengil asinxron ma'lumotlar bazasi.

---

## 🚀 O'rnatish va Ishga Tushirish

### 1. Python muhitini tayyorlash
Tizimda **Python 3.10+** o'rnatilgan bo'lishi kerak.

```bash
# Kerakli kutubxonalarni o'rnatish
pip install -r requirements.txt
```

### 2. Sozlash Ustasini Ishga Tushirish (Setup Wizard)
Barcha kalitlarni (Bot Token, API ID, API Hash) oson sozlash uchun:

```bash
python setup_wizard.py
```

Ushbu interaktiv skript sizdan quyidagilarni so'raydi va `.env` faylini avtomatik yaratadi:
1. **BOT_TOKEN**: [@BotFather](https://t.me/BotFather) dan olingan bot tokeni.
2. **API_ID va API_HASH**: [my.telegram.org](https://my.telegram.org) saytidan olingan kalitlar.
3. **Telegram raqamingiz**: Begona kanallarni o'qish uchun MTProto sessiyasi ulanadi.

### 3. Botni Ishga Tushirish
```bash
python run.py
```

---

## 📱 Botdan Foydalanish Qo'llanmasi

1. **O'z kanalingizga botni administrator qiling**:
   - Kanalingiz sozlamalariga kiring (Channel Settings -> Administrators).
   - Botni qidirib topib, **"Post Messages" (Xabarlarni joylash)** ruxsatini bering.
2. **Botga `/start` buyrug'ini yuboring**:
   - **"🔄 Kanal Kloner"** bo'limiga kiring.
   - **"➕ Yangi kanal ulash"** tugmasini bosing.
   - 1-qadamda **Manba kanal** (masalan: `@kunuzofficial`) username yoki linkini yuboring.
   - 2-qadamda **O'zingizning kanalingiz** (masalan: `@mening_kanalim`) username yoki ID sini yuboring.
3. **Sozlamalarni moslang**:
   - **Link tozalash**: Yoqing yoki o'chiring.
   - **Shaxsiy imzo**: Xabarlar ostiga qo'yiladigan matnni kiriting.
   - **Qora ro'yxat**: Taqiqlangan so'zlarni kiriting.
   - **Tarixni ko'chirish**: Kerak bo'lsa eski postlarni ham ko'chiring.

---

## 🧪 Testlarni Ishga Tushirish

Loyihaning barcha birlik va integratsion testlarini tekshirish uchun:

```bash
python -m unittest discover tests
```

---

## 📂 Fayllar Strukturasi

```
channelcloner/
├── config/
│   ├── __init__.py
│   └── settings.py               # .env va tizim sozlamalari
├── database/
│   ├── __init__.py
│   ├── models.py                 # Ma'lumotlar modellari
│   └── db_manager.py             # Asinxron SQLite menejeri
├── services/
│   ├── __init__.py
│   ├── cloner_engine.py          # Klonlash va jo'natishning asosiy yadrosi
│   ├── telethon_listener.py      # Telethon MTProto eshituvchisi va tarix kloneri
│   ├── text_processor.py         # Reklama tozalash, imzo va blacklist filtrlari
│   └── media_handler.py          # Albomlar, rasmlar, videolar va fayllar boshqaruvi
├── bot/
│   ├── __init__.py
│   ├── bot_instance.py           # Aiogram Bot va Dispatcher yaratish
│   ├── handlers/
│   │   ├── start.py              # /start va asosiy menyu
│   │   ├── cloner_menu.py        # Kanal ulash va boshqarish menyusi
│   │   ├── settings_menu.py      # Imzo va blacklist sozlamalari
│   │   ├── history_clone.py      # Tarixni ko'chirish
│   │   └── help_guide.py         # Qo'llanma
│   ├── keyboards/
│   │   └── inline_buttons.py     # Inline tugmalar
│   └── states/
│       └── cloner_states.py      # FSM holatlari
├── tests/
│   ├── test_text_processor.py    # Matn filtrlari testlari
│   ├── test_db_manager.py        # Baza testlari
│   └── test_media_handler.py     # Media va albom testlari
├── setup_wizard.py               # Interaktiv sozlash ustasi
├── run.py                        # Asosiy ishga tushirish fayli
├── requirements.txt              # Kutubxonalar
├── .env.example                  # Sozlamalar namunasi
└── README.md                     # Hujjatlar
```
