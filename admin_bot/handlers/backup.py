import os
import zipfile
import asyncio
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, FSInputFile
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from services.security_vault import security_vault
from services.custom_emojis import SAVE_BACKUP, SUCCESS, ERROR, LOADING, LOCK_LOCKED, WARN

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="admin_backup_router")

@router.callback_query(F.data == "admin_download_backup")
@router.message(Command("backup"))
@router.message(F.text.contains("Baza Nusxasi"))
async def cb_download_backup(event: CallbackQuery | Message):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        wait_msg = await event.message.answer(f"{LOADING} Ma'lumotlar bazasining zaxira nusxasi (ZIP snapshot) tayyorlanmoqda...")
    else:
        wait_msg = await event.answer(f"{LOADING} Ma'lumotlar bazasining zaxira nusxasi (ZIP snapshot) tayyorlanmoqda...")

    backup_path = await db_manager.create_backup_file()

    if not backup_path or not os.path.exists(backup_path):
        await wait_msg.edit_text(f"{ERROR} Baza zaxira nusxasini yaratishda xatolik yuz berdi!", reply_markup=get_back_to_admin_keyboard())
        return

    zip_path = f"{backup_path}.zip"
    enc_path = f"{zip_path}.enc"
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(backup_path, arcname=os.path.basename(backup_path))

        zip_size = os.path.getsize(zip_path)
        max_backup_bytes = 50 * 1024 * 1024  # 50 MB Telegram bot API upload boundary
        if zip_size > max_backup_bytes:
            raise ValueError(f"Zaxira fayli hajmi ({zip_size / (1024*1024):.1f} MB) 50 MB ruxsat etilgan limitdan oshdi!")

        # Encrypt the ZIP archive before transmitting over Telegram to prevent plaintext exposure
        def _sync_encrypt_backup():
            with open(zip_path, "rb") as f_in:
                raw_data = f_in.read()
            encrypted_data = security_vault.encrypt_bytes(raw_data)
            with open(enc_path, "wb") as f_out:
                f_out.write(encrypted_data)
        await asyncio.to_thread(_sync_encrypt_backup)

        # STRICT SECURITY: Verify encryption succeeded. NEVER fallback to plaintext ZIP!
        if not os.path.exists(enc_path) or os.path.getsize(enc_path) == 0:
            raise RuntimeError("Shifrlash muvaffaqiyatsiz bo'ldi. Xavfsizlik talablariga muvofiq shifrlanmagan fayl yuborilmaydi!")

        send_file = enc_path
        file_size_mb = os.path.getsize(send_file) / (1024 * 1024)

        doc = FSInputFile(send_file, filename=os.path.basename(send_file))
        caption = (
            f"{SUCCESS} <b>Baza xavfsiz zaxira nusxasi tayyor!</b>\n\n"
            f"Fayl: <code>{os.path.basename(send_file)}</code>\n"
            f"Hajmi: <code>{file_size_mb:.2f} MB</code>\n"
            f"Xavfsizlik: {LOCK_LOCKED} <b>AES-128 / Fernet bilan to'liq shifrlangan</b>\n\n"
            f"<i>Tiklash uchun: <code>python scripts/decrypt_backup.py {os.path.basename(send_file)}</code></i>"
        )
        chat_id = event.from_user.id
        if isinstance(event, CallbackQuery):
            await event.bot.send_document(
                chat_id=chat_id,
                document=doc,
                caption=caption,
                parse_mode="HTML"
            )
        else:
            await event.answer_document(
                document=doc,
                caption=caption,
                parse_mode="HTML"
            )
        try:
            await wait_msg.delete()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    except Exception as e:
        logger.error(f"Error sending backup document: {e}")
        try:
            await wait_msg.edit_text(f"{ERROR} Faylni yuborishda xatolik: {e}", reply_markup=get_back_to_admin_keyboard())
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    finally:
        # Cleanup temporary backup files after sending
        for p in (backup_path, zip_path, enc_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
