import logging
import inspect
from typing import Optional
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from config.settings import settings
from services.custom_emojis import (
    USERS_GROUP, CROWN, STARS, SUCCESS, ERROR, WARN, SEARCH, INFO,
    USER_PROFILE, MEDAL_BRONZE, STAR_SPARKLE, clean_for_alert,
    ID_CROWN, ID_STARS, ID_ERROR, ID_SEARCH, ID_HOME, ID_ROCKET,
    ID_USER, ID_SETTINGS, ID_BACK, ID_DIAMOND, ID_DOCUMENT, ID_FORWARD
)

from bot.utils import safe_answer, html_escape

logger = logging.getLogger(__name__)
router = Router(name="admin_user_management_router")

class AdminUserSG(StatesGroup):
    waiting_for_user_query = State()

@router.callback_query(F.data.startswith("admin_users_page_"))
@router.callback_query(F.data == "admin_users_list")
@router.message(F.text.contains("Foydalanuvchilar"))
async def cb_users_list(event: CallbackQuery | Message, state: FSMContext = None):
    if state:
        await state.clear()
    page = 1
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        if event.data and event.data.startswith("admin_users_page_"):
            try:
                page = max(1, int(event.data.split("_")[3]))
            except (ValueError, IndexError):
                page = 1

    users = await db_manager.get_users_detailed()
    
    if not users:
        text = f"{WARN} Bazada hali foydalanuvchilar yo'q."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="ID / @Username orqali qidirish",
                icon_custom_emoji_id=ID_SEARCH,
                style="primary",
                callback_data="adm_search_user"
            )],
            [InlineKeyboardButton(
                text="Boshqaruv Paneliga Qaytish",
                icon_custom_emoji_id=ID_SETTINGS,
                style="danger",
                callback_data="admin_main_dashboard"
            )]
        ])
    else:
        page_size = 10
        total_users = len(users)
        total_pages = max(1, (total_users + page_size - 1) // page_size)
        page = min(page, total_pages)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_users = users[start_idx:end_idx]

        text = f"{USERS_GROUP} <b>Foydalanuvchilar va Obunalar Ro'yxati ({start_idx + 1}-{min(end_idx, total_users)}/{total_users}):</b>\n\n"
        kb_buttons = [
            [InlineKeyboardButton(
                text="ID / @Username orqali qidirish",
                icon_custom_emoji_id=ID_SEARCH,
                style="primary",
                callback_data="adm_search_user"
            )]
        ]
        
        for idx, u in enumerate(page_users, start_idx + 1):
            tier_badge = f"{CROWN} VIP" if u["tier"] == "vip" else (f"{STARS} Pro" if u["tier"] == "pro" else f"{MEDAL_BRONZE} Sinov")
            username_str = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
            user_name_val = u.get("full_name") or "Foydalanuvchi"
            text += f"<b>{idx}.</b> {html_escape(user_name_val)} ({username_str}) — {tier_badge} | Kanallari: {u['channel_count']} ta\n"
            
            btn_text = f"{user_name_val[:14]} ({(u.get('tier') or 'free').upper()})"
            kb_buttons.append([
                InlineKeyboardButton(
                    text=btn_text,
                    icon_custom_emoji_id=ID_USER,
                    style="primary",
                    callback_data=f"adm_user_{u['user_id']}"
                )
            ])
        
        # Pagination row
        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append(InlineKeyboardButton(text="Oldingi", icon_custom_emoji_id=ID_BACK, style="primary", callback_data=f"admin_users_page_{page - 1}"))
            nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", icon_custom_emoji_id=ID_DOCUMENT, style="primary", callback_data="noop"))
            if page < total_pages:
                nav_row.append(InlineKeyboardButton(text="Keyingi", icon_custom_emoji_id=ID_FORWARD, style="primary", callback_data=f"admin_users_page_{page + 1}"))
            kb_buttons.append(nav_row)

        kb_buttons.append([
            InlineKeyboardButton(
                text="Boshqaruv Paneliga Qaytish",
                icon_custom_emoji_id=ID_SETTINGS,
                style="danger",
                callback_data="admin_main_dashboard"
            )
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "adm_search_user")
async def cb_start_user_search(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AdminUserSG.waiting_for_user_query)
    
    text = f"""
{SEARCH} <b>Foydalanuvchini Qidirish:</b>

Telegram <code>User ID</code> raqamini yoki <code>@username</code> nomini yuboring.
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
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

@router.message(AdminUserSG.waiting_for_user_query)
async def process_user_search_query(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        await message.answer(f"{INFO} Qidirish bekor qilindi.", parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())
        return

    query = message.text.strip()
    await state.clear()
    
    clean_q = query
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if clean_q.startswith(prefix):
            clean_q = clean_q[len(prefix):]
    clean_q = clean_q.lstrip("@").strip()
    
    if clean_q.lstrip("-").isdigit():
        target_uid = int(clean_q)
        user = await db_manager.get_user_by_id(target_uid)
        if not user:
            text = f"{ERROR} <b>ID bo'yicha foydalanuvchi topilmadi:</b> <code>{target_uid}</code>\n\nUshbu foydalanuvchi hali botdan ro'yxatdan o'tmagan."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Qayta qidirish", icon_custom_emoji_id=ID_SEARCH, style="primary", callback_data="adm_search_user")],
                [InlineKeyboardButton(text="Ro'yxatga qaytish", icon_custom_emoji_id=ID_BACK, style="primary", callback_data="admin_users_list")]
            ])
            await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
            return
        await render_user_detail_screen(target_uid, message)
        return

    results = await db_manager.search_users(query)
    if not results:
        text = f"{ERROR} <b>Foydalanuvchi topilmadi:</b> <code>{query}</code>\n\nIltimos, Telegram User ID raqamini kiritib ko'ring."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Qayta qidirish",
                icon_custom_emoji_id=ID_SEARCH,
                style="primary",
                callback_data="adm_search_user"
            )],
            [InlineKeyboardButton(
                text="Ro'yxatga qaytish",
                icon_custom_emoji_id=ID_BACK,
                style="danger",
                callback_data="admin_users_list"
            )]
        ])
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    if len(results) == 1:
        await render_user_detail_screen(results[0]["user_id"], message)
        return

    text = f"{USERS_GROUP} <b>Topilgan foydalanuvchilar ({len(results)} ta):</b>\n\n"
    kb_buttons = []
    for u in results:
        tier_badge = f"{CROWN} VIP" if u["tier"] == "vip" else (f"{STARS} Pro" if u["tier"] == "pro" else "Sinov")
        uname = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        text += f"• <b>{html_escape(u['full_name'])}</b> ({uname}) — {tier_badge}\n"
        kb_buttons.append([
            InlineKeyboardButton(
                text=f"{u['full_name']} ({uname})",
                icon_custom_emoji_id=ID_USER,
                style="primary",
                callback_data=f"adm_user_{u['user_id']}"
            )
        ])
    kb_buttons.append([
        InlineKeyboardButton(
            text="Orqaga",
            icon_custom_emoji_id=ID_BACK,
            style="danger",
            callback_data="admin_users_list"
        )
    ])
    await message.answer(text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

async def render_user_detail_screen(user_id: int, event: CallbackQuery | Message):
    user = await db_manager.get_user_stats(user_id)
    sub = user["subscription"]
    is_primary_super = (user_id == settings.PRIMARY_SUPER_ADMIN_ID)
    is_super = is_primary_super or (user_id in settings.admin_ids)
    is_adm = is_super or await db_manager.is_admin(user_id)
    
    tier_label = f"{CROWN} VIP Cheksiz" if sub.tier == "vip" else (f"{STARS} Pro" if sub.tier == "pro" else "Free (Sinov)")
    exp_date = sub.expires_at[:10] if sub.expires_at else (sub.trial_expires_at[:10] if sub.trial_expires_at else "Mavjud emas")
    admin_status_label = "👑 Asosiy Bosh Admin" if is_primary_super else ("👑 Super Admin" if is_super else ("🛡 Admin" if is_adm else "Foydalanuvchi"))

    text = f"""
{USER_PROFILE} <b>Foydalanuvchi Tafsilotlari & Obuna Boshqaruvi:</b>
───────────────────────────
├ <b>User ID:</b> <code>{user_id}</code>
├ <b>Rol:</b> <code>{admin_status_label}</code>
├ <b>Obuna Tarifi:</b> {tier_label}
├ <b>Muddati:</b> <code>{exp_date}</code>
├ <b>Ulangan Kanallar:</b> <code>{user['total_pairs']}</code> ta
├ <b>Faol Kanallar:</b> <code>{user['active_pairs']}</code> ta
├ <b>Jami Ko'chirgan Postlari:</b> <code>{user['total_cloned']}</code> ta
└ <b>Bugun Ko'chirgan Postlari:</b> <code>{user['today_cloned']}</code> ta
───────────────────────────
<i>Ushbu foydalanuvchiga obuna muddatini berishingiz mumkin:</i>
"""
    kb_rows = [
        [
            InlineKeyboardButton(
                text="+ 30 kun VIP",
                icon_custom_emoji_id=ID_CROWN,
                style="success",
                callback_data=f"adm_grant_{user_id}_vip_30"
            ),
            InlineKeyboardButton(
                text="+ 30 kun PRO",
                icon_custom_emoji_id=ID_STARS,
                style="primary",
                callback_data=f"adm_grant_{user_id}_pro_30"
            )
        ],
        [
            InlineKeyboardButton(
                text="+ 90 kun VIP (3 oy)",
                icon_custom_emoji_id=ID_CROWN,
                style="success",
                callback_data=f"adm_grant_{user_id}_vip_90"
            ),
            InlineKeyboardButton(
                text="+ 90 kun PRO (3 oy)",
                icon_custom_emoji_id=ID_STARS,
                style="primary",
                callback_data=f"adm_grant_{user_id}_pro_90"
            )
        ],
        [
            InlineKeyboardButton(
                text="+ 1 Yil VIP (365 kun)",
                icon_custom_emoji_id=ID_CROWN,
                style="success",
                callback_data=f"adm_grant_{user_id}_vip_365"
            ),
            InlineKeyboardButton(
                text="Cheksiz VIP (Lifetime)",
                icon_custom_emoji_id=ID_DIAMOND,
                style="success",
                callback_data=f"adm_grant_{user_id}_vip_3650"
            )
        ],
        [
            InlineKeyboardButton(
                text="Tarifni Bekor Qilish (Free)",
                icon_custom_emoji_id=ID_ERROR,
                style="danger",
                callback_data=f"adm_revoke_{user_id}"
            )
        ]
    ]

    # Admin privilege toggle row
    if not is_super:
        if is_adm:
            kb_rows.append([
                InlineKeyboardButton(
                    text="Admin Huquqini Bekor Qilish",
                    icon_custom_emoji_id=ID_ERROR,
                    style="danger",
                    callback_data=f"adm_admin_revoke_{user_id}"
                )
            ])
        else:
            kb_rows.append([
                InlineKeyboardButton(
                    text="Admin Etib Tayinlash",
                    icon_custom_emoji_id=ID_SETTINGS,
                    style="primary",
                    callback_data=f"adm_admin_grant_{user_id}"
                )
            ])

    kb_rows.append([
        InlineKeyboardButton(
            text="Orqaga",
            icon_custom_emoji_id=ID_BACK,
            style="danger",
            callback_data="admin_users_list"
        )
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Error editing user detail message: {e}")
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("adm_user_"))
async def cb_user_detail(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = int(callback.data.split("_")[2])
    await render_user_detail_screen(user_id, callback)

_cached_public_bot: Optional[Bot] = None

def _get_public_bot(current_bot: Optional[Bot] = None) -> Optional[Bot]:
    global _cached_public_bot
    if current_bot and getattr(current_bot, "token", None) == settings.BOT_TOKEN:
        return current_bot
    if not settings.BOT_TOKEN:
        return None
    if _cached_public_bot is None:
        _cached_public_bot = Bot(token=settings.BOT_TOKEN)
    return _cached_public_bot

async def close_cached_public_bot():
    global _cached_public_bot
    if _cached_public_bot and getattr(_cached_public_bot, "session", None):
        try:
            import inspect
            close_res = _cached_public_bot.session.close()
            if inspect.isawaitable(close_res):
                await close_res
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        _cached_public_bot = None

@router.callback_query(F.data.startswith("adm_grant_"))
async def cb_grant_tier(callback: CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[2])
    tier = parts[3]
    days = int(parts[4]) if len(parts) > 4 else 30
    if days <= 0:
        days = 30
    
    await db_manager.activate_subscription(
        user_id=user_id,
        tier=tier,
        stars=0,
        charge_id=f"admin_manual_grant_{user_id}_{tier}_{days}d_{int(datetime.now(timezone.utc).timestamp())}",
        days=days
    )
    
    # Notify user via public client bot instance
    target_bot = _get_public_bot(callback.bot)
    if target_bot:
        try:
            tier_title = "VIP Cheksiz" if tier == "vip" else "PRO"
            duration_str = f"{days} kunlik" if days < 1000 else "Muddatsiz (Cheksiz)"
            congrat_text = f"""
{SUCCESS} <b>Tabriklaymiz! Obunangiz Faollashtirildi!</b>

Administrator sizning hisobingizga <b>{duration_str} {tier_title}</b> obunasini sovg'a qildi!

{STAR_SPARKLE} Barcha premium funksiyalar (avto-tarjima, AI qayta yozish, video watermark, cheksiz kanallar) siz uchun to'liq ochildi!
"""
            await target_bot.send_message(chat_id=user_id, text=congrat_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not send grant notification to user {user_id}: {e}")
    
    duration_label = f"{days} kunlik" if days < 1000 else "Cheksiz"
    await safe_answer(callback, f"Foydalanuvchiga {duration_label} {tier.upper()} tarifi berildi va xabar yuborildi!", show_alert=True)
    await render_user_detail_screen(user_id, callback)

@router.callback_query(F.data.startswith("adm_revoke_"))
async def cb_revoke_tier(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if user_id == settings.PRIMARY_SUPER_ADMIN_ID or user_id in settings.admin_ids or await db_manager.is_admin(user_id):
        await safe_answer(callback, "Super Admin tarifini bekor qilib bo'lmaydi!", show_alert=True)
        return
    await db_manager.revoke_subscription(user_id)
    await safe_answer(callback, "Foydalanuvchi tarifi Free holatiga qaytarildi.", show_alert=True)
    await render_user_detail_screen(user_id, callback)


@router.callback_query(F.data.startswith("adm_admin_grant_"))
async def cb_grant_admin_privilege(callback: CallbackQuery):
    caller_id = callback.from_user.id
    if caller_id != settings.PRIMARY_SUPER_ADMIN_ID and caller_id not in settings.admin_ids:
        await safe_answer(callback, "Faqat Bosh Super Admin boshqalarga Admin huquqini bera oladi!", show_alert=True)
        return
    user_id = int(callback.data.split("_")[3])
    await db_manager.set_admin_status(user_id, True)
    await safe_answer(callback, "Foydalanuvchiga Admin huquqi berildi!", show_alert=True)
    await render_user_detail_screen(user_id, callback)


@router.callback_query(F.data.startswith("adm_admin_revoke_"))
async def cb_revoke_admin_privilege(callback: CallbackQuery):
    caller_id = callback.from_user.id
    if caller_id != settings.PRIMARY_SUPER_ADMIN_ID and caller_id not in settings.admin_ids:
        await safe_answer(callback, "Faqat Bosh Super Admin admin huquqini bekor qila oladi!", show_alert=True)
        return
    user_id = int(callback.data.split("_")[3])
    if user_id == settings.PRIMARY_SUPER_ADMIN_ID or user_id in settings.admin_ids:
        await safe_answer(callback, "Asosiy Super Admin huquqini bekor qilib bo'lmaydi!", show_alert=True)
        return
    await db_manager.set_admin_status(user_id, False)
    await safe_answer(callback, "Admin huquqi muvaffaqiyatli bekor qilindi.", show_alert=True)
    await render_user_detail_screen(user_id, callback)

