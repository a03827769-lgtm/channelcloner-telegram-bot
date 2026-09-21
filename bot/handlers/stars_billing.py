import logging
from typing import Union
from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from database.db_manager import db_manager
from config.settings import settings
from bot.keyboards.stars_keyboards import get_stars_plans_keyboard
from bot.keyboards.inline_buttons import get_back_to_main_keyboard
from services.custom_emojis import (
    STARS, CROWN, MEDAL_BRONZE, DIAMOND, TRANSLATE, MONEY,
    REFRESH, FLASH, IMAGE, LOCK_UNLOCKED, ROCKET, PARTY, ERROR,
    SUCCESS, BOX, CLEAN, SIGNATURE, LOADING, STAR_SPARKLE, CALENDAR,
    SERVER_CPU, VIDEO, INFO, ID_CROWN, ID_HOME, ID_VIDEO, ID_FLASH
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
        "description": "Ko'chmas Mulk Auto-Story Cloner ($700+), Cheksiz kanallar, Telegram Premium Animatsion Emojilar, Himoyalangan kanallar, AI Paraphraser, Video Watermark.",
        "stars": 300,
        "days": 30
    },
    "private_50": {
        "title": "14 Kunlik Kirish (Private Unlock)",
        "description": "Yopiq botdan 14 kun davomida to'liq foydalanish va sinov muddati huquqi.",
        "stars": 50,
        "days": 14
    }
}

def format_uz_date(iso_str: str) -> str:
    """Formats ISO datetime string (YYYY-MM-DD... or DD.MM.YYYY) into Uzbek standard date: DD-Oy, YYYY-yil"""
    if not iso_str:
        return "N/A"
    months_uz = [
        "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
        "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"
    ]
    try:
        dt_part = iso_str.split("T")[0] if "T" in iso_str else iso_str.split(" ")[0]
        if "." in dt_part and "-" not in dt_part:
            parts = dt_part.split(".")
            if len(parts) == 3:
                day, month, year = parts
                m_idx = int(month) - 1
                if 0 <= m_idx < 12:
                    return f"{int(day)}-{months_uz[m_idx]}, {year}-yil"
        year, month, day = dt_part.split("-")
        m_idx = int(month) - 1
        if 0 <= m_idx < 12:
            return f"{int(day)}-{months_uz[m_idx]}, {year}-yil"
        return dt_part
    except Exception:
        return iso_str[:10] if len(iso_str) >= 10 else iso_str

from bot.utils import safe_answer, html_escape

@router.callback_query(F.data.in_(["menu_stars", "menu_billing"]))
@router.message(Command("stars"))
@router.message(Command("billing"))
@router.message(Command("tariflar"))
@router.message(F.text.contains("Tariflar"))
async def cb_billing_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    sub = await db_manager.get_user_subscription(user_id)

    if user_id in settings.admin_ids or await db_manager.is_admin(user_id):
        tier_badge = f"{CROWN} <b>VIP Cheksiz (Super Admin Maxsus Tarifi — Cheklovlarsiz)</b>"
    elif sub.tier == "vip" and sub.is_active:
        exp_str = format_uz_date(sub.expires_at)
        tier_badge = f"{CROWN} <b>VIP Cheksiz</b> <i>({exp_str} gacha)</i>"
    elif sub.tier == "pro" and sub.is_active:
        exp_str = format_uz_date(sub.expires_at)
        tier_badge = f"{STARS} <b>Pro Tarif</b> <i>({exp_str} gacha)</i>"
    elif sub.tier == "free" and sub.is_trial_active:
        trial_str = format_uz_date(sub.trial_expires_at)
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
├ {VIDEO} <b>Real Estate Auto-Story Cloner ($700+)</b> (Playwright 4K kollaj, Ken Burns video, Prime Time navbat)
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

    import json as _json
    invoice_payload = _json.dumps({"t": tier, "u": callback.from_user.id})
    target_bot = bot or callback.bot
    try:
        await target_bot.send_invoice(
            chat_id=callback.from_user.id,
            title=plan["title"],
            description=plan["description"],
            payload=invoice_payload,
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

@router.callback_query(F.data == "private_unlock_stars_50")
async def cb_private_unlock_stars_50(callback: CallbackQuery, bot: Bot = None):
    plan = PLANS["private_50"]
    prices = [LabeledPrice(label=plan["title"], amount=plan["stars"])]

    await safe_answer(callback, "To'lov cheki yuborilmoqda...")

    import json as _json
    invoice_payload = _json.dumps({"t": "private_50", "u": callback.from_user.id})
    target_bot = bot or callback.bot
    try:
        await target_bot.send_invoice(
            chat_id=callback.from_user.id,
            title=plan["title"],
            description=plan["description"],
            payload=invoice_payload,
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="private_50"
        )
    except Exception as e:
        logger.error(f"Failed to create 50 Stars invoice: {e}")
        await callback.message.answer(
            f"{ERROR} <b>Hisob-faktura yaratishda xatolik:</b> {e}\n\nIltimos, qaytadan urinib ko'ring yoki administratorga murojaat qiling.",
            parse_mode="HTML"
        )

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    import json as _json
    payload = pre_checkout_query.invoice_payload or ""

    # Parse JSON payload (new format) or legacy underscore format
    tier = None
    payload_user_id = None
    try:
        parsed = _json.loads(payload)
        tier = parsed.get("t")
        payload_user_id = int(parsed.get("u", 0))
    except (ValueError, KeyError, _json.JSONDecodeError):
        # Backward compat: legacy format "stars_plan_{tier}_{user_id}"
        if payload.startswith("stars_plan_"):
            parts = payload.split("_")
            if len(parts) >= 4:
                try:
                    tier = parts[2]
                    payload_user_id = int(parts[3])
                except (ValueError, IndexError):
                    pass

    if not tier or payload_user_id is None:
        await pre_checkout_query.answer(ok=False, error_message="Noto'g'ri to'lov ma'lumoti!")
        return

    if payload_user_id != pre_checkout_query.from_user.id:
        await pre_checkout_query.answer(ok=False, error_message="Foydalanuvchi mos kelmadi!")
        return

    expected_plan = PLANS.get(tier)
    if not expected_plan or pre_checkout_query.total_amount != expected_plan["stars"]:
        await pre_checkout_query.answer(ok=False, error_message="Tarif summasi mos kelmadi!")
        return

    await pre_checkout_query.answer(ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""

    import json as _json
    tier = None
    user_id = None
    try:
        parsed = _json.loads(payload)
        tier = parsed.get("t")
        user_id = int(parsed.get("u", 0))
    except (ValueError, KeyError, _json.JSONDecodeError):
        # Backward compat: legacy format "stars_plan_{tier}_{user_id}"
        if payload.startswith("stars_plan_"):
            try:
                parts = payload.split("_")
                tier = parts[2]
                user_id = int(parts[3])
            except Exception as e:
                logger.error(f"Error parsing legacy payment payload '{payload}': {e}")
                return
        else:
            logger.warning(f"Unknown payment payload format: {payload}")
            return

    if not tier or not user_id:
        logger.warning(f"Failed to extract tier or user_id from payment payload: {payload}")
        return

    plan = PLANS.get(tier)
    if not plan:
        logger.warning(f"Unknown payment tier in payload: {tier}")
        return

    if payment.total_amount < plan["stars"]:
        logger.error(
            f"Underpaid payment received for user {user_id}: "
            f"received {payment.total_amount} Stars, expected at least {plan['stars']} Stars"
        )
        return

    # Check for payment idempotency to prevent duplicate notification or duplicate processing
    if payment.telegram_payment_charge_id and await db_manager.is_payment_processed(payment.telegram_payment_charge_id):
        logger.warning(f"Duplicate payment replay for charge_id {payment.telegram_payment_charge_id} from user {user_id}. Skipping duplicate alert.")
        await message.answer(
            f"{INFO} <b>Ushbu to'lov (<code>{payment.telegram_payment_charge_id}</code>) allaqachon muvaffaqiyatli qabul qilingan va balansingizga kiritilgan.</b>",
            parse_mode="HTML"
        )

        return

    try:
        if tier == "private_50":
            sub = await db_manager.activate_subscription(
                user_id=user_id,
                tier="free",
                stars=payment.total_amount,
                charge_id=payment.telegram_payment_charge_id,
                days=14
            )
            await db_manager.add_user_to_whitelist(
                user_id=user_id,
                added_by=0,
                source="stars_50",
                note="50 Stars orqali 14 kunlik sinov faollashtirildi"
            )
            exp_str = format_uz_date(sub.trial_expires_at) if sub.trial_expires_at else "14 kun"
            text = f"""
{SUCCESS} <b>50 Stars To'lovingiz Muvaffaqiyatli Qabul Qilindi!</b>

├ {STARS} <b>Faollashtirildi:</b> 14 Kunlik Kirish (Free Tier)
├ {CALENDAR} <b>Amal qilish muddati:</b> {exp_str} gacha (14 kun)
├ {STARS} <b>To'langan summa:</b> {payment.total_amount} Stars
└ {DIAMOND} <b>Tranzaksiya ID:</b> <code>{payment.telegram_payment_charge_id}</code>

<i>Botdan to'liq foydalanish imkoniyati ochildi! Kanallaringizni bog'lashni boshlashingiz mumkin.</i>
"""
            kb = get_back_to_main_keyboard()
        else:
            sub = await db_manager.activate_subscription(
                user_id=user_id,
                tier=tier,
                stars=payment.total_amount,
                charge_id=payment.telegram_payment_charge_id,
                days=plan["days"]
            )

            exp_str = format_uz_date(sub.expires_at) if sub.expires_at else "30 kun"

            text = f"""
{SUCCESS} <b>To'lovingiz muvaffaqiyatli qabul qilindi!</b>

├ {CROWN if tier == 'vip' else STARS} <b>Faollashtirilgan tarif:</b> {tier.upper()}
├ {CALENDAR} <b>Amal qilish muddati:</b> {exp_str} gacha (30 kun)
├ {STARS} <b>To'langan summa:</b> {payment.total_amount} Stars
└ {DIAMOND} <b>Tranzaksiya ID:</b> <code>{payment.telegram_payment_charge_id}</code>

<i>Barcha yangi imkoniyatlar kanallaringiz uchun darhol ishga tushirildi!</i>
"""
            if tier == "vip":
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Istoriya Klonerni Ochish",
                            style="success",
                            icon_custom_emoji_id=ID_FLASH,
                            callback_data="story_main_menu"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Asosiy Menyu",
                            style="danger",
                            icon_custom_emoji_id=ID_HOME,
                            callback_data="menu_main"
                        )
                    ]
                ])
            else:
                kb = get_back_to_main_keyboard()
    except Exception as act_err:
        logger.critical(f"Critical error activating subscription for user {user_id}: {act_err}", exc_info=True)
        await message.answer(
            f"⚠️ <b>To'lovingiz qabul qilindi (Tranzaksiya ID: <code>{payment.telegram_payment_charge_id}</code>), ammo tizimda faollashtirishda vaqtinchalik nosozlik yuz berdi.</b>\n\n"
            f"Xavotir olmang, to'lovingiz xavfsiz saqlangan. Iltimos, administrator bilan bog'laning.",
            parse_mode="HTML"
        )
        return
    try:
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as send_err:
        logger.warning(f"Could not send receipt to user {user_id}: {send_err}")

    # Notify admins of payment
    try:
        from config.settings import settings
        user_name = html_escape(message.from_user.full_name) if message.from_user else "Noma'lum"
        admin_notify_text = (
            f"{STARS} <b>Yangi Stars To'lovi Qabul Qilindi!</b>\n\n"
            f"├ <b>Foydalanuvchi:</b> <code>{user_id}</code> ({user_name})\n"
            f"├ <b>Tarif:</b> {tier.upper()}\n"
            f"├ <b>Summa:</b> {payment.total_amount} Stars\n"
            f"└ <b>Tranzaksiya:</b> <code>{payment.telegram_payment_charge_id}</code>"
        )
        for admin_id in settings.admin_ids:
            try:
                await message.bot.send_message(chat_id=admin_id, text=admin_notify_text, parse_mode="HTML")
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
    except Exception as e:
        logger.error(f"Failed to notify admins of stars payment: {e}")
