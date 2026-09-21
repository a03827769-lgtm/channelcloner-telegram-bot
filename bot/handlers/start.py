import logging
from typing import Union
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
from bot.keyboards.inline_buttons import (
    get_main_menu_keyboard,
    get_back_to_main_keyboard,
    get_quickstart_keyboard,
    get_main_reply_keyboard
)
from config.settings import settings
from bot.utils import safe_answer
from services.custom_emojis import (
    TELEGRAM, CROWN, SUCCESS, FLASH, WARN, ERROR,
    TRANSLATE, IMAGE, LOCK_UNLOCKED, MONEY, CLEAN, SIGNATURE, REFRESH,
    STATS, ROCKET, NUM_1, NUM_2, LINK, STARS, ARROW_DOWN, VIDEO, clean_for_alert
)

logger = logging.getLogger(__name__)
router = Router(name="start_router")


def get_welcome_text() -> str:
    return f"""
{TELEGRAM} <b>Telegram Kanal va Guruh Kloner Tizimi</b>

{FLASH} <b>Asosiy Imkoniyatlar:</b>
├ {FLASH} <b>Real-Vaqtda Klonlash:</b> Manba kanalda yangi post chiqishi bilanoq darhol sizning kanalingizga yetib boradi.
├ {SUCCESS} <b>100% To'liq Media:</b> Matnlar, Rasmlar, Videolar, <b>Albomlar</b>, Ovozli xabarlar, Dumaloq videolar, Hujjatlar va Stikerlar.
├ {TRANSLATE} <b>Avto-Tarjima:</b> Chet el kanallaridagi yangiliklarni bir soniyada o'zbek tiliga o'girish.
├ {IMAGE} <b>Rasmga Watermark:</b> Har bir rasmga o'z logotipingiz yoki kanalingiz nomini tushirish.
├ {LOCK_UNLOCKED} <b>Protected Content Mode:</b> Forward taqiqlangan yopiq darslik kanallarini ko'chirish.
├ {MONEY} <b>Referal Almashtirgich:</b> Begona linklarni o'z referal havolalaringizga almashtirish.
├ {CLEAN} <b>Reklamani Tozalash:</b> Begona kanal havolalari avtomatik tozalanadi.
├ {VIDEO} <b>Real Estate Auto-Story Cloner (VIP):</b> $700+ hashamatli uylarni avtomatik aniqlab, 4K Playwright kollaj va Ken Burns video ko'rinishida Telegram Istoriyasiga avto-joylash.
├ {SIGNATURE} <b>Shaxsiy Imzo:</b> Xabar ostiga o'z kanalingiz havolasini joylash.
└ {REFRESH} <b>Tarixni Ko'chirish (Backfill):</b> Manba kanaldagi eski postlarni ham ko'chirish.

Quyidagi menyu orqali kerakli bo'limni tanlang:
"""

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    is_admin = False
    if user:
        is_admin = (user.id in settings.admin_ids)
        await db_manager.get_or_create_user(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
            is_admin=is_admin
        )
        if not is_admin:
            is_admin = await db_manager.is_admin(user.id)

    # Set persistent chat menu button to WebApp for this user
    if user:
        from bot.keyboards.inline_buttons import get_active_webapp_url
        active_url = get_active_webapp_url()
        if active_url and active_url.startswith("https://"):
            try:
                from aiogram.types import MenuButtonWebApp, WebAppInfo
                await message.bot.set_chat_menu_button(
                    chat_id=user.id,
                    menu_button=MenuButtonWebApp(text="📱 Mini App", web_app=WebAppInfo(url=active_url))
                )
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

    # Send persistent reply keyboard and inline welcome dashboard
    await message.answer(
        text=f"{ARROW_DOWN} <b>Quyidagi menyudan kerakli bo'limni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )

    await message.answer(
        text=get_welcome_text(),
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(is_admin=is_admin)
    )

@router.message(Command("app", "miniapp"))
@router.message(F.text.in_({"📱 Mini Appni Ochish", "Mini App", "📱 Mini App"}))
async def cmd_open_miniapp(message: Message):
    from bot.keyboards.inline_buttons import get_active_webapp_url
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    active_url = get_active_webapp_url()
    if active_url.startswith("https://"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Mini Appni Ochish", web_app=WebAppInfo(url=active_url))],
            [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="menu_main")]
        ])
        await message.answer(
            f"{ROCKET} <b>ChannelCloner Pro — Telegram Mini App</b>\n\n"
            f"Kanal va guruhlarni boshqarish, jonli tahlillar, grafiklar, "
            f"VIP Story Studio va avto-klonlash sozlamalarini qulay Mini App orqali boshqaring!\n\n"
            f"<i>Pastdagi tugmani bosing:</i>",
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        await message.answer(
            f"{WARN} <b>Mini App yuklanmoqda...</b>\n\n"
            f"Iltimos 5-10 soniyadan so'ng qayta urinib ko'ring.",
            parse_mode="HTML"
        )

@router.callback_query(F.data == "menu_open_miniapp")
async def cb_open_miniapp(callback: CallbackQuery):
    await safe_answer(callback)
    from bot.keyboards.inline_buttons import get_active_webapp_url
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    active_url = get_active_webapp_url()
    if active_url.startswith("https://"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Mini Appni Ochish", web_app=WebAppInfo(url=active_url))],
            [InlineKeyboardButton(text="⬅️ Ortga", callback_data="menu_main")]
        ])
        await callback.message.answer(
            f"{ROCKET} <b>ChannelCloner Pro — Telegram Mini App</b>\n\n"
            f"Quyidagi tugma orqali ilovani ochishingiz mumkin:",
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        await safe_answer(callback, "⚠️ Mini App hozir ulanmoqda. Bir necha soniyadan so'ng qayta urinib ko'ring.", show_alert=True)

@router.callback_query(F.data == "menu_quickstart")
async def cb_quickstart(callback: CallbackQuery):
    await safe_answer(callback)
    text = f"""
{ROCKET} <b>Tezkor Boshlash Qo'llanmasi (30 Soniyada):</b>

Botdan to'liq foydalanish uchun bor-yo'g'i 2 ta oddiy qadam:

{NUM_1} <b>Kanal Juftligini Bog'lash:</b>
• Manba kanaldan <b>istalgan bitta xabarni</b> ushbu botga <b>FORWARD (uzatish)</b> qiling yoki havolasini yozing.
• O'zingizning kanalingizdan <b>istalgan bitta xabarni</b> botga <b>FORWARD</b> qiling <i>(botingiz kanalingizda administrator bo'lishi kerak)</i>.

{NUM_2} <b>Test Post va Sozlamalar:</b>
• Kanal sozlamalaridagi <b>\"{ROCKET} Test Post\"</b> tugmasini bosing — kanalingizga sinov xabari yuboriladi!
• Xohlaganingizcha Avto-tarjima, Imzo yoki Suv belgisini yoqing.

Quyidagi tugmalar orqali hoziroq boshlang:
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_quickstart_keyboard()
    )

@router.callback_query(F.data == "menu_main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    is_admin = (user_id in settings.admin_ids) or await db_manager.is_admin(user_id)
    await callback.message.edit_text(
        text=get_welcome_text(),
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(is_admin=is_admin)
    )

@router.callback_query(F.data == "menu_admin")
async def cb_menu_admin_fallback(callback: CallbackQuery):
    await safe_answer(callback, "Ushbu bo'lim faqat bot administratorlari uchun ruxsat etilgan!", show_alert=True)

@router.callback_query(F.data == "check_private_access")
async def cb_check_private_access(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    is_allowed = await db_manager.can_user_access_bot(user_id)
    if is_allowed:
        await safe_answer(callback, "Ruxsat tasdiqlandi! Xush kelibsiz!", show_alert=True)
        await state.clear()
        is_admin = (user_id in settings.admin_ids) or await db_manager.is_admin(user_id)
        try:
            await callback.message.delete()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        await callback.message.answer(
            text=f"{ARROW_DOWN} <b>Quyidagi menyudan kerakli bo'limni tanlang:</b>",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        await callback.message.answer(
            text=get_welcome_text(),
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(is_admin=is_admin)
        )
    else:
        await safe_answer(
            callback,
            "Hali ruxsat berilmagan. Iltimos, admin javobini kuting yoki 50 Stars to'lang.",
            show_alert=True
        )


@router.message(F.text.contains("Istoriya"))
async def handle_story_button_text(message: Message, state: FSMContext):
    from bot.handlers.story_menu import render_story_main_menu, check_is_vip, get_story_vip_upgrade_text_and_keyboard
    await state.clear()
    user_id = message.from_user.id
    if not await check_is_vip(user_id):
        text, kb = await get_story_vip_upgrade_text_and_keyboard(user_id)
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return
    text, kb = await render_story_main_menu(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "menu_stats")
@router.message(Command("stats"))
@router.message(F.text.contains("Statistika"))
async def cb_stats(event: Union[CallbackQuery, Message]):
    user_id = event.from_user.id
    user_stats = await db_manager.get_user_stats(user_id)
    sub = user_stats["subscription"]

    is_admin = (user_id in settings.admin_ids) or await db_manager.is_admin(user_id)
    tier_label = f"{CROWN} VIP Cheksiz" if sub.tier == "vip" or is_admin else (f"{STARS} Pro" if sub.tier == "pro" else "Free (14 kunlik Sinov)")

    text = f"""
{STATS} <b>Sizning Shaxsiy Statistikangiz:</b>

├ {LINK} <b>Ulangan kanallaringiz:</b> <code>{user_stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol ishlayotgan kanallar:</b> <code>{user_stats['active_pairs']}</code> ta
├ {ROCKET} <b>Jami ko'chirilgan postlaringiz:</b> <code>{user_stats['total_cloned']}</code> ta
├ {FLASH} <b>Bugun ko'chirilgan postlar:</b> <code>{user_stats['today_cloned']}</code> ta
└ {STARS} <b>Obuna tarifi:</b> {tier_label}

<i>Har bir kanalingizning alohida grafik va prevyularini <b>{REFRESH} Kanal Kloner</b> bo'limidan ko'rishingiz mumkin.</i>
"""
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await event.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_to_main_keyboard()
        )
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_to_main_keyboard()
        )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    from services.telethon_listener import telethon_listener
    user_id = message.from_user.id
    is_admin = user_id in settings.admin_ids or await db_manager.is_admin(user_id)

    # Clear any active FSM state
    await state.clear()

    # BUG #19 fix: only cancel tasks belonging to this user (admin cancels all)
    cancelled_pairs = []
    for pair_id, task in list(telethon_listener.active_history_tasks.items()):
        if task and not task.done():
            # Check ownership — only cancel if admin or pair belongs to this user
            try:
                pair = await db_manager.get_pair_by_id(pair_id)
                if is_admin or (pair and pair.user_id == user_id):
                    task.cancel()
                    telethon_listener.active_history_tasks.pop(pair_id, None)
                    cancelled_pairs.append(pair_id)
            except Exception:
                if is_admin:
                    task.cancel()
                    telethon_listener.active_history_tasks.pop(pair_id, None)
                    cancelled_pairs.append(pair_id)

    if cancelled_pairs:
        await message.answer(
            f"{WARN} <b>Bekor qilindi!</b>\n\n"
            f"Barcha faol jarayonlar va FSM holatlari tozalandi.\n"
            f"Ko'chirilgan postlar saqlanib qoldi.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
    else:
        await message.answer(
            f"{SUCCESS} <b>Hech qanday faol jarayon topilmadi.</b>\n\n"
            f"Asosiy menyuga qaytildi.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )


@router.message(Command("briefing", "digest"))
async def cmd_market_briefing(message: Message):
    """Generates today's Executive Real Estate Market Analytics and Audio Briefing (VIP Only)"""
    user_id = message.from_user.id
    is_vip = await db_manager.is_vip(user_id) or (user_id in settings.admin_ids)
    if not is_vip:
        from bot.keyboards.story_keyboards import get_story_vip_upgrade_keyboard
        await message.answer(
            f"{CROWN} <b>VIP Cheksiz Tarif Talab Qilinadi!</b>\n\n"
            f"Kunlik Toshkent ko'chmas mulk bozori tahlili va audio podkasti faqat <b>VIP Cheksiz</b> foydalanuvchilariga taqdim etiladi.",
            parse_mode="HTML",
            reply_markup=get_story_vip_upgrade_keyboard()
        )
        return

    # Rate limiting: prevent spamming expensive audio briefing generation
    cache_key = f"briefing_ratelimit_{user_id}"
    from services.cache_manager import cache_manager
    if await cache_manager.seen_users_cache.get(cache_key):
        await message.answer("⏳ <i>Iltimos kuting: Kunlik tahlilni daqiqasiga 1 martadan ko'p so'rash mumkin emas.</i>", parse_mode="HTML")
        return
    await cache_manager.seen_users_cache.set(cache_key, True, ttl=60.0)

    from services.market_analytics import market_analytics_service
    status_msg = await message.answer("⏳ <i>Kunlik bozor tahlili va audio podkast tayyorlanmoqda...</i>", parse_mode="HTML")
    try:
        await market_analytics_service.send_daily_briefing(message.bot, message.chat.id)
        try:
            await status_msg.delete()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    except Exception as e:
        logger.error(f"Error generating market briefing: {e}", exc_info=True)
        await message.answer(f"{ERROR} Xatolik yuz berdi. Iltimos keyinroq urinib ko'ring.", parse_mode="HTML")


