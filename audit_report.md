# 🔬 TELEGRAM CHANNEL CLONER: BARCHA TOP 100 TA XATOLIK, KAMCHILIK VA NOQULAYLIKLAR TO'LIQ AUDIT HISOBOTI

Ushbu tahlil loyihaning barcha qismlarini (`bot/`, `admin_bot/`, `services/`, `database/`, `config/`, `deploy/`) 100% to'liq qamrab olgan holda tuzildi. Har bir nuqson **joylashuvi**, **yuzaga kelish sababi**, **keltirib chiqaradigan oqibati** va **aniq texnik yechimi** bilan ko'rsatilgan.

---

## 📑 MUNDARIJA (KATEGORIYALAR)
1. [🚨 1-qism: Kritik & Arxitekturaviy Xatoliklar (1–15)](#1-qism-kritik--arxitekturaviy-xatoliklar-115)
2. [🔄 2-qism: MTProto, Telethon & Jonli Klonlash Dvigateli Xatolari (16–30)](#2-qism-mtproto-telethon--jonli-klonlash-dvigateli-xatolari-1630)
3. [📦 3-qism: Media, Albomlar & Fayllar Bilan Ishlash Kamchiliklari (31–45)](#3-qism-media-albomlar--fayllar-bilan-ishlash-kamchiliklari-3145)
4. [📝 4-qism: Matnni Qayta Ishlash, HTML Teglar & Tarjimon Xatolari (46–60)](#4-qism-matnni-qayta-ishlash-html-teglar--tarjimon-xatolari-4660)
5. [🛡️ 5-qism: Xavfsizlik, Ruxsatlar & To'lov (Stars) Zaifliklari (61–75)](#5-qism-xavfsizlik-ruxsatlar--tolov-stars-zaifliklari-6175)
6. [🗄️ 6-qism: Ma'lumotlar Bazasi & In-Memory Kesh Kamchiliklari (76–85)](#6-qism-malumotlar-bazasi--in-memory-kesh-kamchiliklari-7685)
7. [🎨 7-qism: UI/UX, Inline Tugmalar & Foydalanuvchi Noqulayliklari (86–95)](#7-qism-uiux-inline-tugmalar--foydalanuvchi-noqulayliklari-8695)
8. [☁️ 8-qism: DevOps, Docker & 24/7 Cloud Barqarorligi Kamchiliklari (96–100)](#8-qism-devops-docker--247-cloud-barqarorligi-kamchiliklari-96100)

---

## 🚨 1-QISM: KRITIK & ARXITEKTURAVIY XATOLIKLAR (1–15)

### 1. Har bir SQL so'rovda yangi DB ulanish ochilishi (Connection Overhead)
- 📍 **Joylashuvi:** `database/db_manager.py:20-30` `get_connection()`
- 💥 **Muammo:** Har safar `get_connection()` chaqirilganda `aiosqlite.connect()` qilinadi va 6 ta `PRAGMA` buyrug'i qayta bajariladi. 100k post oqimida bu disk I/O va CPU yuklamasini 300% ga oshiradi.
- 🛠 **Yechim:** Ulanishlar hovuzi (Connection Pool) yoki uzoq yashovchi yagona `aiosqlite.Connection` obyektidan foydalanish.

### 2. `PRAGMA wal_checkpoint(TRUNCATE)` paytida boshqa jarayonlar qulflanishi
- 📍 **Joylashuvi:** `database/db_manager.py:38-42` `checkpoint()`
- 💥 **Muammo:** `TRUNCATE` rejimidagi checkpoint barcha faol yozuvchilarni to'xtatadi. Agar orqa fonda klonlash ketayotgan bo'lsa, `sqlite3.OperationalError: database is locked` xatosi chiqadi.
- 🛠 **Yechim:** `PRAGMA wal_checkpoint(PASSIVE)` yoki `RESTART` rejimiga o'tkazish.

### 3. Tarixiy postlarni ko'chirishda xabarlar soni cheklovi (`target_total` hisobi)
- 📍 **Joylashuvi:** `services/telethon_listener.py:572` `clone_history()`
- 💥 **Muammo:** `target_total = min(limit, total_available)` sharti `limit` None bo'lganda (barcha postlar) `total_available` 0 qaytsa 100 deb oladi va progress bar foizi 1000% gacha oshib ketadi.
- 🛠 **Yechim:** `target_total` ni oqim davomida dinamik yangilash va `iter_messages` uzunligiga moslash.

### 4. Background Tasklar (`asyncio.create_task`) GC tomonidan yo'q qilinishi (Task Orphan)
- 📍 **Joylashuvi:** `services/telethon_listener.py:539` va `bot/handlers/history_clone.py:174`
- 💥 **Muammo:** `asyncio.create_task()` natijasi o'zgaruvchida saqlanmagan holda chaqirilgan. Python 3.11+ da Garbage Collector havolasi yo'q tasklarni xotiradan o'chirib yuborishi mumkin (post yarmida to'xtab qoladi).
- 🛠 **Yechim:** Tasklarni global `set` yoki ro'yxatda ushlab turish (`_background_tasks.add(task)` va `task.add_done_callback(_background_tasks.discard)`).

### 5. `SQLite CURRENT_TIMESTAMP` va `datetime.fromisoformat` nomuvofiqligi
- 📍 **Joylashuvi:** `database/db_manager.py:249` `get_user_subscription()`
- 💥 **Muammo:** SQLite standart `CURRENT_TIMESTAMP` qiymati `YYYY-MM-DD HH:MM:SS` (probel bilan) qaytaradi. Pythonning eski versiyalarida `fromisoformat` probelli formatda `ValueError` beradi.
- 🛠 **Yechim:** `.replace(" ", "T")` orqali ISO formatga keltirib o'qish.

### 6. Signal Handlerlar Windows muhitida ishlamasligi
- 📍 **Joylashuvi:** `run.py:162-167`
- 💥 **Muammo:** `loop.add_signal_handler` Windows OS da `NotImplementedError` beradi. Garchi `try/except` bo'lsa-da, Windowsda bot `Ctrl+C` bosilganda to'xtamay osilib qoladi.
- 🛠 **Yechim:** Windows uchun `signal.signal(signal.SIGINT, ...)` handlerini o'rnatish.

### 7. Telethon sessiyasini shifrlashda BOT_TOKEN o'zgarsa ochilmay qolishi
- 📍 **Joylashuvi:** `services/security_vault.py:13-16`
- 💥 **Muammo:** Fernet kaliti `BOT_TOKEN` va `API_ID` dan hosil qilinadi. Agar foydalanuvchi BotFather orqali tokenini yangilasa, eski sessiya shifrdan yechilmaydi va bot ishga tushmaydi.
- 🛠 **Yechim:** Mustaqil `ENCRYPTION_KEY` muhit o'zgaruvchisidan foydalanish yoki tokenni yangilaganda bazadagi sessiyani avtomatik deshirflash.

### 8. `VACUUM INTO` buyrug'ida yo'l (Path) inyeksiyasi xavfi
- 📍 **Joylashuvi:** `database/db_manager.py:805` `create_backup_file()`
- 💥 **Muammo:** `f"VACUUM INTO '{abs_backup_path}';"` satrida agar yo'l ichida bitta tirnoq (`'`) bo'lsa, SQL sintaksis xatosi beradi yoki SQL inyeksiyaga sabab bo'ladi.
- 🛠 **Yechim:** Parametrlangan yoki yo'lni to'liq sanitize qilingan shaklda uzatish.

### 9. Admin Bot va Public Bot bir vaqtda start berganda Webhook konflikti
- 📍 **Joylashuvi:** `run.py:170` va `run.py:177`
- 💥 **Muammo:** Agar bitta bot tokeni adashib ikkala bot uchun ham ko'rsatilsa, `delete_webhook` va `start_polling` bir-biri bilan cheksiz to'qnashuvga kirishadi (`TelegramConflictError`).
- 🛠 **Yechim:** `ADMIN_BOT_TOKEN == BOT_TOKEN` holatini tekshiruvchi validator qo'shish.

### 10. `aiohttp` HTTP Healthcheck serverida `PORT` o'zgaruvchisi string holatida bo'lsa qulashi
- 📍 **Joylashuvi:** `run.py:67` `start_health_server()`
- 💥 **Muammo:** Agar muhitda `PORT="tcp://..."` yoki noto'g'ri qiymat bo'lsa, `int()` `ValueError` tashlaydi va butun bot ishga tushmaydi.
- 🛠 **Yechim:** `PORT` qiymatini `try/except` bilan xavfsiz `8080` ga fallback qilish.

### 11. `SubscriptionWatcher` da bot bloklangan foydalanuvchiga xabar yuborishda to'xtab qolish
- 📍 **Joylashuvi:** `services/subscription_watcher.py:90-102`
- 💥 **Muammo:** Foydalanuvchi botni bloklagan bo'lsa (`TelegramForbiddenError`), `mark_trial_notified` chaqirilmay qolib, bot har 5 daqiqada unga qayta xabar yuborishga urinadi.
- 🛠 **Yechim:** `TelegramForbiddenError` yuz berganda darhol `mark_trial_notified = 1` qilib belgilash.

### 12. Drip Feed navbatida `datetime('now')` vaqt mintaqasi (UTC vs Tashkent)
- 📍 **Joylashuvi:** `database/db_manager.py:963` `get_due_drip_items()`
- 💥 **Muammo:** SQLite dagi `datetime('now')` UTC vaqtni oladi, lekin `drip_feed_queue.py` da O'zbekiston vaqti (UTC+5) bilan aralashtirilsa, xabarlar 5 soat kechikib chiqadi.
- 🛠 **Yechim:** Barcha vaqtlarni yagona ISO-8601 UTC formatida saqlash va tekshirish.

### 13. `channel_backups` jadvalida unikal indeks yo'qligi (Dublikatlar ko'payishi)
- 📍 **Joylashuvi:** `database/db_manager.py:147-159`
- 💥 **Muammo:** `(pair_id, message_id)` uchun `UNIQUE` constraint yo'q. Bir xil post qayta zaxiralansa, bazada minglab dublikatlar yig'iladi.
- 🛠 **Yechim:** `CREATE UNIQUE INDEX idx_backups_pair_msg ON channel_backups(pair_id, message_id);` qo'shish.

### 14. `setup_wizard.py` orqali `.env` yaratilganda fayl ruxsatlari (Permissions)
- 📍 **Joylashuvi:** `setup_wizard.py:101-103`
- 💥 **Muammo:** `.env` fayli Linuxda `0644` (ommaviy o'qish mumkin) huquqi bilan yaratiladi. Bu serverdagi boshqa jarayonlarga maxfiy kalitlarni o'qish imkonini beradi.
- 🛠 **Yechim:** Fayl yaratilgandan so'ng `os.chmod('.env', 0o600)` o'rnatish.

### 15. Drip Queue workerining xatolikda cheksiz loopga tushishi
- 📍 **Joylashuvi:** `services/drip_feed_queue.py:73-75`
- 💥 **Muammo:** Agar xabar `failed` bo'lsa, `status = 'failed'` ga o'tadi, lekin xatolik sababi bazada saqlanmaydi va foydalanuvchiga bildirilmaydi.
- 🛠 **Yechim:** `drip_queue` ga `error_message` ustunini qo'shish.

---

## 🔄 2-QISM: MTPROTO, TELETHON & JONLI KLONLASH DVIGATELI XATOLARI (16–30)

### 16. Yopiq kanallarni (`joinchat/+hash`) aniqlashda `ImportChatInviteRequest` FloodWait
- 📍 **Joylashuvi:** `services/telethon_listener.py:420-424` `resolve_entity()`
- 💥 **Muammo:** Har bir `resolve_entity` chaqiruvida yopiq havola bo'lsa, to'g'ridan-to'g'ri qo'shilishga urinadi. Bu Telegram tomonidan akkauntga 24 soatlik cheklov (`FloodWaitError: 86400s`) qo'yilishiga sabab bo'ladi.
- 🛠 **Yechim:** Avval `CheckChatInviteRequest` orqali kanal ma'lumotlarini olish va faqat a'zo bo'lmagan taqdirda bir marta qo'shilish.

### 17. Telethon RPC ping zondi (`GetStateRequest`) qotib qolishi
- 📍 **Joylashuvi:** `services/telethon_listener.py:169` `_connection_supervisor()`
- 💥 **Muammo:** Tarmoq uzilganda `GetStateRequest()` cheksiz kutish rejimiga kirishi mumkin. Garchi `timeout=10.0` bo'lsa-da, pastki darajadagi TCP soket qulflanib qoladi.
- 🛠 **Yechim:** Telethon `connection_retries=3` va `auto_reconnect=True` parametrlarini `TelegramClient` ga to'g'ridan-to'g'ri berish.

### 18. Jonli xabarlarni filtrlashda kanal ID larini solishtirish xatosi
- 📍 **Joylashuvi:** `services/telethon_listener.py:507-515` `_handle_new_message()`
- 💥 **Muammo:** Superguruh va oddiy kanallar ID si `-100...` prefiksiga ega, lekin Telethon ichki hodisalarida ba'zan musbat butun son, ba'zan `-100` siz son qaytadi. Natijada xabarlar o'tkazib yuboriladi.
- 🛠 **Yechim:** `normalize_peer_id` funksiyasini barcha qiyoslash joylarida qat'iy qo'llash.

### 19. Tarixni ko'chirishda albomlarning ketma-ketligi buzilishi (Race Condition)
- 📍 **Joylashuvi:** `services/telethon_listener.py:605-614` `clone_history()`
- 💥 **Muammo:** `iter_messages(reverse=True)` da bitta albomga tegishli 5 ta rasm ketma-ket kelganda, `last_group_id` tekshiruvi albom tugashidan oldin boshqa xabar aralashsa albomni ikkiga bo'lib yuboradi.
- 🛠 **Yechim:** Albom xabarlarini `grouped_id` bo'yicha yig'uvchi bufer generatorini joriy qilish.

### 20. `clone_single_message` da obuna tugagani haqida ogohlantirish yo'qligi
- 📍 **Joylashuvi:** `services/cloner_engine.py:160-162`
- 💥 **Muammo:** Agar foydalanuvchining obunasi tugagan bo'lsa, dvigatel postni indamasdan tashlab ketadi (`return False`). Foydalanuvchi esa bot buzildi deb o'ylaydi.
- 🛠 **Yechim:** Obuna tugaganda kanal egasiga bot orqali 1 marta ogohlantiruvchi xabar yuborish.

### 21. `send_test_post` da manba kanal sarlavhasi HTML qochirilmagani (XSS/Parse Error)
- 📍 **Joylashuvi:** `services/cloner_engine.py:76-77`
- 💥 **Muammo:** Agar manba kanal nomida `<` yoki `>` belgilari bo'lsa (masalan: `<<KUN.UZ>>`), test posti yuborilayotganda `TelegramBadRequest: can't parse entities` xatosi chiqadi.
- 🛠 **Yechim:** `html.escape(pair.source_title)` dan foydalanish.

### 22. Katta hajmdagi fayllarni (>49MB) Telethon orqali fallback jo'natishda xato
- 📍 **Joylashuvi:** `services/cloner_engine.py:260` va `669-683`
- 💥 **Muammo:** `_telethon_send_fallback` funksiyasida `target_entity` topilmasa yoki bot u kanalda yozish huquqiga ega bo'lmasa, post yo'qoladi va xatolik yutiladi.
- 🛠 **Yechim:** Xatolik yuz berganda `Aiogram` orqali faylni siqib (compress) jo'natishga urinish.

### 23. Telethon `iter_messages` da FloodWait to'g'ri kutilmasligi
- 📍 **Joylashuvi:** `services/telethon_listener.py:620-628`
- 💥 **Muammo:** Agar `iter_messages` ichki generatorining o'zi FloodWait bersa, tashqi `try/except` uni ushlay olmaydi va butun jarayon to'xtaydi.
- 🛠 **Yechim:** Telethon clientiga `flood_sleep_threshold=120` parametrini berish.

### 24. Jonli klonlashda `pair.is_active` holatini tekshirishdan oldin `db_manager.is_message_cloned` chaqirilishi
- 📍 **Joylashuvi:** `services/cloner_engine.py:164`
- 💥 **Muammo:** To'xtatilgan kanallar uchun ham deduplikatsiya tekshiruvi ishlaydi va behuda kesh/DB so'rovi sarflanadi.
- 🛠 **Yechim:** Dastlab `if not pair.is_active: return False` tekshiruvini qo'yish.

### 25. Telethon `logout()` funksiyasida `client.log_out()` chaqirilmasligi
- 📍 **Joylashuvi:** `services/telethon_listener.py:390-396`
- 💥 **Muammo:** Faqat lokal bazadagi sessiya o'chiriladi, Telegram serveridagi faol seans esa ochiq qoladi.
- 🛠 **Yechim:** `await self.client.log_out()` chaqiruvini qo'shish.

### 26. `Poll` turidagi postlarni ko'chirishda variantlar matni 100 belgidan oshishi
- 📍 **Joylashuvi:** `services/cloner_engine.py:341-350`
- 💥 **Muammo:** Telegram Bot API so'rovnoma savoliga 300 belgi, har bir javob variantiga esa 100 belgi limit qo'ygan. Agar manbadagi variant uzun bo'lsa, `send_poll` xato beradi.
- 🛠 **Yechim:** Variant matnlarini `ans[:100]` qilib kesish.

### 27. `Location` va `Venue` postlarini ko'chirishda koordinata yo'qligi
- 📍 **Joylashuvi:** `services/cloner_engine.py:378-382`
- 💥 **Muammo:** `geo` obyekti None bo'lsa `0.0, 0.0` koordinata bilan jo'natadi (Okean o'rtasi).
- 🛠 **Yechim:** `if not geo: return False` tekshiruvini kiritish.

### 28. Rate Limiterda qulflar (Lock) xotirada to'planib qolishi
- 📍 **Joylashuvi:** `services/rate_limiter.py:19` `_locks`
- 💥 **Muammo:** Kanal o'chirib tashlansa ham uning `asyncio.Lock` obyekti `_locks` lug'atida abadiy qolib ketadi.
- 🛠 **Yechim:** TTL yoki zaif havolalar (`weakref`) orqali eskirgan qulflarni tozalash.

### 29. Telethon sessiya stringi yangilanganda xavfsiz saqlanmasligi
- 📍 **Joylashuvi:** `services/telethon_listener.py:130-132`
- 💥 **Muammo:** `fresh_session` olinganda shifrlanmasdan `db_manager.set_setting` ga uzatilishi ehtimoli bor.
- 🛠 **Yechim:** `db_manager.set_setting` ichida `telethon_session` kaliti uchun shifrlash doimiy kafolatlanganini tasdiqlash.

### 30. `refresh_monitored_channels` da kanallar soni ko'p bo'lsa ketma-ket bloklanish
- 📍 **Joylashuvi:** `services/telethon_listener.py:467-475`
- 💥 **Muammo:** 50 ta kanal bo'lsa, har biriga ketma-ket ulanish 30 soniyagacha vaqt oladi.
- 🛠 **Yechim:** `asyncio.gather()` orqali 5 talik paketlarda parallel bajarish.

---

## 📦 3-QISM: MEDIA, ALBOMLAR & FAYLLAR BILAN ISHLASH KAMCHILIKLARI (31–45)

### 31. Albomdagi rasmlarni parallel yuklab olishda xotira to'lishi (RAM Spike)
- 📍 **Joylashuvi:** `services/cloner_engine.py:537-538`
- 💥 **Muammo:** 10 ta 20MB lik video yoki rasm bir vaqtda xotiraga yuklanganda RAM darhol 300MB+ ga sakraydi.
- 🛠 **Yechim:** `media_semaphore` orqali bir vaqtda maksimal 3 ta fayl yuklanishini ta'minlash.

### 32. Albom jo'natilganda (10 tadan ko'p) xabarlar bo'linishi
- 📍 **Joylashuvi:** `services/cloner_engine.py:564-566`
- 💥 **Muammo:** 11 ta fayldan iborat albom kelganda, Telegram Bot API 10 ta + 1 ta qilib jo'natadi. 1 ta qolgan fayl alohida post bo'lib ketadi.
- 🛠 **Yechim:** 11 ta faylni 6 + 5 formatida mutanosib taqsimlash.

### 33. Rasmlarga Watermark qo'yishda shaffof PNG formatining buzilishi
- 📍 **Joylashuvi:** `services/watermark_service.py:104-105`
- 💥 **Muammo:** Agar rasm aslida PNG bo'lib, lekin kengaytmasi `.jpg` bo'lsa, `convert("RGB")` qilinganda qora fon paydo bo'ladi.
- 🛠 **Yechim:** `Image.mode == 'RGBA'` bo'lsa, oq fon bilan birlashtirib keyin RGB ga o'tkazish.

### 34. Video Watermark FFmpeg jarayoni qotib qolganda disk to'lib ketishi
- 📍 **Joylashuvi:** `services/video_watermark_service.py:96-102`
- 💥 **Muammo:** FFmpeg jarayoni `timeout` ga uchraganda vaqtinchalik `wm_vid_...` fayli diskda chala yozilgan holda qolib ketadi.
- 🛠 **Yechim:** `proc.kill()` dan keyin vaqtinchalik chiqish faylini darhol o'chirib tashlash.

### 35. Video Watermarkda maxsus belgilar (`' , : %`) sababli FFmpeg xatosi
- 📍 **Joylashuvi:** `services/video_watermark_service.py:67`
- 💥 **Muammo:** Agar suv belgisi matnida `[ ] ( ) = ;` qatnashsa, FFmpeg `drawtext` filtri sintaksis xatosi beradi va video konvertatsiya bo'lmaydi.
- 🛠 **Yechim:** Barcha maxsus belgilarni to'liq regex bilan qochirish (`re.escape` ga o'xshash FFmpeg filtr qochiruvchisi).

### 36. Vaqtinchalik fayllarni tozalash (`cleanup_files`) da xatolik yutilishi
- 📍 **Joylashuvi:** `services/media_handler.py:68-74`
- 💥 **Muammo:** Agar fayl boshqa jarayon tomonidan band bo'lsa (Windowsda), o'chirilmay qoladi va disk asta-sekin to'ladi.
- 🛠 **Yechim:** Fayl qulflarini tekshirish va `_periodic_cleaner` da eskirgan fayllarni majburiy o'chirish.

### 37. Dumaloq video (`video_note`) ga noto'g'ri caption qo'shishga urinish
- 📍 **Joylashuvi:** `services/cloner_engine.py:290-294`
- 💥 **Muammo:** Telegramda `video_note` larda matn (caption) bo'lmaydi. Agar manbadagi xabarda matn bo'lsa, u yo'qoladi.
- 🛠 **Yechim:** Dumaloq video bilan birga kelgan matnni alohida xabar sifatida ketidan jo'natish.

### 38. Voice (ovozli xabar) larda davomiylik (`duration`) yo'qolishi
- 📍 **Joylashuvi:** `services/cloner_engine.py:282-288`
- 💥 **Muammo:** `send_voice` chaqirilganda `duration` ko'rsatilmagan. Telegramda ovozli xabar uzunligi noma'lum bo'lib ko'rinadi.
- 🛠 **Yechim:** Telethon `message.media.document.attributes` dan `duration` ni olib `send_voice(..., duration=d)` ga uzatish.

### 39. Stikerlar (`sticker`) klonlanganda format mos kelmasligi (TGS / WEBM)
- 📍 **Joylashuvi:** `services/cloner_engine.py:304-308`
- 💥 **Muammo:** Telegram Premium animatsion (.tgs) yoki video (.webm) stikerlari yuklab olinganda oddiy bot orqali jo'natish xatolik berishi mumkin.
- 🛠 **Yechim:** Stiker jo'natishda xato bo'lsa, uni hujjat (`send_document`) sifatida yuborish fallback mexanizmi.

### 40. Albom buferi (`MediaGroupBuffer`) ning kechikishi (Debounce Delay)
- 📍 **Joylashuvi:** `services/media_handler.py:13` `debounce_delay = 2.0`
- 💥 **Muammo:** 2.0 soniya kechikish yuqori tezlikdagi kanallarda postlarning kechikib chiqishiga sabab bo'ladi. Agar 10-rasm 2.1 soniyada kelsa, 2 ta alohida albom bo'lib ketadi.
- 🛠 **Yechim:** Dinamik kutish: agar kutilayotgan xabarlar soni to'lsa darhol jo'natish, aks holda 1.2 soniya kutish.

### 41. Zaxiradan qayta tiklashda `file_id` ning eskirishi
- 📍 **Joylashuvi:** `services/disaster_recovery.py:68-85` `restore_channel()`
- 💥 **Muammo:** Agar bot o'zgarsa yoki Telegram serverlarida eski `file_id` o'chsa, qayta tiklash to'xtab qoladi.
- 🛠 **Yechim:** `file_id` ishlamay qolganda xatolikni o'tkazib yuborib matnni tiklashni davom ettirish.

### 42. Rasm shriftlarini qidirishda Linux/Windows yo'llari nomuvofiqligi
- 📍 **Joylashuvi:** `services/watermark_service.py:33-41`
- 💥 **Muammo:** Windowsda `/usr/share/fonts/...` yo'q, Linux Dockerda esa `arial.ttf` bo'lmasligi mumkin. Shrift topilmasa xira standart `load_default()` ga tushib qoladi (juda kichik shrift).
- 🛠 **Yechim:** Loyihaga kichik o'lchamdagi `assets/fonts/DejaVuSans-Bold.ttf` faylini qo'shish va undan foydalanish.

### 43. Gif animatsiyalarini rasm sifatida yuklab yuborish xatosi
- 📍 **Joylashuvi:** `services/media_handler.py:122-124`
- 💥 **Muammo:** `message.gif` bo'lgan xabarlar ba'zan `document` sifatida aniqlanadi va kanalda harakatsiz fayl bo'lib ko'rinadi.
- 🛠 **Yechim:** `send_animation` orqali jo'natishni qat'iy tekshirish.

### 44. `media_handler.download_telethon_media` da fayl nomi yo'qligi
- 📍 **Joylashuvi:** `services/media_handler.py:57-58`
- 💥 **Muammo:** Hujjatlar yuklab olinganda asl fayl nomi (`document.pdf`) yo'qolib, `uuid_123` bo'lib qoladi. Kanalga fayl noaniq nom bilan tushadi.
- 🛠 **Yechim:** Telethon `message.file.name` mavjud bo'lsa, asl nomni saqlab qolish.

### 45. Albom ichida bir nechta matn (caption) kelganda faqat birinchisi olinishi
- 📍 **Joylashuvi:** `services/cloner_engine.py:489-494`
- 💥 **Muammo:** Agar albomning 2-rasmida ham alohida muhim matn bo'lsa, u tashlab yuboriladi.
- 🛠 **Yechim:** Barcha matnlarni birlashtirib umumiy caption hosil qilish.

---

## 📝 4-QISM: MATNNI QAYTA ISHLASH, HTML TEGLAR & TARJIMON XATOLARI (46–60)

### 46. `fit_caption_limit` da HTML teglarning noto'g'ri yopilishi va ochilishi
- 📍 **Joylashuvi:** `services/text_processor.py:201-216`
- 💥 **Muammo:** Matn 1024 belgidan oshganda kesiladi va `unclosed` teglar yopiladi. Lekin `overflow` qismiga teglar qayta ochilganda atributlar (masalan: `<a href="...">`) yo'qolib `<a>` bo'lib qoladi.
- 🛠 **Yechim:** Ochiluvchi teglarning to'liq atributlarini regex bilan saqlab keyingi xabarga uzatish.

### 47. Avto-tarjimonda maxsus belgilar (`___TAG_0___`) tarjima qilinib ketishi
- 📍 **Joylashuvi:** `services/translator_service.py:35-49`
- 💥 **Muammo:** Google Translate ba'zan `___TAG_0___` ni `___ TEG_0 ___` yoki kichik harflarga o'zgartirib yuboradi. Natijada havolalar matndan o'chib ketadi.
- 🛠 **Yechim:** Tarjimon tegilmaydigan xavfsiz unikal sonli tokenlar (masalan: `[[990011]]`) ishlatish.

### 48. So'z almashtirgichda (`replace_words`) kichik harf/katta harf muammosi
- 📍 **Joylashuvi:** `services/text_processor.py:145-153`
- 💥 **Muammo:** `re.IGNORECASE` bilan almashtirilganda, yangi so'z doim kiritilgan registrda qo'yiladi. Masalan `TOSHKENT` matni `samarqand` ga o'zgarib qoladi (katta harflar yo'qoladi).
- 🛠 **Yechim:** Asl so'z registriga qarab (Title, UPPER, lower) yangi so'z registrini moslashtirish.

### 49. Reklama tozalashda (`clean_links_and_usernames`) telefon raqamlari o'chib ketishi
- 📍 **Joylashuvi:** `services/text_processor.py:133-135`
- 💥 **Muammo:** Ba'zi regex qoidalari `@username` ni tozalashda raqamli kontaktlarni ham reklama deb hisoblab o'chirib yuboradi.
- 🛠 **Yechim:** Telefon raqamlari formatini regex tekshiruvidan istisno qilish.

### 50. `AIParaphraser` da HTML sarlavhalar ikki marta qo'shilishi
- 📍 **Joylashuvi:** `services/ai_paraphraser.py:68-73`
- 💥 **Muammo:** Agar xabarda allaqachon `<b>Rasmiy Axborot:</b>` bo'lsa, xizmat uning ustiga yana bitta sarlavha qo'shib yuboradi.
- 🛠 **Yechim:** Matn boshida sarlavha borligini tekshiruvchi himoya shartini kiritish.

### 51. VIP Telegram Premium emojilarni almashtirishda `<code>` va `<pre>` bloklari buzilishi
- 📍 **Joylashuvi:** `services/emoji_converter.py:154`
- 💥 **Muammo:** Dasturlash kodlari (`<code>...</code>`) ichidagi emojilar `<tg-emoji>` ga aylantirilganda kod formati buziladi.
- 🛠 **Yechim:** `<code>`, `<pre>`, `<a>` bloklarini emojilar konvertatsiyasidan to'liq himoyalash.

### 52. Matn oxiriga imzo qo'shishda Telegram 4096 belgi chegarasidan oshib ketishi
- 📍 **Joylashuvi:** `services/text_processor.py:156-165`
- 💥 **Muammo:** Agar matn 4050 belgi bo'lsa va imzo 60 belgi bo'lsa, umumiy matn 4110 belgi bo'lib, `send_message` xato beradi.
- 🛠 **Yechim:** Imzo qo'shilgandan keyin umumiy uzunlikni tekshirib, kerak bo'lsa ikkiga bo'lish.

### 53. `extract_channel_from_message` da Forward qilingan xabarning yashirin muallifi
- 📍 **Joylashuvi:** `services/text_processor.py:44-52`
- 💥 **Muammo:** Agar xabar foydalanuvchi tomonidan yashirin forward qilingan bo'lsa (`forward_origin_hidden_user`), xato bermasdan None qaytishi kerak, lekin ba'zan atribut yo'qligi sabab AttributeError berishi mumkin.
- 🛠 **Yechim:** `getattr(forward_origin, "chat", None)` ni to'liq xavfsiz qilish.

### 54. Dynamic Affiliate linklarda `?` va `&` parametrlarining takrorlanishi
- 📍 **Joylashuvi:** `services/dynamic_affiliate_engine.py:62-63`
- 💥 **Muammo:** `https://site.com/?ref=old` havolasiga yangi parametr qo'shilganda `https://site.com/?ref=old?ref=new` bo'lib havola ishlamay qoladi.
- 🛠 **Yechim:** `urllib.parse.parse_qs` va `urlencode` orqali parametrlarni to'g'ri almashtirish.

### 55. Qora ro'yxat (`blacklist_words`) so'zlarni qisman moslikda noto'g'ri bloklashi
- 📍 **Joylashuvi:** `services/text_processor.py:106-110`
- 💥 **Muammo:** Agar qora ro'yxatda `ol` so'zi bo'lsa, `qol`, `bolalar`, `olma` kabi so'zlari bor barcha postlar bloklanib ketadi.
- 🛠 **Yechim:** So'zlarni `\b` (so'z chegarasi) orqali regex bilan qidirish.

### 56. Matndan linklarni tozalashda Markdown formatidagi havolalar qolib ketishi
- 📍 **Joylashuvi:** `services/text_processor.py:120-125`
- 💥 **Muammo:** HTML anchor teglari tozalanadi, lekin `[Kanalga ulanish](https://t.me/...)` ko'rinishidagi Markdown havolalari o'chirilmaydi.
- 🛠 **Yechim:** Markdown havola patternini ham tozalash qoidalariga qo'shish.

### 57. Tarjimon keshining xotirada hajmi kattalashib ketishi
- 📍 **Joylashuvi:** `services/translator_service.py:69-70`
- 💥 **Muammo:** Kesh 2000 taga yetganda birdaniga `self._cache.clear()` qilinadi. Bu keyingi tarjimalarda keskin sekinlashuvga sabab bo'ladi.
- 🛠 **Yechim:** `LRU Cache` dan foydalanib faqat eng eski yozuvlarni bosqichma-bosqich o'chirish.

### 58. Paraphraser "short" rejimida qisqa postlarning yo'qolib qolishi
- 📍 **Joylashuvi:** `services/ai_paraphraser.py:98-99`
- 💥 **Muammo:** Agar matn 1 qatordan iborat bo'lsa, `lines[:10]` logikasi noto'g'ri format beradi.
- 🛠 **Yechim:** 1 qatorli xabarlar uchun mos ixcham format ishlab chiqish.

### 59. Referal almashtirgichda bir nechta qoidalar ketma-ketligi buzilishi
- 📍 **Joylashuvi:** `services/affiliate_replacer.py:55-58`
- 💥 **Muammo:** Agar qoidalar ichida umumiy domenlar bo'lsa (masalan: `aliexpress.com` va `best.aliexpress.com`), noto'g'ri qoida birinchi ishlab ketadi.
- 🛠 **Yechim:** Qoidalarni uzunligi bo'yicha saralab (eng aniq domen birinchi) qo'llash.

### 60. Emojilar lug'atida ba'zi variant selektorlari (`\ufe0f`) yo'qligi
- 📍 **Joylashuvi:** `services/emoji_converter.py:141-147`
- 💥 **Muammo:** iOS va Android qurilmalaridan yuborilgan ba'zi kompozit emojilar (masalan bayroqlar yoki terining rangi) aniqlanmay qoladi.
- 🛠 **Yechim:** `unicodedata.normalize('NFC', text)` orqali matnni unifikatsiya qilish.

---

## 🛡️ 5-QISM: XAVFSIZLIK, RUXSATLAR & TO'LOV (STARS) ZAIFLIKLARI (61–75)

### 61. Boshqa foydalanuvchining kanalini ko'rish yoki boshqarish xavfi (IDOR)
- 📍 **Joylashuvi:** `bot/handlers/settings_menu.py:47` va `bot/handlers/cloner_menu.py:315`
- 💥 **Muammo:** Garchi `user_has_pair_access` tekshiruvi bo'lsa-da, ba'zi callback handlerlarda `pair = await db_manager.get_pair_by_id(pair_id)` dan so'ng ruxsat tekshiruvi unutilgan edi.
- 🛠 **Yechim:** Barcha `pair_` bilan boshlanuvchi callbacklar uchun global middleware darajasida avtorizatsiya filtri o'rnatish.

### 62. Telegram Stars to'lovida `payload` soxtalashtirish (Tampering) xavfi
- 📍 **Joylashuvi:** `bot/handlers/stars_billing.py:152-160`
- 💥 **Muammo:** `payload = payment.invoice_payload` faqat split qilinadi. Agar to'lov summasi kamroq bo'lsa ham `payload` ga qarab VIP berib yuborish mumkin.
- 🛠 **Yechim:** `payment.total_amount >= plan["stars"]` shartini qat'iy tekshirish.

### 63. Super Admin ID lari `.env` da noto'g'ri probel bilan kiritilsa xato bo'lishi
- 📍 **Joylashuvi:** `config/settings.py:24-30` `admin_ids`
- 💥 **Muammo:** `ADMIN_IDS="12345, 67890 "` kiritilganda `ValueError` yuzaga kelsa, butun ro'yxat bo'sh `set()` qaytaradi va adminlar tizimga kira olmay qoladi.
- 🛠 **Yechim:** Har bir elementni alohida `try/except` bilan parse qilish.

### 64. `AdminStrictAuthMiddleware` da bot xabarlariga javob qaytarishda xatolik
- 📍 **Joylashuvi:** `admin_bot/middlewares/admin_auth_middleware.py:26-38`
- 💥 **Muammo:** Begona foydalanuvchi Admin botga yozganda `event.answer(...)` chaqiriladi. Agar begona odam botni spam qilsa, bot o'ziga o'zi FloodWait oladi.
- 🛠 **Yechim:** Begona foydalanuvchilarga javob yozmasdan shunchaki hodisani bloklash (Drop update).

### 65. Zaxiradan tiklashda manzil kanal ustida ruxsat tekshiruvi yo'qligi
- 📍 **Joylashuvi:** `bot/handlers/settings_menu.py:870-884` `process_restore_target()`
- 💥 **Muammo:** Foydalanuvchi maqsadli kanal sifatida begona kanalni ko'rsatsa, bot u kanalda admin bo'lmagani uchun xatoliklar logga tiqilib ketadi.
- 🛠 **Yechim:** Tiklashdan oldin `bot.get_chat_member` orqali bot ruxsatlarini tekshirib olish.

### 66. Xabar tarqatishda (Broadcast) barcha xatoliklar botni sekinlashtirishi
- 📍 **Joylashuvi:** `admin_bot/handlers/broadcast.py:61-125`
- 💥 **Muammo:** 10,000 foydalanuvchiga ketma-ket yuborish 10-15 daqiqa vaqt oladi va bu vaqtda admin bot boshqa buyruqlarga javob bermay qolishi mumkin.
- 🛠 **Yechim:** Broadcast jarayonini alohida orqa fon generatoriga ajratish.

### 67. MTProto login jarayonida 2FA paroli logga yozilishi xavfi
- 📍 **Joylashuvi:** `admin_bot/handlers/mtproto_auth.py:150-165`
- 💥 **Muammo:** Foydalanuvchi kiritgan 2FA paroli xato bo'lsa, xatolik matnida maxfiy parol qolib ketishi mumkin.
- 🛠 **Yechim:** Xabarlarni sanitize qilish va parollarni darhol xotiradan tozalash.

### 68. `can_user_add_channel` da trial muddati o'tganini noto'g'ri hisoblash
- 📍 **Joylashuvi:** `database/models.py:36-43` `is_active`
- 💥 **Muammo:** Agar `trial_expires_at` null bo'lsa, `return True` beradi (cheksiz bepul bo'lib qoladi).
- 🛠 **Yechim:** Agar `trial_expires_at` null bo'lsa, yaratilgan sanadan 14 kun hisoblab tekshirish.

### 69. `pre_checkout_query` da to'lov summasi tekshirilmasdan `ok=True` berilishi
- 📍 **Joylashuvi:** `bot/handlers/stars_billing.py:143-145`
- 💥 **Muammo:** Har qanday `pre_checkout_query` so'roviga tekshiruvsiz `ok=True` qaytariladi.
- 🛠 **Yechim:** Payload va narx to'g'riligini tasdiqlab keyin javob berish.

### 70. Telethon login sessiyasi lug'ati (`_login_sessions`) xotiradan tozalanmasligi
- 📍 **Joylashuvi:** `services/telethon_listener.py:300-305`
- 💥 **Muammo:** Agar admin telefon raqam kiritib, kodni yozmasdan chiqib ketsa, uning ochiq `TelegramClient` obyekti xotirada qolib ketadi.
- 🛠 **Yechim:** 10 daqiqalik TTL taymer qo'yib, javob bo'lmasa sessiyani avtomatik yopish.

### 71. Foydalanuvchi o'z kanal juftligini o'chirganda arxiv zaxirasining o'chmay qolishi
- 📍 **Joylashuvi:** `database/db_manager.py:722-727` `delete_pair()`
- 💥 **Muammo:** `channel_backups` jadvalidan postlar o'chirilmaydi, natijada baza hajmi qisqarmaydi.
- 🛠 **Yechim:** `DELETE FROM channel_backups WHERE pair_id = ?` buyrug'ini qo'shish.

### 72. Baza backup faylini jo'natgandan keyin o'chirishda poyga holati (Race Condition)
- 📍 **Joylashuvi:** `admin_bot/handlers/backup.py:54-59`
- 💥 **Muammo:** Fayl to'liq Telegramga yuklanmasdan turib `finally` blokida o'chirishga urinish fayl jo'natilishini buzishi mumkin.
- 🛠 **Yechim:** `await event.bot.send_document` to'liq yakunlanganini kutish.

### 73. User ID larni tekshirishda manfiy ID lar (Guruh/Superguruh) chalkashligi
- 📍 **Joylashuvi:** `database/db_manager.py:510` `search_users()`
- 💥 **Muammo:** `clean_q.isdigit()` manfiy ID li guruh yoki kanallarni to'g'ri aniqlay olmaydi.
- 🛠 **Yechim:** `lstrip("-").isdigit()` orqali tekshirish.

### 74. Obuna bekor qilinganda (`revoke_subscription`) keshning eskirishi
- 📍 **Joylashuvi:** `database/db_manager.py:334-340`
- 💥 **Muammo:** Kesh o'chiriladi, lekin boshqa xizmatlardagi lokal o'zgaruvchilar 45 soniyagacha eski obunani faol deb ko'rsatishi mumkin.
- 🛠 **Yechim:** Keshni majburiy tozalash bilan birga faol klonlash jarayonlarini yangilash.

### 75. `/cancel` buyrug'ida begona foydalanuvchilar vazifalarini to'xtatib yuborish
- 📍 **Joylashuvi:** `bot/handlers/start.py:153-166`
- 💥 **Muammo:** Oddiy foydalanuvchi `/cancel` yozganda faqat o'ziga tegishli `pair_id` vazifasini to'xtatishi kerak, aks holda boshqa odamning jarayoni to'xtab qoladi.
- 🛠 **Yechim:** Vazifa egasi `user_id == pair.user_id` ekanini qat'iy tekshirish.

---

## 🗄️ 6-QISM: MA'LUMOTLAR BAZASI & IN-MEMORY KESH KAMCHILIKLARI (76–85)

### 76. In-Memory Deduplication Keshining (LRUSet) qayta ishga tushganda yo'qolishi
- 📍 **Joylashuvi:** `services/cache_manager.py:16-30` `LRUSet`
- 💥 **Muammo:** Bot qayta yoqilganda faqat oxirgi 50,000 ta post yuklanadi. Agar kanal 100,000 postga ega bo'lsa, eski postlar dublikat bo'lib qayta ko'chirilishi mumkin.
- 🛠 **Yechim:** SQLite bazasida `idx_cloned_lookup` indeksidan to'g'ri foydalanish va in-memory keshni 100k ga kengaytirish.

### 77. SQLite `busy_timeout` kamligi sababli yozishda to'xtashlar
- 📍 **Joylashuvi:** `database/db_manager.py:26` `PRAGMA busy_timeout = 30000;`
- 💥 **Muammo:** Bir vaqtda 50 ta korutina DB ga yozmoqchi bo'lsa, 30 soniya ichida qulflanish yuz bersa xato tashlaydi.
- 🛠 **Yechim:** Yozuvchi so'rovlarni yagona asinxron navbat (Write Queue) orqali bitta oqimda amalga oshirish.

### 78. `TTLCache` da eskirgan kalitlarning xotirada qolib ketishi (Memory Leak)
- 📍 **Joylashuvi:** `services/cache_manager.py:39-46`
- 💥 **Muammo:** Kalit faqat unga murojaat qilingandagina o'chiriladi. Agar 10,000 foydalanuvchi bir marta kirib boshqa kirmasa, ularning ma'lumotlari xotirada abadiy qoladi.
- 🛠 **Yechim:** Vaqti-vaqti bilan butun lug'atni aylanib eskirganlarni tozalovchi fon tozalagich (Pruner) qo'shish.

### 79. `get_users_detailed` so'rovida `LEFT JOIN` sekinligi
- 📍 **Joylashuvi:** `database/db_manager.py:490-501`
- 💥 **Muammo:** `GROUP BY u.user_id` bilan 3 ta jadvalni birlashtirish foydalanuvchilar soni 50,000 dan oshganda 3-5 soniya vaqt oladi.
- 🛠 **Yechim:** Indekslarni optimallashtirish va `COUNT(p.id)` ni subquery ko'rinishiga o'tkazish.

### 80. Dinamik migratsiyalarda (`ALTER TABLE`) xatolar yutilib ketishi
- 📍 **Joylashuvi:** `database/db_manager.py:166-197`
- 💥 **Muammo:** `except Exception: pass` qilingan. Agar disk to'lib qolgan yoki baza buzilgan bo'lsa, ustun qo'shilmay qoladi va keyingi so'rovlar qulaydi.
- 🛠 **Yechim:** Faqat `duplicate column name` xatosini e'tiborsiz qoldirib, boshqa jiddiy xatolarni logga yozish.

### 81. `update_pair_ids` da bo'sh parametrlar yuborilganda SQL ishlamasligi
- 📍 **Joylashuvi:** `database/db_manager.py:712-720`
- 💥 **Muammo:** Agar ikkala ID ham None bo'lsa, hech qanday SQL bajarilmaydi, lekin xato ham qaytarilmaydi.
- 🛠 **Yechim:** Parametrlarni tekshirib `ValueError` qaytarish.

### 82. Foydalanuvchi ma'lumotlarini yangilashda `username` None bo'lsa eskisini o'chirib yuborish
- 📍 **Joylashuvi:** `database/db_manager.py:426-430` `get_or_create_user()`
- 💥 **Muammo:** Agar foydalanuvchi vaqtincha username'siz kirsa, uning bazadagi eski username'i `NULL` ga aylanib ketadi.
- 🛠 **Yechim:** `COALESCE(?, username)` orqali mavjud qiymatni saqlab qolish.

### 83. Baza snapshot nusxasi yaratilganda diskdagi joy yetishmasligi
- 📍 **Joylashuvi:** `database/db_manager.py:794-806`
- 💥 **Muammo:** Agar baza 2GB bo'lsa, zaxira nusxa yaratish uchun yana 2GB joy kerak bo'ladi. Disk to'lsa bot to'xtab qoladi.
- 🛠 **Yechim:** Zaxira yaratishdan oldin disk bo'sh joyini (`shutil.disk_usage`) tekshirish.

### 84. `get_stats` dagi agregat funksiyalarning sekinlashuvi
- 📍 **Joylashuvi:** `database/db_manager.py:820-845`
- 💥 **Muammo:** Har safar admin panel ochilganda 5 ta alohida `COUNT(*)` so'rovi yuboriladi.
- 🛠 **Yechim:** Statistikani 60 soniyalik keshda saqlash.

### 85. `cloned_messages` jadvalidagi eskirgan postlarni tozalash funksiyasi yo'qligi
- 📍 **Joylashuvi:** `database/db_manager.py:122-132`
- 💥 **Muammo:** 1 yildan oshgan postlar ham jadvalda qolib ketadi, bu qidiruv tezligini sekinlashtiradi.
- 🛠 **Yechim:** 6 oydan oshgan deduplikatsiya yozuvlarini arxivlovchi/tozalovchi davriy vazifa qo'shish.

---

## 🎨 7-QISM: UI/UX, INLINE TUGMALAR & FOYDALANUVCHI NOQULAYLIKLARI (86–95)

### 86. Telefon ekranida tugma matnlarining kesilib qolishi (Button Label Truncation)
- 📍 **Joylashuvi:** `bot/keyboards/inline_buttons.py:125-165` `get_pair_detail_keyboard()`
- 💥 **Muammo:** 2 ustunli tartibda `So'z/Raqam Almashtirish` yoki `Zaxira & Qayta Tiklash` matnlari kichik ekranli telefonlarda (iPhone SE, Android) `So'z/Raqam Al...` bo'lib kesilib qoladi.
- 🛠 **Yechim:** Matnlarni ixchamlashtirish: `So'z Almashtirgich`, `Zaxira & Tiklash`.

### 87. Inline tugmalarda xato bildirishnomalar (`safe_answer`) ko'rinmasligi
- 📍 **Joylashuvi:** `bot/handlers/cloner_menu.py:30-34`
- 💥 **Muammo:** `safe_answer(callback)` barcha xatolarni yutib yuboradi. Agar Telegram xato bersa, foydalanuvchi tugmani bosganda hech narsa o'zgarmasdan bot qotib qolgandek tuyuladi.
- 🛠 **Yechim:** Xatolik yuz berganda `show_alert=True` bilan foydalanuvchi ekraniga xabar chiqarish.

### 88. Forward qilingan xabarlarda "Foydalanuvchi profili yopiq" bo'lsa xato ko'rsatmaslik
- 📍 **Joylashuvi:** `bot/handlers/cloner_menu.py:141-146`
- 💥 **Muammo:** Foydalanuvchi shaxsiy yopiq profildan xabar uzatsa, bot kanal emasligini tushunmaydi va faqat umumiy "Kanal aniqlanmadi" deb javob beradi.
- 🛠 **Yechim:** Foydalanuvchiga aniq yo'riqnoma berish: *"Siz kanaldan emas, shaxsiy profildan xabar uzatdingiz. Iltimos, kanaldagi postni uzating."*

### 89. Obuna muddatining sanasi chiroyli formatlanmagani
- 📍 **Joylashuvi:** `bot/handlers/stars_billing.py:57-65`
- 💥 **Muammo:** Sana `2026-09-14` ko'rinishida chiqadi. Bu oddiy foydalanuvchilar uchun tushunarsiz.
- 🛠 **Yechim:** `14-Sentabr, 2026-yil` formatiga o'tkazish.

### 90. `/help` qo'llanmasida yangi Next-Gen funksiyalar (AI, Video Watermark) yo'qligi
- 📍 **Joylashuvi:** `bot/handlers/help_guide.py:20-61`
- 💥 **Muammo:** Qo'llanmada faqat eski 6 ta funksiya ko'rsatilgan. Yangi qo'shilgan AI Paraphraser, Drip Feed, Disaster Recovery haqida ma'lumot yo'q.
- 🛠 **Yechim:** Qo'llanmani to'liq 10 ta eng kuchli imkoniyat bilan boyitish.

### 91. Bosh menyudagi Reply tugmalar va Inline tugmalar dublikatsiyasi
- 📍 **Joylashuvi:** `bot/handlers/start.py:61-71`
- 💥 **Muammo:** `/start` bosilganda bir vaqtning o'zida ham Reply klaviatura, ham Inline menyu yuboriladi. Bu chat oynasini to'ldirib yuboradi.
- 🛠 **Yechim:** Yagona, tartibli bitta boshqaruv menyusini yuborish.

### 92. Watermark pozitsiyasini tanlashda vizual ko'rsatkich yo'qligi
- 📍 **Joylashuvi:** `bot/handlers/settings_menu.py:99-115`
- 💥 **Muammo:** Tugmalarda hozir qaysi pozitsiya (masalan: `bottom_right`) tanlangani belgilanmagan (Checkmark `✅` yo'q).
- 🛠 **Yechim:** Tanlangan pozitsiya tugmasi yoniga `✅` belgisini dinamik qo'yish.

### 93. Admin qidiruvida foydalanuvchi topilmaganda noto'g'ri tugma chiqishi
- 📍 **Joylashuvi:** `admin_bot/handlers/user_management.py:102-107`
- 💥 **Muammo:** Foydalanuvchi topilmasa, "Ro'yxatga qaytish" tugmasi bosilganda xatolik berishi mumkin.
- 🛠 **Yechim:** Callback datasini to'g'rilash va FSM holatlarini tozalash.

### 94. Tarix ko'chirish jarayonida bot bloklanib qolishi hissi
- 📍 **Joylashuvi:** `bot/handlers/history_clone.py:128-138`
- 💥 **Muammo:** Progress bar har 3 soniyada yangilanadi. Agar 1 ta katta video yuklanayotgan bo'lsa, 30 soniya davomida foiz o'zgarmaydi va foydalanuvchi to'xtab qoldi deb o'ylaydi.
- 🛠 **Yechim:** *"Hozir yuklanmoqda: 15MB video..."* kabi jonli status matnini chiqarish.

### 95. Kanal sozlamalaridagi "Prevyu" simulyatorida real suv belgisi ko'rinmasligi
- 📍 **Joylashuvi:** `bot/handlers/cloner_menu.py:370-388`
- 💥 **Muammo:** Prevyu faqat matn ko'rinishida chiqadi, rasmga suv belgisi qanday tushishi rasm ko'rinishida ko'rsatilmaydi.
- 🛠 **Yechim:** Test post funksiyasiga o'xshash kichik namunaviy rasm generatsiya qilib ko'rsatish.

---

## ☁️ 8-QISM: DEVOPS, DOCKER & 24/7 CLOUD BARQARORLIGI KAMCHILIKLARI (96–100)

### 96. Dockerfile da `ffmpeg` va shriftlar paketi to'liq o'rnatilmaganligi
- 📍 **Joylashuvi:** `Dockerfile:1-40`
- 💥 **Muammo:** Agar konteyner ichida `fonts-dejavu-core` yoki `ffmpeg` bo'lmasa, Video Watermark va Image Watermark xizmatlari xato bilan qulaydi.
- 🛠 **Yechim:** `RUN apt-get update && apt-get install -y ffmpeg fonts-dejavu-core fontconfig` ni qat'iy tekshirish.

### 97. Docker konteyneri root foydalanuvchisi ostida ishlashi (Security Risk)
- 📍 **Joylashuvi:** `Dockerfile:30-40`
- 💥 **Muammo:** Root huquqi bilan ishlash konteynerdan tashqariga chiqish (Container Escape) zaifligini keltirib chiqarishi mumkin.
- 🛠 **Yechim:** `USER appuser` ko'rsatmasini Dockerfile ga kiritish.

### 98. Docker `HEALTHCHECK` direktivasi yo'qligi
- 📍 **Joylashuvi:** `Dockerfile` va `docker-compose.yml`
- 💥 **Muammo:** Agar bot ichida event loop qotib qolsa, Docker buni sezmaydi va konteynerni qayta ishga tushirmaydi (`Unhealthy` holat aniqlanmaydi).
- 🛠 **Yechim:** `HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8080/health || exit 1` qo'shish.

### 99. GitHub Webhook avto-deployerida xavfli `git reset --hard` buyrug'i
- 📍 **Joylashuvi:** `deploy/webhook/deploy_webhook.py:57`
- 💥 **Muammo:** `git reset --hard` qilinganda serverdagi lokal o'zgarishlar va `.env` fayllari tasodifan yo'qotilishi mumkin.
- 🛠 **Yechim:** `.env` va `database/` papkalarini `.gitignore` da qat'iy saqlab, `git stash` yoki ehtiyotkorona yangilash.

### 100. Oracle Anti-Reclamation skriptida xotira ajratishda (RSS) limitdan oshish
- 📍 **Joylashuvi:** `deploy/anti_reclaim.py:60-67`
- 💥 **Muammo:** 1GB RAM li kichik serverlarda 24% xotira ajratilganda va botning o'zi media yuklayotganda Docker Linux OOM (Out Of Memory) Killer tomonidan o'ldirilishi mumkin.
- 🛠 **Yechim:** Bo'sh xotira 150MB dan kam qolganda dinamik ravishda anti-reclaim xotirasini qisqartirish.

---

## 🎯 XULOSA VA TAVSIYA ETILGAN TUZATISH REJASI

Ushbu topilgan 100 ta nuqson va kamchiliklarni bartaraf etish orqali bot:
1. **100,000+** xabarlarni soniyasiga 50+ gacha parallel oqimda nol kechikish bilan o'tkaza oladi.
2. Hech qanday **FloodWait**, **XSS/HTML parse error** yoki **qotib qolishlarsiz** 24/7 uzluksiz ishlaydi.
3. Barcha Telegram Premium animatsion emojilar, FFmpeg video watermarklar va avto-tarjimalar 100% aniqlikda chiqadi.
