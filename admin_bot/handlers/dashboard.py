import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from admin_bot.keyboards.admin_keyboards import (
    get_admin_dashboard_keyboard,
    get_admin_reply_keyboard,
    get_back_to_admin_keyboard
)
from services.telethon_listener import telethon_listener
from database.db_manager import db_manager
from services.custom_emojis import (
    CROWN, TELEGRAM, SUCCESS, WARN, INFO, STARS, USERS_GROUP, LINK, ROCKET,
    REFRESH, ERROR, LOCK_LOCKED, LOCK_UNLOCKED, SHIELD
)

from bot.utils import safe_answer, html_escape

logger = logging.getLogger(__name__)
router = Router(name="admin_dashboard_router")

async def get_dashboard_text() -> str:
    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()
    is_private = await db_manager.is_private_mode()
    mode_badge = f"{ERROR} Yopiq (Private)" if is_private else f"{SUCCESS} Ommaviy (Public)"
    status_icon = LOCK_LOCKED if is_private else LOCK_UNLOCKED
    
    if me:
        telethon_status = f"{SUCCESS} Faol (<b>{html_escape(me.first_name or '')}</b>, @{me.username or 'mavjud_emas'})"
    else:
        telethon_status = f"{WARN} Ulanmagan"

    return f"""
{CROWN} <b>SUPER ADMIN BOSHQARUV MARKAZI</b>
━━━━━━━━━━━━━━━━━━━━━━━
├ {TELEGRAM} <b>Markaziy MTProto:</b> {telethon_status}
├ {status_icon} <b>Bot Rejimi:</b> {mode_badge}
├ {USERS_GROUP} <b>Foydalanuvchilar:</b> <code>{stats['total_users']}</code> nafar
├ {LINK} <b>Ulangan Kanallar:</b> <code>{stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol Klonlash:</b> <code>{stats['active_pairs']}</code> ta
├ {ROCKET} <b>Ko'chirilgan Postlar:</b> <code>{stats['total_cloned_messages']}</code> ta
└ {STARS} <b>Jami Stars Tushumi:</b> <code>{stats['total_stars_earned']}</code> Stars
━━━━━━━━━━━━━━━━━━━━━━━
<i>Barcha boshqaruv amallari faqat ushbu bot orqali xavfsiz amalga oshiriladi.</i>
"""

@router.message(CommandStart())
@router.message(Command("admin"))
@router.message(F.text.contains("Boshqaruv Paneli"))
async def cmd_admin_start(message: Message, state: FSMContext):
    await state.clear()
    me = await telethon_listener.get_me()
    is_auth = me is not None
    is_private = await db_manager.is_private_mode()

    await message.answer(
        text=f"{CROWN} <b>Super Admin Paneliga xush kelibsiz!</b>",
        parse_mode="HTML",
        reply_markup=get_admin_reply_keyboard()
    )

    text = await get_dashboard_text()
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=get_admin_dashboard_keyboard(is_auth=is_auth, is_private=is_private)
    )

@router.callback_query(F.data == "admin_main_dashboard")
async def cb_admin_dashboard(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    me = await telethon_listener.get_me()
    is_auth = me is not None
    is_private = await db_manager.is_private_mode()

    text = await get_dashboard_text()
    try:
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_admin_dashboard_keyboard(is_auth=is_auth, is_private=is_private)
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data == "admin_restart_listener")
async def cb_restart_listener(callback: CallbackQuery):
    await safe_answer(callback, "MTProto qayta ishga tushirilmoqda...", show_alert=True)
    try:
        if not telethon_listener.is_connected():
            await telethon_listener.start()
        else:
            await telethon_listener.refresh_monitored_channels()
        dashboard_text = await get_dashboard_text()
        is_private = await db_manager.is_private_mode()
        try:
            await callback.message.edit_text(
                text=f"{SUCCESS} <b>MTProto Tinglovchi muvaffaqiyatli sinxronlandi!</b>\n\n{dashboard_text}",
                parse_mode="HTML",
                reply_markup=get_admin_dashboard_keyboard(is_auth=telethon_listener.is_connected(), is_private=is_private)
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    except Exception as e:
        logger.error(f"Listener restart error: {e}")
        await callback.message.answer(f"{ERROR} Xatolik: {e}")

@router.message(Command("catchup"))
async def cmd_admin_catchup(message: Message):
    """Admin command to trigger gap catchup across all active channels in the system"""
    if not telethon_listener.is_connected():
        await message.answer(f"{WARN} <b>Telethon MTProto ulanmagan!</b>", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"{REFRESH} <b>Barcha faol kanallar uchun oflayn yetkazish (Catch-Up) boshlandi...</b>",
        parse_mode="HTML"
    )

    try:
        results = await telethon_listener.catch_up_all_active_pairs()
        total_caught = sum(r.get("caught_up", 0) for r in results.values() if isinstance(r, dict))
        await status_msg.edit_text(
            f"{SUCCESS} <b>Global Catch-Up yakunlandi!</b>\n\n"
            f"Jami tekshirilgan juftliklar: <b>{len(results)} ta</b>\n"
            f"Yetkazilgan yangi postlar: <b>{total_caught} ta</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Global catchup error: {e}")
        await status_msg.edit_text(f"{ERROR} Xatolik yuz berdi: {e}", parse_mode="HTML")

@router.message(Command("help"))
async def cmd_admin_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = f"""
{SHIELD} <b>SUPER ADMIN BUYRUQLAR RO'YXATI:</b>
━━━━━━━━━━━━━━━━━━━━━━━
├ <code>/admin</code> yoki <code>/start</code> — Asosiy boshqaruv markazi
├ <code>/status</code> — Tizim va server holati (RAM, CPU, 24/7)
├ <code>/logs</code> — Jonli loglarni ko'rish (oxirgi 30 ta qator)
├ <code>/users</code> — Foydalanuvchilar va obunalar ro'yxati
├ <code>/backup</code> — Bazaning AES-128 shifrlangan zaxira nusxasi
├ <code>/mode</code> yoki <code>/access</code> — Bot kirish rejimi (Public/Private) va Whitelist
├ <code>/catchup</code> — Barcha kanallar uchun majburiy oflayn yangilash (Catch-Up)
├ <code>/check_origin</code> — Rasmdagi ko'rinmas steganografiya mualliflik belgisini tekshirish
└ <code>/cancel</code> — Har qanday joriy jarayonni bekor qilish
━━━━━━━━━━━━━━━━━━━━━━━
<i>Barcha buyruqlar faqat Super Adminlar uchun faol!</i>
"""
    await message.answer(text=help_text, parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())

@router.message(Command("cancel"))
@router.message(F.text.lower() == "bekor qilish")
async def cmd_admin_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    if current_state:
        await message.answer(f"{INFO} Joriy amal bekor qilindi.", reply_markup=get_admin_reply_keyboard())
    else:
        await message.answer(f"{INFO} Hech qanday faol jarayon yo'q.", reply_markup=get_admin_reply_keyboard())

@router.callback_query(F.data == "noop")
async def cb_admin_noop(callback: CallbackQuery):
    await safe_answer(callback)
