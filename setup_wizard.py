import asyncio
import os
import sys
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import logging
logger = logging.getLogger(__name__)

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
    phone_raw = input("   Telefon raqamingiz (+998901234567): ").strip()
    try:
        from services.phone_utils import normalize_phone_number
        is_val, norm_p, _ = normalize_phone_number(phone_raw)
        phone = norm_p if is_val else phone_raw
    except Exception:
        phone = phone_raw

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

    # 5. WRITE / UPDATE .ENV FILE (Preserve existing keys like ADMIN_BOT_TOKEN, ENCRYPTION_KEY, etc.)
    existing_env: dict[str, str] = {}
    if os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if line_s and not line_s.startswith("#") and "=" in line_s:
                        k, v = line_s.split("=", 1)
                        existing_env[k.strip()] = v.strip()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    existing_env["BOT_TOKEN"] = bot_token
    existing_env["TELEGRAM_API_ID"] = str(api_id)
    existing_env["TELEGRAM_API_HASH"] = api_hash
    if session_string:
        existing_env["TELETHON_SESSION"] = session_string
    existing_env["ADMIN_IDS"] = admin_id
    if "DB_PATH" not in existing_env:
        existing_env["DB_PATH"] = "database/cloner.db"
    if "TEMP_DOWNLOAD_DIR" not in existing_env:
        existing_env["TEMP_DOWNLOAD_DIR"] = "temp_media"

    env_lines = ["# Telegram Channel Cloner Configuration\n"]
    for k, v in existing_env.items():
        env_lines.append(f"{k}={v}\n")

    with open(".env", "w", encoding="utf-8") as f:
        f.writelines(env_lines)

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
