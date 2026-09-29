import logging
from datetime import datetime, timezone
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard, get_revoke_confirm_keyboard
from admin_bot.permissions import ensure_super_admin, is_super_admin
from admin_bot.public_bot import get_public_bot, close_fallback_public_bot
from database.db_manager import db_manager
from config.settings import settings
from services.custom_emojis import (
    USERS_GROUP, CROWN, STARS, SUCCESS, ERROR, WARN, SEARCH, INFO,
    USER_PROFILE, MEDAL_BRONZE, STAR_SPARKLE, CALENDAR,
    ID_CROWN, ID_STARS, ID_ERROR, ID_SEARCH,
    ID_USER, ID_SETTINGS, ID_BACK, ID_DIAMOND, ID_DOCUMENT, ID_FORWARD
)

from bot.utils import format_uz_date, safe_answer, html_escape, parse_callback_id
from admin_bot.ui import is_cancel_text, show_screen

logger = logging.getLogger(__name__)

USERS_PAGE_SIZE = 10
MAX_TELEGRAM_USER_ID = 2 ** 53  # Telegram user ids fit in 52 bits
GRANTABLE_TIERS = {"pro", "vip"}
GRANTABLE_DAYS = {30, 90, 365, 3650}


class AdminUserSG(StatesGroup):
    waiting_for_user_query = State()


def _tier_badge(tier: str) -> str:
    if tier == "vip":
        return f"{CROWN} VIP"
    if tier == "pro":
        return f"{STARS} Pro"
    return f"{MEDAL_BRONZE} Sinov"


async def _edit_or_answer(event: CallbackQuery | Message, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    await show_screen(event, text, reply_markup)


async def cb_users_list(event: CallbackQuery | Message, state: FSMContext = None):
    if state:
        await state.clear()
    page = 1
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        if event.data and event.data.startswith("admin_users_page_"):
            page = max(1, parse_callback_id(event.data))

    users, total_users = await db_manager.get_users_detailed_page(offset=(page - 1) * USERS_PAGE_SIZE, limit=USERS_PAGE_SIZE)
    total_pages = max(1, (total_users + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    if page > total_pages and total_users:
        page = total_pages
        users, total_users = await db_manager.get_users_detailed_page(offset=(page - 1) * USERS_PAGE_SIZE, limit=USERS_PAGE_SIZE)

    search_row = [InlineKeyboardButton(
        text="ID / @Username orqali qidirish",
        icon_custom_emoji_id=ID_SEARCH,
        style="primary",
        callback_data="adm_search_user"
    )]
    back_row = [InlineKeyboardButton(
        text="Boshqaruv Paneliga Qaytish",
        icon_custom_emoji_id=ID_SETTINGS,
        style="danger",
        callback_data="admin_main_dashboard"
    )]

    if not users:
        text = f"{WARN} Bazada hali foydalanuvchilar yo'q."
        kb = InlineKeyboardMarkup(inline_keyboard=[search_row, back_row])
    else:
        start_idx = (page - 1) * USERS_PAGE_SIZE
        text = (
            f"{USERS_GROUP} <b>Foydalanuvchilar va Obunalar Ro'yxati "
            f"({start_idx + 1}-{start_idx + len(users)}/{total_users}):</b>\n\n"
        )
        kb_buttons = [search_row]
        for idx, u in enumerate(users, start_idx + 1):
            username_str = f"@{html_escape(u['username'])}" if u.get("username") else f"ID:{u['user_id']}"
            user_name_val = u.get("full_name") or "Foydalanuvchi"
            text += (
                f"<b>{idx}.</b> {html_escape(user_name_val)} ({username_str}) — "
                f"{_tier_badge(u.get('tier') or 'free')} | Kanallari: {u.get('channel_count', 0)} ta\n"
            )
            kb_buttons.append([InlineKeyboardButton(
                text=f"{user_name_val[:14]} ({(u.get('tier') or 'free').upper()})",
                icon_custom_emoji_id=ID_USER,
                style="primary",
                callback_data=f"adm_user_{u['user_id']}"
            )])

        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append(InlineKeyboardButton(text="Oldingi", icon_custom_emoji_id=ID_BACK, style="primary", callback_data=f"admin_users_page_{page - 1}"))
            nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", icon_custom_emoji_id=ID_DOCUMENT, style="primary", callback_data="noop"))
            if page < total_pages:
                nav_row.append(InlineKeyboardButton(text="Keyingi", icon_custom_emoji_id=ID_FORWARD, style="primary", callback_data=f"admin_users_page_{page + 1}"))
            kb_buttons.append(nav_row)
        kb_buttons.append(back_row)
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await _edit_or_answer(event, text, kb)


async def cb_start_user_search(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AdminUserSG.waiting_for_user_query)
    text = f"""
{SEARCH} <b>Foydalanuvchini Qidirish:</b>

Telegram <code>User ID</code> raqamini, <code>@username</code> yoki ism bo'yicha qidiring.
<i>Misol:</i> <code>7770001</code> yoki <code>@durov</code>
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Bekor qilish",
            icon_custom_emoji_id=ID_ERROR,
            style="danger",
            callback_data="admin_users_list"
        )]
    ])
    await _edit_or_answer(callback, text, kb)


async def process_user_search_query(message: Message, state: FSMContext):
    if not message.text or is_cancel_text(message):
        await state.clear()
        await message.answer(f"{INFO} Qidirish bekor qilindi.", parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())
        return

    query = message.text.strip()[:64]
    await state.clear()

    clean_q = query
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if clean_q.lower().startswith(prefix):
            clean_q = clean_q[len(prefix):]
            break
    clean_q = clean_q.lstrip("@").strip()

    retry_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Qayta qidirish", icon_custom_emoji_id=ID_SEARCH, style="primary", callback_data="adm_search_user")],
        [InlineKeyboardButton(text="Ro'yxatga qaytish", icon_custom_emoji_id=ID_BACK, style="danger", callback_data="admin_users_list")]
    ])

    if not clean_q:
        await message.answer(f"{ERROR} Qidiruv so'rovi bo'sh.", parse_mode="HTML", reply_markup=retry_kb)
        return

    if clean_q.isdigit():
        target_uid = int(clean_q)
        user = await db_manager.get_user_by_id(target_uid) if 0 < target_uid < MAX_TELEGRAM_USER_ID else None
        if not user:
            await message.answer(
                f"{ERROR} <b>ID bo'yicha foydalanuvchi topilmadi:</b> <code>{html_escape(clean_q)}</code>\n\n"
                f"Ushbu foydalanuvchi hali botdan ro'yxatdan o'tmagan.",
                parse_mode="HTML",
                reply_markup=retry_kb
            )
            return
        await render_user_detail_screen(target_uid, message)
        return

    results = await db_manager.search_users(clean_q)
    if not results:
        await message.answer(
            f"{ERROR} <b>Foydalanuvchi topilmadi:</b> <code>{html_escape(clean_q)}</code>\n\n"
            f"Iltimos, Telegram User ID raqamini kiritib ko'ring.",
            parse_mode="HTML",
            reply_markup=retry_kb
        )
        return

    if len(results) == 1:
        await render_user_detail_screen(results[0]["user_id"], message)
        return

    text = f"{USERS_GROUP} <b>Topilgan foydalanuvchilar ({len(results)} ta):</b>\n\n"
    kb_buttons = []
    for u in results:
        uname = f"@{u['username']}" if u.get("username") else f"ID:{u['user_id']}"
        full_name = u.get("full_name") or "Foydalanuvchi"
        text += f"• <b>{html_escape(full_name)}</b> ({html_escape(uname)}) — {_tier_badge(u.get('tier') or 'free')}\n"
        kb_buttons.append([InlineKeyboardButton(
            text=f"{full_name[:24]} ({uname})",
            icon_custom_emoji_id=ID_USER,
            style="primary",
            callback_data=f"adm_user_{u['user_id']}"
        )])
    kb_buttons.append([InlineKeyboardButton(text="Orqaga", icon_custom_emoji_id=ID_BACK, style="danger", callback_data="admin_users_list")])
    await message.answer(text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))


async def render_user_detail_screen(user_id: int, event: CallbackQuery | Message):
    user = await db_manager.get_user_stats(user_id)
    sub = user["subscription"]
    is_primary_super = bool(settings.PRIMARY_SUPER_ADMIN_ID) and user_id == settings.PRIMARY_SUPER_ADMIN_ID
    is_super = is_super_admin(user_id)
    is_adm = is_super or await db_manager.is_admin(user_id)

    tier_label = f"{CROWN} VIP" if sub.tier == "vip" else (f"{STARS} Pro" if sub.tier == "pro" else "Free (Sinov)")
    exp_date = sub.expires_at[:10] if sub.expires_at else (sub.trial_expires_at[:10] if sub.trial_expires_at else "Mavjud emas")
    admin_status_label = "👑 Asosiy Bosh Admin" if is_primary_super else ("👑 Super Admin" if is_super else ("🛡 Admin" if is_adm else "Foydalanuvchi"))

    text = f"""
{USER_PROFILE} <b>Foydalanuvchi Tafsilotlari & Obuna Boshqaruvi:</b>
───────────────────────────
├ <b>User ID:</b> <code>{user_id}</code>
├ <b>Rol:</b> <code>{admin_status_label}</code>
├ <b>Obuna Tarifi:</b> {tier_label}
├ <b>Muddati:</b> <code>{html_escape(exp_date)}</code>
├ <b>Ulangan Kanallar:</b> <code>{user['total_pairs']}</code> ta
├ <b>Faol Kanallar:</b> <code>{user['active_pairs']}</code> ta
├ <b>Jami Ko'chirgan Postlari:</b> <code>{user['total_cloned']}</code> ta
└ <b>Bugun Ko'chirgan Postlari:</b> <code>{user['today_cloned']}</code> ta
───────────────────────────
<i>Obuna muddatini uzaytirish yoki bekor qilish faqat Super Admin uchun.</i>
"""
    kb_rows = [
        [
            InlineKeyboardButton(text="+ 30 kun VIP", icon_custom_emoji_id=ID_CROWN, style="success", callback_data=f"adm_grant_{user_id}_vip_30"),
            InlineKeyboardButton(text="+ 30 kun PRO", icon_custom_emoji_id=ID_STARS, style="primary", callback_data=f"adm_grant_{user_id}_pro_30")
        ],
        [
            InlineKeyboardButton(text="+ 90 kun VIP (3 oy)", icon_custom_emoji_id=ID_CROWN, style="success", callback_data=f"adm_grant_{user_id}_vip_90"),
            InlineKeyboardButton(text="+ 90 kun PRO (3 oy)", icon_custom_emoji_id=ID_STARS, style="primary", callback_data=f"adm_grant_{user_id}_pro_90")
        ],
        [
            InlineKeyboardButton(text="+ 1 Yil VIP (365 kun)", icon_custom_emoji_id=ID_CROWN, style="success", callback_data=f"adm_grant_{user_id}_vip_365"),
            InlineKeyboardButton(text="+ 10 Yil VIP", icon_custom_emoji_id=ID_DIAMOND, style="success", callback_data=f"adm_grant_{user_id}_vip_3650")
        ],
        [
            InlineKeyboardButton(text="Tarifni Bekor Qilish (Free)", icon_custom_emoji_id=ID_ERROR, style="danger", callback_data=f"adm_revoke_{user_id}")
        ]
    ]

    if not is_super:
        if is_adm:
            kb_rows.append([InlineKeyboardButton(text="Admin Huquqini Bekor Qilish", icon_custom_emoji_id=ID_ERROR, style="danger", callback_data=f"adm_admin_revoke_{user_id}")])
        else:
            kb_rows.append([InlineKeyboardButton(text="Admin Etib Tayinlash", icon_custom_emoji_id=ID_SETTINGS, style="primary", callback_data=f"adm_admin_grant_{user_id}")])

    kb_rows.append([InlineKeyboardButton(text="Orqaga", icon_custom_emoji_id=ID_BACK, style="danger", callback_data="admin_users_list")])
    await _edit_or_answer(event, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))


def _parse_target_user(data: str, prefix: str) -> int:
    """Returns the positive user id encoded right after `prefix`, or 0 when the payload is malformed."""
    if not data or not data.startswith(prefix):
        return 0
    raw = data[len(prefix):].split("_", 1)[0]
    if not raw.isdigit():
        return 0
    uid = int(raw)
    return uid if 0 < uid < MAX_TELEGRAM_USER_ID else 0


async def cb_user_detail(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = _parse_target_user(callback.data, "adm_user_")
    if not user_id:
        return
    await render_user_detail_screen(user_id, callback)


async def cb_grant_tier(callback: CallbackQuery):
    if not await ensure_super_admin(callback):
        return
    parts = (callback.data or "").split("_")
    try:
        user_id = int(parts[2])
        tier = parts[3]
        days = int(parts[4])
    except (IndexError, ValueError):
        await safe_answer(callback, "Noto'g'ri so'rov.", show_alert=True)
        return
    if user_id <= 0 or tier not in GRANTABLE_TIERS or days not in GRANTABLE_DAYS:
        await safe_answer(callback, "Noto'g'ri tarif yoki muddat.", show_alert=True)
        return

    # Answer first: the grant + notification can take longer than the callback answer window.
    duration_label = f"{days} kunlik"
    await safe_answer(callback, f"Foydalanuvchiga {duration_label} {tier.upper()} tarifi berilmoqda...")

    sub = await db_manager.activate_subscription(
        user_id=user_id,
        tier=tier,
        stars=0,
        charge_id=f"admin_manual_grant_{user_id}_{tier}_{days}d_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        days=days
    )

    target_bot = get_public_bot(callback.bot)
    if target_bot:
        try:
            # A lower tier granted on top of an active higher one is added as extra time of that tier
            active_tier = getattr(sub, "tier", None) if getattr(sub, "tier", None) in ("pro", "vip") else tier
            converted_note = (
                f"\n<i>Sovg'a faol {active_tier.upper()} tarifingizga qo'shimcha muddat sifatida qo'shildi.</i>\n"
                if active_tier != tier else ""
            )
            expires = getattr(sub, "expires_at", None)
            expiry_line = f"\n{CALENDAR} <b>Amal qilish muddati:</b> {format_uz_date(expires)} gacha\n" if expires else ""
            congrat_text = f"""
{SUCCESS} <b>Tabriklaymiz! Obunangiz Faollashtirildi!</b>

Administrator sizning hisobingizga <b>{duration_label} {tier.upper()}</b> obunasini sovg'a qildi!
{converted_note}{expiry_line}
{STAR_SPARKLE} Tarifingizga kiruvchi barcha premium funksiyalar siz uchun ochildi.
"""
            await target_bot.send_message(chat_id=user_id, text=congrat_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not send grant notification to user {user_id}: {e}")

    await render_user_detail_screen(user_id, callback)


async def cb_revoke_tier(callback: CallbackQuery):
    """Step 1: ask for confirmation before wiping a (possibly paid) plan."""
    if not await ensure_super_admin(callback):
        return
    user_id = _parse_target_user(callback.data, "adm_revoke_")
    if not user_id:
        await safe_answer(callback, "Noto'g'ri so'rov.", show_alert=True)
        return
    if is_super_admin(user_id):
        await safe_answer(callback, "Super Admin tarifini bekor qilib bo'lmaydi!", show_alert=True)
        return
    await safe_answer(callback)
    text = (
        f"{WARN} <b>Tarifni bekor qilishni tasdiqlang</b>\n\n"
        f"Foydalanuvchi <code>{user_id}</code> Free tarifiga o'tkaziladi, barcha kanallari va Story "
        f"monitoringi to'xtatiladi. Bu amalni ortga qaytarib bo'lmaydi."
    )
    await _edit_or_answer(callback, text, get_revoke_confirm_keyboard(user_id))


async def cb_revoke_tier_confirm(callback: CallbackQuery):
    """Step 2: revoke after explicit confirmation."""
    if not await ensure_super_admin(callback):
        return
    user_id = _parse_target_user(callback.data, "adm_revoke_ok_")
    if not user_id:
        await safe_answer(callback, "Noto'g'ri so'rov.", show_alert=True)
        return
    if is_super_admin(user_id):
        await safe_answer(callback, "Super Admin tarifini bekor qilib bo'lmaydi!", show_alert=True)
        return
    await db_manager.revoke_subscription(user_id)
    await safe_answer(callback, "Foydalanuvchi tarifi Free holatiga qaytarildi.", show_alert=True)
    await render_user_detail_screen(user_id, callback)


async def cb_grant_admin_privilege(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await safe_answer(callback, "Faqat Super Admin boshqalarga Admin huquqini bera oladi!", show_alert=True)
        return
    user_id = _parse_target_user(callback.data, "adm_admin_grant_")
    if not user_id:
        await safe_answer(callback, "Noto'g'ri so'rov.", show_alert=True)
        return
    await db_manager.set_admin_status(user_id, True)
    await safe_answer(callback, "Foydalanuvchiga Admin huquqi berildi!", show_alert=True)
    await render_user_detail_screen(user_id, callback)


async def cb_revoke_admin_privilege(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await safe_answer(callback, "Faqat Super Admin admin huquqini bekor qila oladi!", show_alert=True)
        return
    user_id = _parse_target_user(callback.data, "adm_admin_revoke_")
    if not user_id:
        await safe_answer(callback, "Noto'g'ri so'rov.", show_alert=True)
        return
    if is_super_admin(user_id):
        await safe_answer(callback, "Super Admin huquqini bekor qilib bo'lmaydi!", show_alert=True)
        return
    await db_manager.set_admin_status(user_id, False)
    await safe_answer(callback, "Admin huquqi muvaffaqiyatli bekor qilindi.", show_alert=True)
    await render_user_detail_screen(user_id, callback)


async def close_cached_public_bot():
    """Shutdown hook kept for run.py: closes the fallback public bot session if one was created."""
    await close_fallback_public_bot()
