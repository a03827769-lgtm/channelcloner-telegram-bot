import asyncio
import os
import sys
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

DEVICE_MODEL = "Klonla Bot Server"
SYSTEM_VERSION = "Linux Server 64bit"
APP_VERSION = "KlonlaBot Pro v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

def print_banner():
    print("=" * 65)
    print("   🤖 TELEGRAM KANAL KLONER — SOZLASH USTASI (SETUP WIZARD)")
    print("=" * 65)
    print("Ushbu yordamchi botingiz va Telegram akkauntingizni sozlashga yordam beradi.\n")

async def main():
    print_banner()

    # 1. BOT TOKEN
    print("1️⃣  TELEGRAM BOT TOKEN")
    print("   @BotFather dan olgan bot tokeningizni kiriting:")
    bot_token = input("   BOT_TOKEN: ").strip()
    while not bot_token or ":" not in bot_token:
        print("   ❌ Noto'g'ri format! Masalan: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ")
        bot_token = input("   BOT_TOKEN: ").strip()

    # 2. TELEGRAM API CREDENTIALS
    print("\n2️⃣  TELEGRAM API ID & HASH")
    print("   https://my.telegram.org saytidan olingan API ma'lumotlari:")
    
    api_id_raw = input("   API_ID (masalan: 12345678): ").strip()
    while not api_id_raw.isdigit():
        print("   ❌ API_ID faqat raqamlardan iborat bo'lishi kerak!")
        api_id_raw = input("   API_ID: ").strip()
    api_id = int(api_id_raw)

    api_hash = input("   API_HASH (masalan: 0123456789abcdef0123456789abcdef): ").strip()
    while len(api_hash) < 10:
        print("   ❌ API_HASH noto'g'ri!")
        api_hash = input("   API_HASH: ").strip()

    # 3. ADMIN ID
    print("\n3️⃣  ADMIN TELEGRAM ID (Ixtiyoriy)")
    print("   O'zingizning Telegram user ID raqamingiz (@userinfobot dan bilish mumkin):")
    admin_id = input("   ADMIN_ID: ").strip()

    # 4. TELETHON USER LOGIN
    print("\n4️⃣  TELEGRAM AKKAUNTGA KIRISH (MTProto Sessiya)")
    print("   Begona ochiq kanallarni kuzatish uchun Telegram akkauntingizga ulanamiz.")
    phone = input("   Telefon raqamingiz (+998901234567): ").strip()

    session_string = ""
    try:
        client = TelegramClient(
            StringSession(),
            api_id,
            api_hash,
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
            lang_code=LANG_CODE,
            system_lang_code=SYSTEM_LANG_CODE
        )
        await client.connect()

        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            code = input("   📩 Telegramga kelgan tasdiqlash kodini kiriting: ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                pwd = input("   🔐 2-bosqichli parolingizni (Two-Step Verification) kiriting: ").strip()
                await client.sign_in(password=pwd)

        session_string = client.session.save()
        me = await client.get_me()
        print(f"\n   ✅ Muvaffaqiyatli ulandi: {me.first_name} (@{me.username or 'yoq'})")
        await client.disconnect()

    except Exception as e:
        print(f"\n   ⚠️ Telethon sessiya yaratishda xatolik: {e}")
        print("   Keyinroq .env fayliga TELETHON_SESSION ni qo'lda kiritishingiz mumkin.")

    # 5. WRITE .ENV FILE
    env_content = f"""# Telegram Channel Cloner Configuration
BOT_TOKEN={bot_token}
TELEGRAM_API_ID={api_id}
TELEGRAM_API_HASH={api_hash}
TELETHON_SESSION={session_string}
ADMIN_IDS={admin_id}
DB_PATH=database/cloner.db
TEMP_DOWNLOAD_DIR=temp_media
"""
    with open(".env", "w", encoding="utf-8") as f:
        f.write(env_content)

    print("\n" + "=" * 65)
    print("🎉 BARCHA SOZLAMALAR .env FAYLIGA SAQLANDI!")
    print("=" * 65)
    print("Endi botni ishga tushirish uchun quyidagi buyruqni bering:")
    print("👉 python run.py\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n❌ Sozlash bekor qilindi.")
        sys.exit(0)
