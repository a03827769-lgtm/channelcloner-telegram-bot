"""
Telegram Kanal Kloner — interaktiv sozlash ustasi (setup wizard).

Loyiha ildizidagi .env faylini yaratadi yoki yangilaydi:
  * savolga javob berilmasa (Enter), mavjud qiymat o'zgarmaydi;
  * fayl atomik tarzda yoziladi (vaqtinchalik fayl + os.replace), izohlar va tartib saqlanadi;
  * ENCRYPTION_KEY bo'lmasa, yangisi yaratiladi;
  * maxfiy qiymatlar (tokenlar, hash, parollar) ekranga chiqarilmaydi.

Ishga tushirish:  python setup_wizard.py
"""

import asyncio
import base64
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"

DEVICE_MODEL = "Klonla Bot Server"
SYSTEM_VERSION = "Linux Server 64bit"
APP_VERSION = "KlonlaBot Pro v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_PLACEHOLDER_RE = re.compile(r"^YOUR_[A-Z0-9_]+_HERE$", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------------
# .env helpers (pure functions, covered by tests)
# ---------------------------------------------------------------------------------------------------

def parse_env_lines(lines: List[str]) -> Dict[str, str]:
    """KEY -> value of the non-comment KEY=VALUE lines (the first occurrence wins)."""
    values: Dict[str, str] = {}
    for line in lines:
        match = _ENV_LINE_RE.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def merge_env_lines(lines: List[str], updates: Dict[str, str]) -> List[str]:
    """Applies `updates` to .env lines: an existing KEY= line is replaced in place (later duplicates of an
    updated key are dropped), missing keys are appended. Comments, blank lines and other keys are kept."""
    result: List[str] = []
    written = set()
    for line in lines:
        match = _ENV_LINE_RE.match(line)
        if match and not line.lstrip().startswith("#") and match.group(1) in updates:
            key = match.group(1)
            if key in written:
                continue
            result.append(f"{key}={updates[key]}\n")
            written.add(key)
            continue
        result.append(line if line.endswith("\n") else line + "\n")
    missing = [key for key in updates if key not in written]
    if missing:
        if result and result[-1].strip():
            result.append("\n")
        result.extend(f"{key}={updates[key]}\n" for key in missing)
    return result


def write_env_atomic(path: Path, lines: List[str]) -> None:
    """Writes the file via a temporary file in the same directory and os.replace (never half-written)."""
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def generate_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def usable(value: Optional[str]) -> Optional[str]:
    """An existing value, unless it is empty or a .env.example placeholder."""
    if value is None:
        return None
    value = value.strip()
    return value if value and not _PLACEHOLDER_RE.match(value) else None


def is_valid_bot_token(value: str) -> bool:
    return bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", value))


def is_valid_api_id(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def is_valid_api_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value))


def is_valid_user_id(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def is_valid_admin_ids(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[,;\s]+\d+)*", value.strip()))


def is_valid_webapp_url(value: str) -> bool:
    return bool(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/\S*)?", value))


# ---------------------------------------------------------------------------------------------------
# Interactive part
# ---------------------------------------------------------------------------------------------------

def ask(label: str, current: Optional[str], validator: Optional[Callable[[str], bool]] = None,
        error: str = "Noto'g'ri qiymat.", secret: bool = False, optional: bool = False) -> Optional[str]:
    """Asks for a value. Enter keeps `current` (or skips an optional value). Secrets are read hidden."""
    if current:
        hint = " [Enter — saqlangan qiymat qoladi]"
    elif optional:
        hint = " [Enter — o'tkazib yuborish]"
    else:
        hint = ""
    while True:
        prompt = f"   {label}{hint}: "
        raw = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if not raw:
            if current:
                return current
            if optional:
                return None
            print("   ❌ Bu qiymat majburiy.")
            continue
        if validator and not validator(raw):
            print(f"   ❌ {error}")
            continue
        return raw


def print_banner():
    print("=" * 65)
    print("   🤖 TELEGRAM KANAL KLONER — SOZLASH USTASI (SETUP WIZARD)")
    print("=" * 65)
    print(f"Sozlamalar fayli: {ENV_PATH}")
    print("Enter bosilsa mavjud qiymat o'zgarmaydi. Maxfiy qiymatlar ekranda ko'rinmaydi.\n")


async def create_telethon_session(api_id: int, api_hash: str) -> Optional[str]:
    """Interactive MTProto login; returns a StringSession or None when skipped/failed."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    phone_raw = input("   Telefon raqamingiz (+998901234567) [Enter — o'tkazib yuborish]: ").strip()
    if not phone_raw:
        return None
    try:
        from services.phone_utils import normalize_phone_number
        is_valid, normalized, _ = normalize_phone_number(phone_raw)
        phone = normalized if is_valid else phone_raw
    except Exception:
        phone = phone_raw

    client = TelegramClient(
        StringSession(), api_id, api_hash,
        device_model=DEVICE_MODEL, system_version=SYSTEM_VERSION, app_version=APP_VERSION,
        lang_code=LANG_CODE, system_lang_code=SYSTEM_LANG_CODE,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("   📩 Telegramga kelgan tasdiqlash kodini kiriting: ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = getpass.getpass("   🔐 2-bosqichli parolingiz (Two-Step Verification): ")
                await client.sign_in(password=password)
        session_string = client.session.save()
        me = await client.get_me()
        print(f"\n   ✅ Muvaffaqiyatli ulandi: {me.first_name} (@{me.username or 'yoq'})")
        return session_string
    except Exception as e:
        print(f"\n   ⚠️ Telethon sessiya yaratishda xatolik: {type(e).__name__}")
        print("   Keyinroq admin bot orqali (MTProto bo'limi) yoki shu ustani qayta ishga tushirib ulashingiz mumkin.")
        return None
    finally:
        await client.disconnect()


async def main():
    print_banner()
    lines: List[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    existing = parse_env_lines(lines)
    current = {key: usable(value) for key, value in existing.items()}
    updates: Dict[str, str] = {}

    print("1️⃣  TELEGRAM BOT TOKENLARI (@BotFather)")
    updates["BOT_TOKEN"] = ask("BOT_TOKEN", current.get("BOT_TOKEN"), is_valid_bot_token,
                               "Noto'g'ri format! Masalan: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ...", secret=True)
    while True:
        admin_token = ask("ADMIN_BOT_TOKEN (alohida admin bot, tavsiya etiladi)", current.get("ADMIN_BOT_TOKEN"),
                          is_valid_bot_token, "Noto'g'ri token formati.", secret=True, optional=True)
        if admin_token and admin_token == updates["BOT_TOKEN"]:
            print("   ❌ ADMIN_BOT_TOKEN asosiy BOT_TOKEN bilan bir xil bo'lmasligi kerak.")
            continue
        break
    if admin_token:
        updates["ADMIN_BOT_TOKEN"] = admin_token

    print("\n2️⃣  TELEGRAM API ID & HASH (https://my.telegram.org)")
    updates["TELEGRAM_API_ID"] = ask("TELEGRAM_API_ID", current.get("TELEGRAM_API_ID"), is_valid_api_id,
                                     "API_ID faqat raqamlardan iborat bo'lishi kerak!")
    updates["TELEGRAM_API_HASH"] = ask("TELEGRAM_API_HASH", current.get("TELEGRAM_API_HASH"), is_valid_api_hash,
                                       "API_HASH 32 ta 16-lik belgidan iborat bo'lishi kerak!", secret=True)

    print("\n3️⃣  ADMINISTRATORLAR (@userinfobot orqali ID ni bilish mumkin)")
    primary = ask("PRIMARY_SUPER_ADMIN_ID", current.get("PRIMARY_SUPER_ADMIN_ID"), is_valid_user_id,
                  "Faqat musbat raqam kiriting.", optional=True)
    if primary:
        updates["PRIMARY_SUPER_ADMIN_ID"] = primary
    admin_ids = ask("ADMIN_IDS (vergul bilan)", current.get("ADMIN_IDS"), is_valid_admin_ids,
                    "Masalan: 123456789,987654321", optional=True)
    if admin_ids:
        updates["ADMIN_IDS"] = admin_ids

    print("\n4️⃣  MINI APP MANZILI (nomli Cloudflare tunnel domeni, https://...)")
    webapp_url = ask("WEBAPP_URL", current.get("WEBAPP_URL"), is_valid_webapp_url,
                     "Manzil https:// bilan boshlanishi kerak.", optional=True)
    if webapp_url:
        updates["WEBAPP_URL"] = webapp_url

    print("\n5️⃣  TELEGRAM AKKAUNTGA KIRISH (MTProto sessiya, ixtiyoriy)")
    session = None
    if current.get("TELETHON_SESSION"):
        again = input("   TELETHON_SESSION allaqachon bor. Yangisini yaratasizmi? (y/N): ").strip().lower()
        if again in ("y", "yes", "ha"):
            session = await create_telethon_session(int(updates["TELEGRAM_API_ID"]), updates["TELEGRAM_API_HASH"])
    else:
        session = await create_telethon_session(int(updates["TELEGRAM_API_ID"]), updates["TELEGRAM_API_HASH"])
    if session:
        updates["TELETHON_SESSION"] = session

    print("\n6️⃣  XAVFSIZLIK")
    if current.get("ENCRYPTION_KEY"):
        print("   ✅ ENCRYPTION_KEY mavjud — o'zgartirilmadi.")
    else:
        updates["ENCRYPTION_KEY"] = generate_encryption_key()
        print("   🔐 Yangi ENCRYPTION_KEY yaratildi va .env ga yozildi (qiymati ekranga chiqarilmaydi).")
        print("      Uning nusxasini xavfsiz joyda saqlang: kalitsiz shifrlangan sessiya va zaxiralarni ochib bo'lmaydi.")

    if not lines:
        lines = ["# Telegram Channel Cloner konfiguratsiyasi (setup_wizard.py). Git'ga qo'shmang!\n"]
    write_env_atomic(ENV_PATH, merge_env_lines(lines, updates))

    print("\n" + "=" * 65)
    print(f"🎉 SOZLAMALAR SAQLANDI: {ENV_PATH}")
    print("=" * 65)
    print("Botni ishga tushirish (bitta nusxada!):")
    print("👉 Windows xizmati: python scripts/setup_autostart.py install --start")
    print("👉 Yoki qo'lda:      python run.py\n")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n❌ Sozlash bekor qilindi (.env o'zgartirilmadi).")
        sys.exit(0)
