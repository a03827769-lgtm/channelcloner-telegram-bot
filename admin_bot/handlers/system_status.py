import logging
import platform
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    INFO, SERVICE_24_7, TELEGRAM, SUCCESS, WARN, USERS_GROUP, LINK,
    ROCKET, STARS, BROADCAST, SETTINGS, DOCUMENT
)

logger = logging.getLogger(__name__)
router = Router(name="admin_status_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

@router.callback_query(F.data == "admin_system_status")
@router.message(F.text.contains("Tizim Holati"))
async def cb_admin_system_status(event: CallbackQuery | Message):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()
    
    if me:
        mtproto_status = f"{SUCCESS} Faol ({me.first_name})"
    else:
        mtproto_status = f"{WARN} Ulanmagan"

    # System metrics
    os_name = f"{platform.system()} {platform.release()}"
    python_ver = platform.python_version()

    text = f"""
{INFO} <b>TIZIM HOLATI VA SERVER MONITORINGI:</b>
───────────────────────────
├ {SERVICE_24_7} <b>Bot API:</b> {SUCCESS} 24/7 Faol
├ {TELEGRAM} <b>MTProto Tinglovchi:</b> {mtproto_status}
├ {SETTINGS} <b>Operatsion Tizim:</b> <code>{os_name}</code>
├ {DOCUMENT} <b>Python Versiyasi:</b> <code>{python_ver}</code>
├ {USERS_GROUP} <b>Jami Foydalanuvchilar:</b> <code>{stats['total_users']}</code> nafar
├ {LINK} <b>Jami Ulangan Kanallar:</b> <code>{stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol Ishlayotgan Kanallar:</b> <code>{stats['active_pairs']}</code> ta
├ {ROCKET} <b>Ko'chirilgan Postlar:</b> <code>{stats['total_cloned_messages']}</code> ta
└ {STARS} <b>Jami Stars Tushumi:</b> <code>{stats['total_stars_earned']}</code> Stars
───────────────────────────
<i>Barcha jarayonlar 100k yuqori yuklamaga moslashtirilgan WAL rejimida ishlamoqda.</i>
"""
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
