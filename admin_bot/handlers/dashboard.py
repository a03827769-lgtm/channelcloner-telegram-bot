import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from admin_bot.keyboards.admin_keyboards import (
    get_admin_dashboard_keyboard,
    get_admin_reply_keyboard,
    get_back_to_admin_keyboard
)
from services.telethon_listener import telethon_listener
from database.db_manager import db_manager
from services.custom_emojis import CROWN, TELEGRAM, SUCCESS, WARN, INFO, STARS, USERS_GROUP, LINK, ROCKET, REFRESH, ERROR

logger = logging.getLogger(__name__)
router = Router(name="admin_dashboard_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

async def get_dashboard_text() -> str:
    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()
    
    if me:
        telethon_status = f"{SUCCESS} Faol (<b>{me.first_name}</b>, @{me.username or 'mavjud_emas'})"
    else:
        telethon_status = f"{WARN} Ulanmagan"

    return f"""
{CROWN} <b>SUPER ADMIN BOSHQARUV MARKAZI</b>
───────────────────────────
├ {TELEGRAM} <b>Markaziy MTProto:</b> {telethon_status}
├ {USERS_GROUP} <b>Foydalanuvchilar:</b> <code>{stats['total_users']}</code> nafar
├ {LINK} <b>Ulangan Kanallar:</b> <code>{stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol Klonlash:</b> <code>{stats['active_pairs']}</code> ta
├ {ROCKET} <b>Ko'chirilgan Postlar:</b> <code>{stats['total_cloned_messages']}</code> ta
└ {STARS} <b>Jami Stars Tushumi:</b> <code>{stats['total_stars_earned']}</code> Stars
───────────────────────────
<i>Barcha boshqaruv amallari faqat ushbu bot orqali xavfsiz amalga oshiriladi.</i>
"""

@router.message(CommandStart())
@router.message(Command("admin"))
@router.message(F.text.contains("Boshqaruv Paneli"))
async def cmd_admin_start(message: Message, state: FSMContext):
    await state.clear()
    me = await telethon_listener.get_me()
    is_auth = me is not None

    await message.answer(
        text=f"{CROWN} <b>Super Admin Paneliga xush kelibsiz!</b>",
        parse_mode="HTML",
        reply_markup=get_admin_reply_keyboard()
    )

    text = await get_dashboard_text()
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=get_admin_dashboard_keyboard(is_auth=is_auth)
    )

@router.callback_query(F.data == "admin_main_dashboard")
async def cb_admin_dashboard(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    me = await telethon_listener.get_me()
    is_auth = me is not None

    text = await get_dashboard_text()
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_admin_dashboard_keyboard(is_auth=is_auth)
    )

@router.callback_query(F.data == "admin_restart_listener")
async def cb_restart_listener(callback: CallbackQuery):
    await safe_answer(callback, "MTProto qayta ishga tushirilmoqda...", show_alert=True)
    try:
        await telethon_listener.refresh_monitored_channels()
        # BUG #72 fix: await inside f-string is a SyntaxError — get text first
        dashboard_text = await get_dashboard_text()
        await callback.message.edit_text(
            text=f"{SUCCESS} <b>MTProto Tinglovchi barcha kanallar bilan muvaffaqiyatli sinxronlandi!</b>\n\n{dashboard_text}",
            parse_mode="HTML",
            reply_markup=get_admin_dashboard_keyboard(is_auth=telethon_listener.is_connected())
        )
    except Exception as e:
        logger.error(f"Listener restart error: {e}")
        await callback.message.answer(f"{ERROR} Xatolik: {e}")
