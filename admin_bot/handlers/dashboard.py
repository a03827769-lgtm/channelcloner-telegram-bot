import asyncio
import logging
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
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
from admin_bot.ui import show_screen

logger = logging.getLogger(__name__)

_listener_restart_lock = asyncio.Lock()
_catchup_lock = asyncio.Lock()


async def get_dashboard_text() -> str:
    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()
    is_private = await db_manager.is_private_mode()
    mode_badge = f"{ERROR} Yopiq (Private)" if is_private else f"{SUCCESS} Ommaviy (Public)"
    status_icon = LOCK_LOCKED if is_private else LOCK_UNLOCKED

    if me:
        username = f"@{html_escape(me.username)}" if getattr(me, "username", None) else "username yo'q"
        telethon_status = f"{SUCCESS} Faol (<b>{html_escape(me.first_name or '')}</b>, {username})"
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


async def _edit_dashboard(callback: CallbackQuery, text: str, reply_markup) -> None:
    await show_screen(callback, text, reply_markup)


async def cmd_admin_start(message: Message, state: FSMContext):
    await state.clear()
    me = await telethon_listener.get_me()
    is_private = await db_manager.is_private_mode()

    await message.answer(
        text=f"{CROWN} <b>Super Admin Paneliga xush kelibsiz!</b>",
        parse_mode="HTML",
        reply_markup=get_admin_reply_keyboard()
    )
    await message.answer(
        text=await get_dashboard_text(),
        parse_mode="HTML",
        reply_markup=get_admin_dashboard_keyboard(is_auth=me is not None, is_private=is_private)
    )


async def cb_admin_dashboard(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    me = await telethon_listener.get_me()
    is_private = await db_manager.is_private_mode()
    await _edit_dashboard(
        callback,
        await get_dashboard_text(),
        get_admin_dashboard_keyboard(is_auth=me is not None, is_private=is_private)
    )


async def cb_restart_listener(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if _listener_restart_lock.locked():
        await safe_answer(callback, "MTProto qayta ulanishi allaqachon davom etmoqda...", show_alert=True)
        return
    await safe_answer(callback, "MTProto qayta ishga tushirilmoqda...")
    async with _listener_restart_lock:
        try:
            if not telethon_listener.is_connected():
                await telethon_listener.start()
            else:
                telethon_listener.invalidate_pairs_cache()
                await telethon_listener.refresh_monitored_channels()
        except Exception as e:
            logger.error(f"Listener restart error: {e}", exc_info=True)

        connected = telethon_listener.is_connected()
        if connected:
            header = f"{SUCCESS} <b>MTProto tinglovchi faol va kanallar sinxronlandi.</b>"
        else:
            header = (
                f"{ERROR} <b>MTProto tinglovchi ulanmadi.</b>\n"
                f"<i>Hisob ulanmagan bo'lishi yoki Telegram bilan aloqa yo'qligi mumkin. MTProto Hisob bo'limini tekshiring.</i>"
            )
        is_private = await db_manager.is_private_mode()
        await _edit_dashboard(
            callback,
            f"{header}\n{await get_dashboard_text()}",
            get_admin_dashboard_keyboard(is_auth=connected, is_private=is_private)
        )


async def cmd_admin_catchup(message: Message):
    """Admin command to trigger gap catch-up across all active channels in the system"""
    if not telethon_listener.is_connected():
        await message.answer(f"{WARN} <b>Telethon MTProto ulanmagan!</b>", parse_mode="HTML")
        return
    if _catchup_lock.locked():
        await message.answer(f"{INFO} Catch-Up allaqachon davom etmoqda. Tugashini kuting.", parse_mode="HTML")
        return

    async with _catchup_lock:
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
            logger.error(f"Global catchup error: {e}", exc_info=True)
            await status_msg.edit_text(f"{ERROR} Catch-Up xatolik bilan to'xtadi: <code>{html_escape(str(e)[:300])}</code>", parse_mode="HTML")


async def cmd_admin_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = f"""
{SHIELD} <b>ADMIN BUYRUQLAR RO'YXATI:</b>
━━━━━━━━━━━━━━━━━━━━━━━
├ <code>/admin</code> yoki <code>/start</code> — Asosiy boshqaruv markazi
├ <code>/status</code> — Tizim va server holati
├ <code>/logs</code> — Jonli loglar (oxirgi 30 ta qator, faqat Super Admin)
├ <code>/users</code> — Foydalanuvchilar va obunalar ro'yxati
├ <code>/backup</code> — Bazaning shifrlangan zaxira nusxasi (faqat Super Admin)
├ <code>/mode</code> yoki <code>/access</code> — Bot kirish rejimi (Public/Private) va Whitelist
├ <code>/catchup</code> — Barcha kanallar uchun majburiy oflayn yangilash (Catch-Up)
├ <code>/check_origin</code> — Rasmdagi ko'rinmas mualliflik belgisini tekshirish
└ <code>/cancel</code> — Joriy jarayonni bekor qilish
━━━━━━━━━━━━━━━━━━━━━━━
<i>MTProto hisob, xabar tarqatish, zaxira nusxa, bot rejimi va tariflar faqat Super Adminlar uchun.</i>
"""
    await message.answer(text=help_text, parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())


async def cmd_admin_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    if message.from_user:
        await telethon_listener.cancel_login(message.from_user.id)
    if current_state:
        await message.answer(f"{INFO} Joriy amal bekor qilindi.", reply_markup=get_admin_reply_keyboard())
    else:
        await message.answer(f"{INFO} Hech qanday faol jarayon yo'q.", reply_markup=get_admin_reply_keyboard())


async def cb_admin_noop(callback: CallbackQuery):
    await safe_answer(callback)
