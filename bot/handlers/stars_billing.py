import logging
from typing import Union
from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice
)
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
from bot.keyboards.stars_keyboards import get_stars_plans_keyboard
from bot.keyboards.inline_buttons import get_back_to_main_keyboard
from services.custom_emojis import (
    STARS, CROWN, MEDAL_BRONZE, DIAMOND, TRANSLATE, MONEY,
    REFRESH, FLASH, IMAGE, LOCK_UNLOCKED, ROCKET, PARTY, ERROR,
    SUCCESS, BOX, CLEAN, SIGNATURE, LOADING, STAR_SPARKLE, CALENDAR,
    SERVER_CPU
)

logger = logging.getLogger(__name__)
router = Router(name="stars_billing_router")

PLANS = {
    "pro": {
        "title": "Pro Tarif (30 kun)",
        "description": "5 ta kanal, AI Content Paraphraser, Video Watermark, Dynamic CTA, Avto-tarjima, Referal almashtirgich.",
        "stars": 100,
        "days": 30
    },
    "vip": {
        "title": "VIP Cheksiz Tarif (30 kun)",
        "description": "Cheksiz kanallar, Telegram Premium Animatsion Emojilar, Himoyalangan kanallar, AI Paraphraser, Video Watermark.",
        "stars": 300,
        "days": 30
    }
}

async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

@router.callback_query(F.data.in_(["menu_stars", "menu_billing"]))
@router.message(F.text.contains("Tariflar"))
async def cb_billing_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    sub = await db_manager.get_user_subscription(user_id)

    if sub.tier == "vip" and sub.is_active:
        exp_str = sub.expires_at[:10] if sub.expires_at else "N/A"
        tier_badge = f"{CROWN} <b>VIP Cheksiz</b> <i>({exp_str} gacha)</i>"
    elif sub.tier == "pro" and sub.is_active:
        exp_str = sub.expires_at[:10] if sub.expires_at else "N/A"
        tier_badge = f"{STARS} <b>Pro Tarif</b> <i>({exp_str} gacha)</i>"
    elif sub.tier == "free" and sub.is_trial_active:
        trial_str = sub.trial_expires_at[:10] if sub.trial_expires_at else "N/A"
        tier_badge = f"{MEDAL_BRONZE} <b>Bepul Sinov (14 Kun)</b> <i>({trial_str} gacha faol)</i>"
    else:
        tier_badge = f"{ERROR} <b>Sinov Muddati Tugagan</b> <i>(Tarif tanlang)</i>"

    text = f"""
{STARS} <b>Telegram Stars — Obuna va Premium Tariflar</b>

Sizning hozirgi tarifingiz: {tier_badge}

{DIAMOND} <b>Mavjud Tariflar va Imkoniyatlar:</b>

<b>{MEDAL_BRONZE} Bepul Sinov (Free Trial — 14 Kun):</b>
├ {BOX} 1 ta faol kanal juftligi
├ {CLEAN} Reklama va begona linklarni tozalash
├ {TRANSLATE} Avto-Tarjima (Auto-Translate)
├ {SIGNATURE} Shaxsiy imzo qo'yish
└ {LOADING} <i>14 kundan so'ng Pro yoki VIP tarifiga o'tish talab etiladi</i>

<b>{STARS} Pro Tarif — 100 Stars (1 oy):</b>
├ {BOX} 5 tagacha faol kanal juftligi
├ {SERVER_CPU} <b>AI Content Paraphraser</b> (Rasmiy, Hype, Tezis uslublari)
├ {IMAGE} <b>Rasm va Video Watermark</b> (FFmpeg Logo urish)
├ {MONEY} <b>Dynamic Affiliate & CTA Tugmalar</b>
├ {TRANSLATE} Avto-Tarjima + Referal Almashtirgich
└ {FLASH} Tezkor xizmat ko'rsatish

<b>{CROWN} VIP Cheksiz — 300 Stars (1 oy):</b>
├ {BOX} <b>Cheksiz kanallar juftligi (999 ta)</b>
├ {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar</b> (Oddiy emojilar avto premium animatsion bo'ladi)
├ {LOCK_UNLOCKED} <b>Himoyalangan (Protected) yopiq kanallarni ko'chirish</b>
├ {SERVER_CPU} Barcha AI Paraphraser va Video Watermark imkoniyatlari
└ {ROCKET} Eng yuqori server ustuvorligi (0 soniya kechikish)

<i>Tarifni faollashtirish uchun quyidagi tugmalardan birini tanlang:</i>
"""
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_stars_plans_keyboard(sub)
        )
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_stars_plans_keyboard(sub)
        )

@router.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery, bot: Bot = None):
    tier = callback.data.replace("buy_plan_", "")
    if tier not in PLANS:
        await safe_answer(callback, "Noto'g'ri tarif!", show_alert=True)
        return

    plan = PLANS[tier]
    prices = [LabeledPrice(label=plan["title"], amount=plan["stars"])]

    await safe_answer(callback, "To'lov cheki yuborilmoqda...")

    target_bot = bot or callback.bot
    try:
        await target_bot.send_invoice(
            chat_id=callback.from_user.id,
            title=plan["title"],
            description=plan["description"],
            payload=f"stars_plan_{tier}_{callback.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter=f"buy_{tier}"
        )
    except Exception as e:
        logger.error(f"Failed to create Telegram Stars invoice: {e}")
        await callback.message.answer(
            f"{ERROR} <b>Hisob-faktura yaratishda xatolik:</b> {e}\n\nIltimos, qaytadan urinib ko'ring yoki administratorga murojaat qiling.",
            parse_mode="HTML"
        )

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload

    if not payload.startswith("stars_plan_"):
        logger.warning(f"Unknown payment payload: {payload}")
        return

    parts = payload.split("_")
    tier = parts[2]
    user_id = int(parts[3])

    plan = PLANS.get(tier, {"stars": payment.total_amount, "days": 30})
    sub = await db_manager.activate_subscription(
        user_id=user_id,
        tier=tier,
        stars=plan["stars"],
        charge_id=payment.telegram_payment_charge_id,
        days=plan["days"]
    )

    text = f"""
{SUCCESS} <b>To'lovingiz muvaffaqiyatli qabul qilindi!</b>

├ {CROWN if tier == 'vip' else STARS} <b>Faollashtirilgan tarif:</b> {tier.upper()}
├ {CALENDAR} <b>Amal qilish muddati:</b> {sub.expires_at[:10]} gacha (30 kun)
├ {STARS} <b>To'langan summa:</b> {payment.total_amount} Stars
└ {DIAMOND} <b>Tranzaksiya ID:</b> <code>{payment.telegram_payment_charge_id}</code>

<i>Barcha yangi imkoniyatlar kanallaringiz uchun darhol ishga tushirildi!</i>
"""
    await message.answer(text=text, parse_mode="HTML", reply_markup=get_back_to_main_keyboard())
