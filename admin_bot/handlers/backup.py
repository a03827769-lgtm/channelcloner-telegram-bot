import os
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from services.custom_emojis import SAVE_BACKUP, SUCCESS, ERROR, LOADING

logger = logging.getLogger(__name__)
router = Router(name="admin_backup_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

@router.callback_query(F.data == "admin_download_backup")
@router.message(F.text.contains("Baza Nusxasi"))
async def cb_download_backup(event: CallbackQuery | Message):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        wait_msg = await event.message.answer(f"{LOADING} Ma'lumotlar bazasining zaxira nusxasi (snapshot backup) tayyorlanmoqda...")
    else:
        wait_msg = await event.answer(f"{LOADING} Ma'lumotlar bazasining zaxira nusxasi (snapshot backup) tayyorlanmoqda...")

    backup_path = await db_manager.create_backup_file()

    if not backup_path or not os.path.exists(backup_path):
        await wait_msg.edit_text(f"{ERROR} Baza zaxira nusxasini yaratishda xatolik yuz berdi!", reply_markup=get_back_to_admin_keyboard())
        return

    try:
        doc = FSInputFile(backup_path, filename=os.path.basename(backup_path))
        chat_id = event.from_user.id
        if isinstance(event, CallbackQuery):
            await event.bot.send_document(
                chat_id=chat_id,
                document=doc,
                caption=f"{SUCCESS} <b>Baza zaxira nusxasi tayyor!</b>\n\nFayl: <code>{os.path.basename(backup_path)}</code>\nSana: Real-vaqt snapshot",
                parse_mode="HTML"
            )
        else:
            await event.answer_document(
                document=doc,
                caption=f"{SUCCESS} <b>Baza zaxira nusxasi tayyor!</b>\n\nFayl: <code>{os.path.basename(backup_path)}</code>\nSana: Real-vaqt snapshot",
                parse_mode="HTML"
            )
        await wait_msg.delete()
    except Exception as e:
        logger.error(f"Error sending backup document: {e}")
        await wait_msg.edit_text(f"{ERROR} Faylni yuborishda xatolik: {e}", reply_markup=get_back_to_admin_keyboard())
    finally:
        # Cleanup temporary backup file after sending
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass
