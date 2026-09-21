import logging
from typing import Optional, Tuple, List, Dict, Any
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from database.db_manager import db_manager
from admin_bot.keyboards.admin_keyboards import (
    get_bot_mode_keyboard,
    get_whitelist_pagination_keyboard,
    get_cancel_whitelist_keyboard,
    get_back_to_admin_keyboard
)
from services.custom_emojis import (
    LOCK_LOCKED, LOCK_UNLOCKED, SUCCESS, SUCCESS_V2, ERROR, WARN, INFO, STARS,
    USERS_GROUP, SHIELD, ADMIN, SUPPORT, KEY, VERIFIED, CALENDAR, clean_for_alert
)
from bot.utils import safe_answer, html_escape

logger = logging.getLogger(__name__)
router = Router(name="admin_access_control_router")

class AccessControlStates(StatesGroup):
    waiting_for_support_user = State()
    waiting_for_whitelist_user = State()

async def render_bot_mode_view() -> Tuple[str, InlineKeyboardMarkup]:
    is_private = await db_manager.is_private_mode()
    support_user = await db_manager.get_support_username()
    all_whitelisted = await db_manager.get_whitelisted_users()
    stars_users = [u for u in all_whitelisted if u.get("source") == "stars_50"]
    admin_users = [u for u in all_whitelisted if u.get("source") != "stars_50"]

    status_icon = LOCK_LOCKED if is_private else LOCK_UNLOCKED
    status_text = f"{ERROR} <b>Yopiq (Private Mode)</b>" if is_private else f"{SUCCESS} <b>Ommaviy (Public Mode)</b>"

    text = f"""
{status_icon} <b>BOT REJIMI VA KIRISH SOZLAMALARI</b>
━━━━━━━━━━━━━━━━━━━━━━━
├ {SHIELD} <b>Hozirgi Rejim:</b> {status_text}
├ {SUPPORT} <b>Admin Aloqa:</b> @{support_user}
├ {USERS_GROUP} <b>Whitelist Foydalanuvchilari:</b> <code>{len(all_whitelisted)}</code> ta
│  ├ {ADMIN} <b>Admin tomonidan:</b> <code>{len(admin_users)}</code> ta
│  └ {STARS} <b>50 Stars to'laganlar:</b> <code>{len(stars_users)}</code> ta
━━━━━━━━━━━━━━━━━━━━━━━
{INFO} <b>Qoidalar va Ruxsatlar:</b>
• {SUCCESS_V2} <b>Ommaviy (Public):</b> Har qanday foydalanuvchi bemalol kiradi.
• {LOCK_LOCKED} <b>Yopiq (Private):</b> Faqat ruxsat berilganlar ishlata oladi.
  - {VERIFIED} <i>Eski foydalanuvchilar va barcha amaldagi obunachilar to'xtovsiz foydalanishadi.</i>
  - {KEY} <i>Yangi foydalanuvchilar:</i> Admin bilan bog'lanishadi yoki {STARS} <b>50 Stars</b> to'lab 14 kunlik sinov bilan botni ochishadi.
"""
    kb = get_bot_mode_keyboard(is_private=is_private, support_username=support_user)
    return text, kb

def render_whitelist_text(users: List[Dict[str, Any]], page: int = 1, page_size: int = 5, source_filter: str = "") -> str:
    total_users = len(users)
    title = f"{STARS} <b>50 STARS ORQALI KIRGANLAR</b>" if source_filter == "stars_50" else f"{USERS_GROUP} <b>WHITELIST RO'YXATI</b>"
    if not users:
        return f"""
{title}
━━━━━━━━━━━━━━━━━━━━━━━
<i>Hozircha hech qanday foydalanuvchi yo'q.</i>
"""

    total_pages = max(1, (total_users + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * page_size
    page_items = users[start_idx:start_idx + page_size]

    lines = [
        f"{title} (Jami: <code>{total_users}</code> ta | Sahifa: <code>{page}/{total_pages}</code>)",
        "━━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for idx, u in enumerate(page_items, start_idx + 1):
        uid = u["user_id"]
        fname = html_escape(u.get("full_name") or "Noma'lum")
        uname = f"@{u['username']}" if u.get("username") else "—"
        src = f"{STARS} 50 Stars" if u.get("source") == "stars_50" else f"{ADMIN} Admin"
        created = str(u.get("created_at") or "")[:10]
        lines.append(f"<b>{idx}. {fname}</b> ({uname})")
        lines.append(f"   ├ {KEY} ID: <code>{uid}</code> | Manba: {src}")
        lines.append(f"   └ {CALENDAR} Sana: <code>{created}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{INFO} <i>Foydalanuvchi ruxsatini bekor qilish uchun pastdagi tugmani bosing:</i>")
    return "\n".join(lines)

@router.callback_query(F.data == "admin_bot_mode")
@router.message(Command("mode"))
@router.message(Command("access"))
async def cb_admin_bot_mode(event: CallbackQuery | Message, state: FSMContext):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
    await state.clear()
    text, kb = await render_bot_mode_view()
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "admin_toggle_bot_mode")
async def cb_toggle_bot_mode(callback: CallbackQuery):
    current = await db_manager.is_private_mode()
    new_mode = not current
    await db_manager.set_private_mode(new_mode)
    alert_msg = "Bot rejimi: Yopiq (Private) ga o'tkazildi!" if new_mode else "Bot rejimi: Ommaviy (Public) ga o'tkazildi!"
    await safe_answer(callback, alert_msg, show_alert=True)
    text, kb = await render_bot_mode_view()
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data == "admin_set_support_user")
async def cb_set_support_user(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AccessControlStates.waiting_for_support_user)
    current_sup = await db_manager.get_support_username()
    text = f"""
{INFO} <b>Admin aloqa username'ini o'zgartirish</b>

Hozirgi: @{current_sup}

Yangi <b>@username</b>ni yuboring (masalan: <code>@admin_support</code> yoki <code>admin_support</code>):
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_cancel_whitelist_keyboard())

@router.message(AccessControlStates.waiting_for_support_user)
async def process_support_user_input(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        text, kb = await render_bot_mode_view()
        await message.answer(f"{INFO} Amaliyot bekor qilindi.\n\n{text}", parse_mode="HTML", reply_markup=kb)
        return

    clean_text = message.text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if clean_text.startswith(prefix):
            clean_text = clean_text[len(prefix):]
    raw = clean_text.lstrip("@").strip()
    if not raw or len(raw) < 3 or " " in raw:
        await message.answer(
            f"{ERROR} <b>Noto'g'ri username formati!</b>\nIltimos, haqiqiy username yuboring (masalan: <code>@support_bot</code>):",
            parse_mode="HTML",
            reply_markup=get_cancel_whitelist_keyboard()
        )
        return

    await db_manager.set_support_username(raw)
    await state.clear()
    await message.answer(
        f"{SUCCESS} <b>Admin aloqa username muvaffaqiyatli saqlandi:</b> @{raw}",
        parse_mode="HTML"
    )
    text, kb = await render_bot_mode_view()
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "admin_whitelist_list")
async def cb_whitelist_list(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    users = await db_manager.get_whitelisted_users()
    text = render_whitelist_text(users, page=1, page_size=5, source_filter="")
    kb = get_whitelist_pagination_keyboard(users, page=1, page_size=5, source="")
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data == "admin_whitelist_stars")
async def cb_whitelist_stars(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    users = await db_manager.get_whitelisted_users(source="stars_50")
    text = render_whitelist_text(users, page=1, page_size=5, source_filter="stars_50")
    kb = get_whitelist_pagination_keyboard(users, page=1, page_size=5, source="stars_50")
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data.startswith("adm_wl_page_"))
async def cb_whitelist_page(callback: CallbackQuery):
    await safe_answer(callback)
    parts = callback.data.replace("adm_wl_page_", "").split("_")
    page = int(parts[0]) if parts[0].isdigit() else 1
    source = "_".join(parts[1:]) if len(parts) > 1 else ""

    users = await db_manager.get_whitelisted_users(source=source if source else None)
    text = render_whitelist_text(users, page=page, page_size=5, source_filter=source)
    kb = get_whitelist_pagination_keyboard(users, page=page, page_size=5, source=source)
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data.startswith("adm_wl_rm_"))
async def cb_whitelist_revoke(callback: CallbackQuery):
    try:
        target_uid = int(callback.data.replace("adm_wl_rm_", ""))
    except ValueError:
        await safe_answer(callback, "Noto'g'ri ID!", show_alert=True)
        return

    removed = await db_manager.remove_user_from_whitelist(target_uid)
    if removed:
        await safe_answer(callback, f"Foydalanuvchi ({target_uid}) whitelistdan muvaffaqiyatli o'chirildi!", show_alert=True)
    else:
        await safe_answer(callback, f"Foydalanuvchi ({target_uid}) topilmadi!", show_alert=True)

    users = await db_manager.get_whitelisted_users()
    text = render_whitelist_text(users, page=1, page_size=5, source_filter="")
    kb = get_whitelist_pagination_keyboard(users, page=1, page_size=5, source="")
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data == "admin_whitelist_add")
async def cb_whitelist_add(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AccessControlStates.waiting_for_whitelist_user)
    text = f"""
{INFO} <b>Foydalanuvchiga ruxsat berish (Whitelistga qo'shish)</b>

Foydalanuvchining <b>Telegram ID</b> raqamini yoki <b>@username</b>ini yuboring:

<i>Masalan: <code>123456789</code> yoki <code>@foydalanuvchi</code></i>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_cancel_whitelist_keyboard())

@router.message(AccessControlStates.waiting_for_whitelist_user)
async def process_whitelist_user_input(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        text, kb = await render_bot_mode_view()
        await message.answer(f"{INFO} Amaliyot bekor qilindi.\n\n{text}", parse_mode="HTML", reply_markup=kb)
        return

    raw = message.text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.strip()

    admin_id = message.from_user.id if message.from_user else 0

    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        target_uid = int(raw)
        await db_manager.get_or_create_user(user_id=target_uid, full_name=f"Foydalanuvchi {target_uid}")
        await db_manager.add_user_to_whitelist(
            user_id=target_uid,
            added_by=admin_id,
            source="admin",
            note="Admin tomonidan qo'shildi"
        )
        await state.clear()
        await message.answer(
            f"{SUCCESS} <b>Foydalanuvchi (ID: <code>{target_uid}</code>) whitelistga muvaffaqiyatli qo'shildi!</b>",
            parse_mode="HTML"
        )
        text, kb = await render_bot_mode_view()
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    # Process username
    clean_uname = raw.lstrip("@").lower()
    user_found = await db_manager.get_user_by_username(clean_uname)
    if user_found:
        await db_manager.add_user_to_whitelist(
            user_id=user_found.user_id,
            added_by=admin_id,
            source="admin",
            note=f"Admin qo'shdi (@{clean_uname})"
        )
        await state.clear()
        await message.answer(
            f"{SUCCESS} <b>@{clean_uname} (ID: <code>{user_found.user_id}</code>) whitelistga muvaffaqiyatli qo'shildi!</b>",
            parse_mode="HTML"
        )
        text, kb = await render_bot_mode_view()
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    await message.answer(
        f"{WARN} <b>@{clean_uname} bazada topilmadi.</b>\n\n"
        f"Foydalanuvchi hali botni ishga tushirmagan bo'lishi mumkin.\n"
        f"Iltimos, uning <b>Telegram ID</b> raqamini yuboring:",
        parse_mode="HTML",
        reply_markup=get_cancel_whitelist_keyboard()
    )
