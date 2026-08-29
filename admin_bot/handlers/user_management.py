import logging
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from config.settings import settings
from services.custom_emojis import (
    USERS_GROUP, CROWN, STARS, SUCCESS, ERROR, WARN, SEARCH,
    USER_PROFILE, MEDAL_BRONZE, ID_CROWN, ID_STARS, ID_ERROR, ID_SEARCH, ID_HOME, ID_ROCKET
)

logger = logging.getLogger(__name__)
router = Router(name="admin_user_management_router")

class AdminUserSG(StatesGroup):
    waiting_for_user_query = State()

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

@router.callback_query(F.data == "admin_users_list")
@router.message(F.text.contains("Foydalanuvchilar"))
async def cb_users_list(event: CallbackQuery | Message, state: FSMContext = None):
    if state:
        await state.clear()
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    users = await db_manager.get_users_detailed()
    
    if not users:
        text = f"{WARN} Bazada hali foydalanuvchilar yo'q."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 ID / @Username orqali qidirish", callback_data="adm_search_user", style="primary", icon_custom_emoji_id=ID_SEARCH)],
            [InlineKeyboardButton(text="Boshqaruv Paneliga Qaytish", callback_data="admin_main_dashboard", style="danger", icon_custom_emoji_id=ID_HOME)]
        ])
    else:
        text = f"{USERS_GROUP} <b>Foydalanuvchilar va Obunalar Ro'yxati (So'nggi {len(users)} ta):</b>\n\n"
        kb_buttons = [
            [InlineKeyboardButton(text="🔍 ID / @Username orqali qidirish", callback_data="adm_search_user", style="primary", icon_custom_emoji_id=ID_SEARCH)]
        ]
        
        for idx, u in enumerate(users[:15], 1):
            tier_badge = f"{CROWN} VIP" if u["tier"] == "vip" else (f"{STARS} Pro" if u["tier"] == "pro" else f"{MEDAL_BRONZE} Sinov")
            username_str = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
            text += f"<b>{idx}.</b> {u['full_name']} ({username_str}) — {tier_badge} | Kanallari: {u['channel_count']} ta\n"
            
            btn_text = f"{u['full_name'][:14]} ({u['tier'].upper()})"
            kb_buttons.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"adm_user_{u['user_id']}")
            ])
        
        kb_buttons.append([
            InlineKeyboardButton(text="Boshqaruv Paneliga Qaytish", callback_data="admin_main_dashboard", style="danger", icon_custom_emoji_id=ID_HOME)
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
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
        [InlineKeyboardButton(text="Bekor qilish", callback_data="admin_users_list", style="danger", icon_custom_emoji_id=ID_ERROR)]
    ])
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

@router.message(AdminUserSG.waiting_for_user_query)
async def process_user_search_query(message: Message, state: FSMContext):
    query = message.text.strip()
    await state.clear()
    
    clean_q = query.lstrip("@").strip()
    
    if clean_q.isdigit():
        target_uid = int(clean_q)
        user = await db_manager.get_user_by_id(target_uid)
        if not user:
            await db_manager.get_or_create_user(target_uid, f"Foydalanuvchi {target_uid}", None)
        await render_user_detail_screen(target_uid, message)
        return

    results = await db_manager.search_users(query)
    if not results:
        text = f"{ERROR} <b>Foydalanuvchi topilmadi:</b> <code>{query}</code>\n\nIltimos, Telegram User ID raqamini kiritib ko'ring."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Qayta qidirish", callback_data="adm_search_user", style="primary", icon_custom_emoji_id=ID_SEARCH)],
            [InlineKeyboardButton(text="Ro'yxatga qaytish", callback_data="admin_users_list", style="danger", icon_custom_emoji_id=ID_HOME)]
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
        text += f"• <b>{u['full_name']}</b> ({uname}) — {tier_badge}\n"
        kb_buttons.append([
            InlineKeyboardButton(text=f"{u['full_name']} ({uname})", callback_data=f"adm_user_{u['user_id']}")
        ])
    kb_buttons.append([
        InlineKeyboardButton(text="Orqaga", callback_data="admin_users_list", style="danger", icon_custom_emoji_id=ID_HOME)
    ])
    await message.answer(text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

async def render_user_detail_screen(user_id: int, event: CallbackQuery | Message):
    user = await db_manager.get_user_stats(user_id)
    sub = user["subscription"]
    
    tier_label = f"{CROWN} VIP Cheksiz" if sub.tier == "vip" else (f"{STARS} Pro" if sub.tier == "pro" else "Free (Sinov)")
    exp_date = sub.expires_at[:10] if sub.expires_at else (sub.trial_expires_at[:10] if sub.trial_expires_at else "Mavjud emas")

    text = f"""
{USER_PROFILE} <b>Foydalanuvchi Tafsilotlari & Obuna Boshqaruvi:</b>
───────────────────────────
├ <b>User ID:</b> <code>{user_id}</code>
├ <b>Obuna Tarifi:</b> {tier_label}
├ <b>Muddati:</b> <code>{exp_date}</code>
├ <b>Ulangan Kanallar:</b> <code>{user['total_pairs']}</code> ta
├ <b>Faol Kanallar:</b> <code>{user['active_pairs']}</code> ta
├ <b>Jami Ko'chirgan Postlari:</b> <code>{user['total_cloned']}</code> ta
└ <b>Bugun Ko'chirgan Postlari:</b> <code>{user['today_cloned']}</code> ta
───────────────────────────
<i>Ushbu foydalanuvchiga obuna muddatini berishingiz mumkin:</i>
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+ 30 kun VIP", callback_data=f"adm_grant_{user_id}_vip_30", style="success", icon_custom_emoji_id=ID_CROWN),
            InlineKeyboardButton(text="+ 30 kun PRO", callback_data=f"adm_grant_{user_id}_pro_30", style="primary", icon_custom_emoji_id=ID_STARS)
        ],
        [
            InlineKeyboardButton(text="+ 90 kun VIP (3 oy)", callback_data=f"adm_grant_{user_id}_vip_90", style="success", icon_custom_emoji_id=ID_CROWN),
            InlineKeyboardButton(text="+ 90 kun PRO (3 oy)", callback_data=f"adm_grant_{user_id}_pro_90", style="primary", icon_custom_emoji_id=ID_STARS)
        ],
        [
            InlineKeyboardButton(text="+ 1 Yil VIP (365 kun)", callback_data=f"adm_grant_{user_id}_vip_365", style="success", icon_custom_emoji_id=ID_CROWN),
            InlineKeyboardButton(text="♾ Cheksiz VIP (Lifetime)", callback_data=f"adm_grant_{user_id}_vip_3650", style="success", icon_custom_emoji_id=ID_ROCKET)
        ],
        [
            InlineKeyboardButton(text="Tarifni Bekor Qilish (Free)", callback_data=f"adm_revoke_{user_id}", style="danger", icon_custom_emoji_id=ID_ERROR)
        ],
        [
            InlineKeyboardButton(text="Orqaga", callback_data="admin_users_list", style="danger", icon_custom_emoji_id=ID_HOME)
        ]
    ])
    
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("adm_user_"))
async def cb_user_detail(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = int(callback.data.split("_")[2])
    await render_user_detail_screen(user_id, callback)

@router.callback_query(F.data.startswith("adm_grant_"))
async def cb_grant_tier(callback: CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[2])
    tier = parts[3]
    days = int(parts[4]) if len(parts) > 4 else 30
    
    await db_manager.activate_subscription(
        user_id=user_id,
        tier=tier,
        stars=0,
        charge_id=f"admin_manual_grant_{tier}_{days}d",
        days=days
    )
    
    # Notify user via public client bot instance
    if settings.BOT_TOKEN:
        public_bot = None
        try:
            public_bot = Bot(token=settings.BOT_TOKEN)
            tier_title = "VIP Cheksiz" if tier == "vip" else "PRO"
            duration_str = f"{days} kunlik" if days < 1000 else "Muddatsiz (Cheksiz)"
            congrat_text = f"""
{SUCCESS} <b>Tabriklaymiz! Obunangiz Faollashtirildi!</b>

Administrator sizning hisobingizga <b>{duration_str} {tier_title}</b> obunasini sovg'a qildi!

✨ Barcha premium funksiyalar (avto-tarjima, AI qayta yozish, video watermark, cheksiz kanallar) siz uchun to'liq ochildi!
"""
            await public_bot.send_message(chat_id=user_id, text=congrat_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not send grant notification to user {user_id}: {e}")
        finally:
            if public_bot and public_bot.session:
                await public_bot.session.close()
    
    duration_label = f"{days} kunlik" if days < 1000 else "Cheksiz"
    await safe_answer(callback, f"Foydalanuvchiga {duration_label} {tier.upper()} tarifi berildi va xabar yuborildi!", show_alert=True)
    await render_user_detail_screen(user_id, callback)

@router.callback_query(F.data.startswith("adm_revoke_"))
async def cb_revoke_tier(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    await db_manager.revoke_subscription(user_id)
    await safe_answer(callback, "Foydalanuvchi tarifi Free holatiga qaytarildi.", show_alert=True)
    await render_user_detail_screen(user_id, callback)

