import os
import asyncio
import zipfile
import logging
from aiogram.types import CallbackQuery, Message, FSInputFile
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from admin_bot.permissions import ensure_super_admin
from database.db_manager import db_manager
from services.security_vault import security_vault
from services.custom_emojis import SUCCESS, ERROR, LOADING, LOCK_LOCKED, WARN

from bot.utils import safe_answer, html_escape

logger = logging.getLogger(__name__)

# Bot API upload limit is 50 MB; keep a small margin for the multipart envelope.
MAX_UPLOAD_BYTES = 49 * 1024 * 1024

_backup_lock = asyncio.Lock()


def _zip_and_encrypt(db_snapshot_path: str, enc_path: str) -> int:
    """Compresses the snapshot and encrypts it in one pass on a worker thread. Returns the encrypted size."""
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
        zipf.write(db_snapshot_path, arcname=os.path.basename(db_snapshot_path))
    encrypted = security_vault.encrypt_bytes(buffer.getvalue())
    buffer.close()
    if not encrypted:
        raise RuntimeError("Shifrlash natijasi bo'sh")
    with open(enc_path, "wb") as f_out:
        f_out.write(encrypted)
    return len(encrypted)


async def cb_download_backup(event: CallbackQuery | Message):
    if not await ensure_super_admin(event):
        return
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
    reply_target = event.message if isinstance(event, CallbackQuery) else event

    if _backup_lock.locked():
        await reply_target.answer(f"{WARN} Zaxira nusxa allaqachon tayyorlanmoqda. Iltimos, kuting.")
        return

    async with _backup_lock:
        wait_msg = await reply_target.answer(f"{LOADING} Ma'lumotlar bazasining shifrlangan zaxira nusxasi tayyorlanmoqda...")
        backup_path = await db_manager.create_backup_file()
        if not backup_path or not os.path.exists(backup_path):
            await wait_msg.edit_text(f"{ERROR} Baza zaxira nusxasini yaratishda xatolik yuz berdi!", reply_markup=get_back_to_admin_keyboard())
            return

        enc_path = f"{backup_path}.zip.enc"
        try:
            enc_size = await asyncio.to_thread(_zip_and_encrypt, backup_path, enc_path)
            if enc_size > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"Shifrlangan zaxira fayli {enc_size / (1024 * 1024):.1f} MB — Telegram bot orqali "
                    f"yuborish chegarasi 50 MB. Zaxirani serverdagi data/backups papkasidan oling."
                )

            file_name = os.path.basename(enc_path)
            caption = (
                f"{SUCCESS} <b>Baza xavfsiz zaxira nusxasi tayyor!</b>\n\n"
                f"Fayl: <code>{html_escape(file_name)}</code>\n"
                f"Hajmi: <code>{enc_size / (1024 * 1024):.2f} MB</code>\n"
                f"Xavfsizlik: {LOCK_LOCKED} <b>Fernet (AES-128 + HMAC) bilan shifrlangan</b>\n\n"
                f"<i>Tiklash uchun: <code>python scripts/decrypt_backup.py {html_escape(file_name)} --extract</code></i>"
            )
            await event.bot.send_document(
                chat_id=event.from_user.id,
                document=FSInputFile(enc_path, filename=file_name),
                caption=caption,
                parse_mode="HTML",
                request_timeout=300
            )
            try:
                await wait_msg.delete()
            except Exception:
                logger.debug("Could not delete backup progress message", exc_info=True)
        except Exception as e:
            logger.error(f"Backup delivery failed: {e}", exc_info=True)
            try:
                await wait_msg.edit_text(f"{ERROR} Zaxira nusxani yuborib bo'lmadi: {html_escape(str(e)[:400])}", reply_markup=get_back_to_admin_keyboard())
            except Exception:
                logger.debug("Could not report backup failure", exc_info=True)
        finally:
            for p in (backup_path, enc_path):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except OSError:
                    logger.debug("Backup temp cleanup failed", exc_info=True)
