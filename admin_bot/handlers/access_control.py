import logging
import re
from typing import Tuple, List, Dict, Any
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db_manager import db_manager
from admin_bot.keyboards.admin_keyboards import (
    get_bot_mode_keyboard,
    get_whitelist_pagination_keyboard,
    get_cancel_whitelist_keyboard
)
from services.custom_emojis import (
    LOCK_LOCKED, LOCK_UNLOCKED, SUCCESS, SUCCESS_V2, ERROR, WARN, INFO, STARS,
    USERS_GROUP, SHIELD, ADMIN, SUPPORT, KEY, VERIFIED, CALENDAR
)
from admin_bot.permissions import ensure_super_admin
from bot.utils import safe_answer, html_escape
from admin_bot.ui import is_cancel_text, show_screen

logger = logging.getLogger(__name__)

# Telegram usernames: 5-32 characters, latin letters, digits and underscores, starting with a letter.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MAX_TELEGRAM_USER_ID = 2 ** 53


async def _edit_screen(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    await show_screen(callback, text, kb)

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
├ {SUPPORT} <b>Admin Aloqa:</b> @{html_escape(support_user)}
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

async def cb_admin_bot_mode(event: CallbackQuery | Message, state: FSMContext):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
    await state.clear()
    text, kb = await render_bot_mode_view()
    if isinstance(event, CallbackQuery):
        await _edit_screen(event, text, kb)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

async def cb_toggle_bot_mode(callback: CallbackQuery):
    if not await ensure_super_admin(callback):
        return
    current = await db_manager.is_private_mode()
    new_mode = not current
    await db_manager.set_private_mode(new_mode)
    alert_msg = "Bot rejimi: Yopiq (Private) ga o'tkazildi!" if new_mode else "Bot rejimi: Ommaviy (Public) ga o'tkazildi!"
    await safe_answer(callback, alert_msg, show_alert=True)
    text, kb = await render_bot_mode_view()
    await _edit_screen(callback, text, kb)

async def cb_set_support_user(callback: CallbackQuery, state: FSMContext):
    if not await ensure_super_admin(callback):
        return
    await safe_answer(callback)
    await state.set_state(AccessControlStates.waiting_for_support_user)
    current_sup = await db_manager.get_support_username()
    text = f"""
{INFO} <b>Admin aloqa username'ini o'zgartirish</b>

Hozirgi: @{html_escape(current_sup)}

Yangi <b>@username</b>ni yuboring (masalan: <code>@admin_support</code> yoki <code>admin_support</code>):
"""
    await _edit_screen(callback, text, get_cancel_whitelist_keyboard())

async def process_support_user_input(message: Message, state: FSMContext):
    if not await ensure_super_admin(message):
        await state.clear()
        return
    if not message.text or is_cancel_text(message):
        await state.clear()
        text, kb = await render_bot_mode_view()
        await message.answer(f"{INFO} Amaliyot bekor qilindi.\n\n{text}", parse_mode="HTML", reply_markup=kb)
        return

    clean_text = message.text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if clean_text.lower().startswith(prefix):
            clean_text = clean_text[len(prefix):]
            break
    raw = clean_text.lstrip("@").strip()
    if not _USERNAME_RE.fullmatch(raw):
        await message.answer(
            f"{ERROR} <b>Noto'g'ri username formati!</b>\nUsername 5-32 ta lotin harfi, raqam yoki _ belgidan iborat bo'lishi va harf bilan boshlanishi kerak (masalan: <code>@support_bot</code>):",
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

async def cb_whitelist_list(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    users = await db_manager.get_whitelisted_users()
    text = render_whitelist_text(users, page=1, page_size=5, source_filter="")
    kb = get_whitelist_pagination_keyboard(users, page=1, page_size=5, source="")
    await _edit_screen(callback, text, kb)

async def cb_whitelist_stars(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    users = await db_manager.get_whitelisted_users(source="stars_50")
    text = render_whitelist_text(users, page=1, page_size=5, source_filter="stars_50")
    kb = get_whitelist_pagination_keyboard(users, page=1, page_size=5, source="stars_50")
    await _edit_screen(callback, text, kb)

async def cb_whitelist_page(callback: CallbackQuery):
    await safe_answer(callback)
    parts = callback.data.replace("adm_wl_page_", "").split("_")
    page = int(parts[0]) if parts[0].isdigit() else 1
    source = "_".join(parts[1:]) if len(parts) > 1 else ""
    if source not in ("", "stars_50", "admin"):
        source = ""

    users = await db_manager.get_whitelisted_users(source=source if source else None)
    text = render_whitelist_text(users, page=page, page_size=5, source_filter=source)
    kb = get_whitelist_pagination_keyboard(users, page=page, page_size=5, source=source)
    await _edit_screen(callback, text, kb)

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
    await _edit_screen(callback, text, kb)

async def cb_whitelist_add(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AccessControlStates.waiting_for_whitelist_user)
    text = f"""
{INFO} <b>Foydalanuvchiga ruxsat berish (Whitelistga qo'shish)</b>

Foydalanuvchining <b>Telegram ID</b> raqamini yoki <b>@username</b>ini yuboring:

<i>Masalan: <code>123456789</code> yoki <code>@foydalanuvchi</code></i>
"""
    await _edit_screen(callback, text, get_cancel_whitelist_keyboard())

async def process_whitelist_user_input(message: Message, state: FSMContext):
    if not message.text or is_cancel_text(message):
        await state.clear()
        text, kb = await render_bot_mode_view()
        await message.answer(f"{INFO} Amaliyot bekor qilindi.\n\n{text}", parse_mode="HTML", reply_markup=kb)
        return

    raw = message.text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip()

    admin_id = message.from_user.id if message.from_user else 0

    if raw.lstrip("-").isdigit():
        target_uid = int(raw)
        if not 0 < target_uid < MAX_TELEGRAM_USER_ID:
            await message.answer(
                f"{ERROR} <b>Noto'g'ri foydalanuvchi ID!</b>\nFoydalanuvchi ID musbat son bo'lishi kerak (kanal yoki guruh ID emas).",
                parse_mode="HTML",
                reply_markup=get_cancel_whitelist_keyboard()
            )
            return
        # Only create a placeholder row for unknown users; never overwrite a known user's real name.
        if not await db_manager.get_user_by_id(target_uid):
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
    if not _USERNAME_RE.fullmatch(clean_uname):
        await message.answer(
            f"{ERROR} <b>Noto'g'ri username yoki ID formati!</b>\nTelegram ID raqamini yoki @username ni yuboring:",
            parse_mode="HTML",
            reply_markup=get_cancel_whitelist_keyboard()
        )
        return
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
        f"{WARN} <b>@{html_escape(clean_uname)} bazada topilmadi.</b>\n\n"
        f"Foydalanuvchi hali botni ishga tushirmagan bo'lishi mumkin.\n"
        f"Iltimos, uning <b>Telegram ID</b> raqamini yuboring:",
        parse_mode="HTML",
        reply_markup=get_cancel_whitelist_keyboard()
    )
