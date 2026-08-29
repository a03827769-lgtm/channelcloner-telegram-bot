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
from services.custom_emojis import (
    TELEGRAM, CROWN, SUCCESS, FLASH,
    TRANSLATE, IMAGE, LOCK_UNLOCKED, MONEY, CLEAN, SIGNATURE, REFRESH,
    STATS, ROCKET, NUM_1, NUM_2, LINK, STARS, ARROW_DOWN
)

logger = logging.getLogger(__name__)
router = Router(name="start_router")

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

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
├ {SIGNATURE} <b>Shaxsiy Imzo:</b> Xabar ostiga o'z kanalingiz havolasini joylash.
└ {REFRESH} <b>Tarixni Ko'chirish (Backfill):</b> Manba kanaldagi eski postlarni ham ko'chirish.

Quyidagi menyu orqali kerakli bo'limni tanlang:
"""

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    if user:
        await db_manager.get_or_create_user(
            user_id=user.id,
            full_name=user.full_name,
            username=user.username,
            is_admin=(user.id in settings.admin_ids)
        )

    # Send persistent reply keyboard and inline welcome dashboard
    await message.answer(
        text=f"{ARROW_DOWN} <b>Quyidagi menyudan kerakli bo'limni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )

    await message.answer(
        text=get_welcome_text(),
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )

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
    await callback.message.edit_text(
        text=get_welcome_text(),
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )

@router.callback_query(F.data == "menu_stats")
@router.message(F.text.contains("Statistika"))
async def cb_stats(event: Union[CallbackQuery, Message]):
    user_id = event.from_user.id
    user_stats = await db_manager.get_user_stats(user_id)
    sub = user_stats["subscription"]

    tier_label = f"{CROWN} VIP Cheksiz" if sub.tier == "vip" else (f"{STARS} Pro" if sub.tier == "pro" else "Free (14 kunlik Sinov)")

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
    is_admin = user_id in settings.admin_ids

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
            f"⛔ <b>Bekor qilindi!</b>\n\n"
            f"Barcha faol jarayonlar va FSM holatlari tozalandi.\n"
            f"Ko'chirilgan postlar saqlanib qoldi.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
    else:
        await message.answer(
            f"✅ <b>Hech qanday faol jarayon topilmadi.</b>\n\n"
            f"Asosiy menyuga qaytildi.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )

