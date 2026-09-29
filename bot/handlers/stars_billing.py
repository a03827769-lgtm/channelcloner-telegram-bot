import json as _json
import logging
from typing import Optional, Tuple, Union
from aiogram import Router, F, Bot
from aiogram.enums import ChatType
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
from config.plans import PLANS
from database.db_manager import db_manager
from bot.access import is_admin_user
from config.settings import settings
from bot.keyboards.stars_keyboards import get_stars_plans_keyboard, active_paid_tier, is_lower_tier_purchase
from bot.keyboards.inline_buttons import MENU_BILLING, get_back_to_main_keyboard
from services.custom_emojis import (
    STARS, CROWN, MEDAL_BRONZE, DIAMOND, TRANSLATE, MONEY,
    FLASH, IMAGE, LOCK_UNLOCKED, ROCKET, ERROR, WARN,
    SUCCESS, BOX, CLEAN, SIGNATURE, LOADING, STAR_SPARKLE, CALENDAR,
    SERVER_CPU, VIDEO, INFO, ID_HOME, ID_FLASH
)
from bot.utils import safe_answer, html_escape, edit_or_send, format_uz_date


logger = logging.getLogger(__name__)

# Payment events (checkout queries, successful and refunded payments) are handled on this parent router
# for every chat, so a payment is never lost to a chat-type filter. The plans menu itself lives in the
# private-chat-only child router.
router = Router(name="stars_billing_router")
menu_router = Router(name="stars_billing_menu_router")
menu_router.message.filter(F.chat.type == ChatType.PRIVATE)
menu_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
router.include_router(menu_router)

# Purchasable subscription plans (the private unlock has its own button in the private-mode prompt)
SUBSCRIPTION_PLAN_KEYS = ("pro", "vip")

def _purchase_notice(current_tier: Optional[str]) -> str:
    """What buying a plan does right now (shown above the purchase buttons)."""
    if current_tier == "vip":
        return f"{INFO} <i>VIP tarifingiz faol: VIP sotib olish muddatni {PLANS['vip']['days']} kunga uzaytiradi. Pro tarif VIP faol bo'lganda taklif qilinmaydi.</i>"
    if current_tier == "pro":
        return (f"{INFO} <i>Pro tarifingiz faol: Pro sotib olish muddatni {PLANS['pro']['days']} kunga uzaytiradi, "
                f"VIP ga o'tsangiz esa qolgan Pro kunlaringiz narx nisbatida VIP kunlariga aylantiriladi.</i>")
    return ""

@menu_router.callback_query(F.data.in_(["menu_stars", "menu_billing"]))
@menu_router.message(Command("stars"))
@menu_router.message(Command("billing"))
@menu_router.message(Command("tariflar"))
@menu_router.message(F.text == MENU_BILLING, ~F.forward_origin)
async def cb_billing_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    sub = await db_manager.get_user_subscription(user_id)
    is_admin = is_admin_user(user_id)
    current_tier = None if is_admin else active_paid_tier(sub)

    if is_admin:
        tier_badge = f"{CROWN} <b>VIP Cheksiz (Super Admin Maxsus Tarifi — Cheklovlarsiz)</b>"
    elif current_tier == "vip":
        exp_str = format_uz_date(sub.expires_at)
        tier_badge = f"{CROWN} <b>VIP Cheksiz</b> <i>({exp_str} gacha)</i>"
    elif current_tier == "pro":
        exp_str = format_uz_date(sub.expires_at)
        tier_badge = f"{STARS} <b>Pro Tarif</b> <i>({exp_str} gacha)</i>"
    elif sub.tier == "free" and sub.is_trial_active:
        trial_str = format_uz_date(sub.trial_expires_at)
        tier_badge = f"{MEDAL_BRONZE} <b>Bepul Sinov (14 Kun)</b> <i>({trial_str} gacha faol)</i>"
    else:
        tier_badge = f"{ERROR} <b>Sinov Muddati Tugagan</b> <i>(Tarif tanlang)</i>"

    notice = _purchase_notice(current_tier)
    footer = ("Siz administratorsiz — barcha imkoniyatlar cheklovsiz ochiq." if is_admin
              else "Tarifni faollashtirish uchun quyidagi tugmalardan birini tanlang:")
    pro, vip = PLANS["pro"], PLANS["vip"]
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

<b>{STARS} Pro Tarif — {pro['stars']} Stars ({pro['days']} kun):</b>
├ {BOX} 5 tagacha faol kanal juftligi
├ {SERVER_CPU} <b>AI Content Paraphraser</b> (Rasmiy, Hype, Tezis uslublari)
├ {IMAGE} <b>Rasm va Video Watermark</b> (FFmpeg Logo urish)
├ {MONEY} <b>Dynamic Affiliate & CTA Tugmalar</b>
├ {TRANSLATE} Avto-Tarjima + Referal Almashtirgich
└ {FLASH} Tezkor xizmat ko'rsatish

<b>{CROWN} VIP Cheksiz — {vip['stars']} Stars ({vip['days']} kun):</b>
├ {BOX} <b>Cheksiz kanallar juftligi (999 ta)</b>
├ {VIDEO} <b>Real Estate Auto-Story Cloner ($700+)</b> (4K kollaj, Ken Burns video, Prime Time navbat)
├ {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar</b> (Oddiy emojilar avto premium animatsion bo'ladi)
├ {LOCK_UNLOCKED} <b>Himoyalangan (Protected) yopiq kanallarni ko'chirish</b>
├ {SERVER_CPU} Barcha AI Paraphraser va Video Watermark imkoniyatlari
└ {ROCKET} Eng yuqori server ustuvorligi (0 soniya kechikish)

{notice}
<i>{footer}</i>
"""
    reply_markup = get_stars_plans_keyboard(sub, is_admin=is_admin)
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await edit_or_send(event, text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=reply_markup)

async def _send_plan_invoice(callback: CallbackQuery, bot: Optional[Bot], plan_key: str, start_parameter: str):
    plan = PLANS[plan_key]
    prices = [LabeledPrice(label=plan["title"], amount=plan["stars"])]

    await safe_answer(callback, "To'lov cheki yuborilmoqda...")

    invoice_payload = _json.dumps({"t": plan_key, "u": callback.from_user.id})
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
            start_parameter=start_parameter
        )
    except Exception as e:
        logger.error(f"Failed to create Telegram Stars invoice ({plan_key}) for {callback.from_user.id}: {e}")
        try:
            await target_bot.send_message(
                chat_id=callback.from_user.id,
                text=f"{ERROR} <b>Hisob-faktura yaratishda xatolik yuz berdi.</b>\n\nIltimos, qaytadan urinib ko'ring yoki administratorga murojaat qiling.",
                parse_mode="HTML"
            )
        except Exception:
            logger.debug("Could not report the invoice error", exc_info=True)

@menu_router.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery, bot: Bot = None):
    tier = callback.data.replace("buy_plan_", "")
    if tier not in SUBSCRIPTION_PLAN_KEYS:
        await safe_answer(callback, "Noto'g'ri tarif!", show_alert=True)
        return

    # Old menus may still show a lower tier: while a higher plan is active it is not sold (the payment
    # could only be converted into extra time of the active plan)
    sub = await db_manager.get_user_subscription(callback.from_user.id)
    if is_lower_tier_purchase(tier, sub):
        await safe_answer(
            callback,
            f"Sizda {sub.tier.upper()} tarifi faol — {tier.upper()} tarifini sotib olish shart emas. Faol tarifingizni uzaytirishingiz mumkin.",
            show_alert=True
        )
        return

    await _send_plan_invoice(callback, bot, tier, f"buy_{tier}")

@menu_router.callback_query(F.data == "private_unlock_stars_50")
async def cb_private_unlock_stars_50(callback: CallbackQuery, bot: Bot = None):
    await _send_plan_invoice(callback, bot, "private_50", "private_50")

def _parse_invoice_payload(payload: str):
    """Returns (kind, user_id, data) from a JSON invoice payload or the legacy `stars_plan_{tier}_{uid}` format."""
    try:
        parsed = _json.loads(payload)
        if isinstance(parsed, dict):
            return parsed.get("t"), int(parsed.get("u", 0) or 0), parsed
    except (ValueError, TypeError):
        pass
    if payload.startswith("stars_plan_"):
        parts = payload.split("_")
        if len(parts) >= 4:
            try:
                return parts[2], int(parts[3]), {}
            except ValueError:
                logger.debug("Failed parsing legacy invoice payload", exc_info=True)
    return None, None, {}

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    tier, payload_user_id, data = _parse_invoice_payload(pre_checkout_query.invoice_payload or "")

    if not tier or not payload_user_id:
        await pre_checkout_query.answer(ok=False, error_message="Noto'g'ri to'lov ma'lumoti!")
        return

    if payload_user_id != pre_checkout_query.from_user.id:
        await pre_checkout_query.answer(ok=False, error_message="Foydalanuvchi mos kelmadi!")
        return

    if tier == "store":
        try:
            order = await db_manager.get_store_order(int(data.get("o") or 0))
        except (TypeError, ValueError):
            order = None
        if (not order or order.user_id != payload_user_id or order.status != "awaiting_payment"
                or pre_checkout_query.total_amount != order.price_stars):
            await pre_checkout_query.answer(ok=False, error_message="Buyurtma topilmadi yoki allaqachon to'langan!")
            return
        await pre_checkout_query.answer(ok=True)
        return

    expected_plan = PLANS.get(tier)
    if not expected_plan or pre_checkout_query.total_amount != expected_plan["stars"]:
        await pre_checkout_query.answer(ok=False, error_message="Tarif summasi mos kelmadi!")
        return

    await pre_checkout_query.answer(ok=True)

async def _handle_store_payment(message: Message, payment, user_id: int, data: dict):
    """Marks a store order paid (idempotent on charge id) and routes it for fulfillment."""
    from services.supplier_service import supplier_service
    try:
        order_id = int(data.get("o") or 0)
    except (TypeError, ValueError):
        order_id = 0
    paid = await db_manager.mark_store_order_paid(
        order_id=order_id,
        user_id=user_id,
        charge_id=payment.telegram_payment_charge_id,
        amount=payment.total_amount
    )
    if not paid:
        logger.warning(f"Store payment for order #{order_id} ignored (replay or invalid state), charge {payment.telegram_payment_charge_id}")
        return
    sender = message.from_user
    result = await supplier_service.fulfill_paid_order(
        order_id,
        user_full_name=sender.full_name if sender else "",
        user_username=(sender.username or "") if sender else ""
    )
    try:
        await message.answer(
            f"{SUCCESS} <b>To'lov qabul qilindi!</b>\n\n"
            f"├ <b>Buyurtma:</b> #{order_id}\n"
            f"├ <b>Summa:</b> {payment.total_amount} Stars\n"
            f"└ <b>Holat:</b> {html_escape(result.get('message', ''))}",
            parse_mode="HTML"
        )
    except Exception:
        logger.debug("Could not send store receipt", exc_info=True)

async def _notify_admins(bot: Bot, text: str):
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    tier, user_id, data = _parse_invoice_payload(payload)

    if not tier or not user_id:
        logger.warning(f"Failed to extract tier or user_id from payment payload: {payload}")
        return

    if tier == "store":
        await _handle_store_payment(message, payment, user_id, data)
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

    charge_id_safe = html_escape(payment.telegram_payment_charge_id)
    # Check for payment idempotency to prevent duplicate notification or duplicate processing
    if payment.telegram_payment_charge_id and await db_manager.is_payment_processed(payment.telegram_payment_charge_id):
        logger.warning(f"Duplicate payment replay for charge_id {payment.telegram_payment_charge_id} from user {user_id}. Skipping duplicate alert.")
        await message.answer(
            f"{INFO} <b>Ushbu to'lov (<code>{charge_id_safe}</code>) allaqachon muvaffaqiyatli qabul qilingan va balansingizga kiritilgan.</b>",
            parse_mode="HTML"
        )

        return

    try:
        sub = await db_manager.activate_subscription(
            user_id=user_id,
            tier=plan["tier"],
            stars=payment.total_amount,
            charge_id=payment.telegram_payment_charge_id,
            days=plan["days"]
        )
        if plan["tier"] == "free":
            await db_manager.add_user_to_whitelist(
                user_id=user_id,
                added_by=0,
                source="stars_50",
                note="50 Stars orqali 14 kunlik sinov faollashtirildi"
            )
            exp_str = format_uz_date(sub.trial_expires_at) if sub.trial_expires_at else f"{plan['days']} kun"
            text = f"""
{SUCCESS} <b>{plan['stars']} Stars To'lovingiz Muvaffaqiyatli Qabul Qilindi!</b>

├ {STARS} <b>Faollashtirildi:</b> {plan['days']} Kunlik Kirish (Free Tier)
├ {CALENDAR} <b>Amal qilish muddati:</b> {exp_str} gacha
├ {STARS} <b>To'langan summa:</b> {payment.total_amount} Stars
└ {DIAMOND} <b>Tranzaksiya ID:</b> <code>{charge_id_safe}</code>

<i>Botdan to'liq foydalanish imkoniyati ochildi! Kanallaringizni bog'lashni boshlashingiz mumkin.</i>
"""
            kb = get_back_to_main_keyboard()
        else:
            exp_str = format_uz_date(sub.expires_at) if sub.expires_at else f"{plan['days']} kun"
            # A lower tier bought while a higher one was active is converted into extra time of the
            # active tier, so the receipt shows the tier the user actually has now
            active_tier = sub.tier if sub.tier in ("pro", "vip") else plan["tier"]
            converted_note = ""
            if active_tier != plan["tier"]:
                converted_note = f"\n{INFO} <i>{plan['tier'].upper()} to'lovi faol {active_tier.upper()} tarifingizga qo'shimcha muddat sifatida qo'shildi.</i>\n"

            text = f"""
{SUCCESS} <b>To'lovingiz muvaffaqiyatli qabul qilindi!</b>

├ {CROWN if active_tier == 'vip' else STARS} <b>Faol tarif:</b> {active_tier.upper()}
├ {CALENDAR} <b>Amal qilish muddati:</b> {exp_str} gacha
├ {STARS} <b>To'langan summa:</b> {payment.total_amount} Stars
└ {DIAMOND} <b>Tranzaksiya ID:</b> <code>{charge_id_safe}</code>
{converted_note}
<i>Barcha yangi imkoniyatlar kanallaringiz uchun darhol ishga tushirildi!</i>
"""
            if active_tier == "vip":
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
            f"{WARN} <b>To'lovingiz qabul qilindi (Tranzaksiya ID: <code>{charge_id_safe}</code>), ammo tizimda faollashtirishda vaqtinchalik nosozlik yuz berdi.</b>\n\n"
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
        user_name = html_escape(message.from_user.full_name) if message.from_user else "Noma'lum"
        admin_notify_text = (
            f"{STARS} <b>Yangi Stars To'lovi Qabul Qilindi!</b>\n\n"
            f"├ <b>Foydalanuvchi:</b> <code>{user_id}</code> ({user_name})\n"
            f"├ <b>Tarif:</b> {html_escape(tier.upper())}\n"
            f"├ <b>Summa:</b> {payment.total_amount} Stars\n"
            f"└ <b>Tranzaksiya:</b> <code>{charge_id_safe}</code>"
        )
        await _notify_admins(message.bot, admin_notify_text)
    except Exception as e:
        logger.error(f"Failed to notify admins of stars payment: {e}")

async def _get_payment_record(charge_id: str) -> Optional[Tuple[int, str, int]]:
    """(user_id, tier, amount) of a recorded Stars payment, or None when the charge is unknown."""
    if not charge_id:
        return None
    async with db_manager.get_connection() as db:
        cursor = await db.execute(
            "SELECT user_id, tier, amount FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,)
        )
        row = await cursor.fetchone()
    return (int(row[0]), str(row[1]), int(row[2] or 0)) if row else None

@router.message(F.refunded_payment)
async def process_refunded_payment(message: Message):
    """A Stars payment was refunded: take back what it bought (plan, private unlock or store order)."""
    refund = message.refunded_payment
    charge_id = refund.telegram_payment_charge_id or ""
    kind, payload_user_id, data = _parse_invoice_payload(refund.invoice_payload or "")
    record = await _get_payment_record(charge_id)
    user_id = record[0] if record else payload_user_id
    paid_tier = record[1] if record else None
    charge_id_safe = html_escape(charge_id)

    if not user_id:
        logger.warning(f"Refund for unknown payment {charge_id} ignored (payload: {refund.invoice_payload!r})")
        return

    if paid_tier == "store" or (paid_tier is None and kind == "store"):
        try:
            order_id = int(data.get("o") or 0)
        except (TypeError, ValueError):
            order_id = 0
        order = await db_manager.get_store_order(order_id) if order_id else None
        if order and order.user_id == user_id and order.status != "refunded":
            await db_manager.update_store_order(order_id, status="refunded", note=f"Stars refund {charge_id}")
        user_text = f"{INFO} <b>Buyurtma #{order_id} uchun {refund.total_amount} Stars qaytarildi.</b>"
        what = f"Do'kon buyurtmasi #{order_id}"
    elif paid_tier in ("pro", "vip"):
        await db_manager.revoke_subscription(user_id)
        user_text = (
            f"{WARN} <b>{refund.total_amount} Stars to'lovingiz qaytarildi.</b>\n\n"
            f"Shu to'lov bilan faollashtirilgan {paid_tier.upper()} tarifi bekor qilindi. "
            f"Tarifni qayta faollashtirish uchun <b>{STARS} Tariflar & Obuna</b> bo'limiga o'ting."
        )
        what = f"{paid_tier.upper()} tarifi bekor qilindi"
    elif paid_tier == "free":
        whitelisted = await db_manager.get_whitelisted_users(source="stars_50")
        if any(row.get("user_id") == user_id for row in whitelisted):
            await db_manager.remove_user_from_whitelist(user_id)
        user_text = (
            f"{WARN} <b>{refund.total_amount} Stars to'lovingiz qaytarildi.</b>\n\n"
            f"To'lov orqali berilgan yopiq botga kirish ruxsati bekor qilindi."
        )
        what = "Yopiq botga kirish (50 Stars) bekor qilindi"
    else:
        # The payment never activated anything (unknown charge): nothing to take back
        user_text = f"{INFO} <b>{refund.total_amount} Stars to'lovingiz qaytarildi.</b>"
        what = "Faollashtirilmagan to'lov"

    logger.warning(f"Stars refund processed for user {user_id}, charge {charge_id}: {what}")
    try:
        await message.bot.send_message(chat_id=user_id, text=user_text, parse_mode="HTML")
    except Exception:
        logger.debug("Could not notify the user about the refund", exc_info=True)
    await _notify_admins(
        message.bot,
        f"{WARN} <b>Stars to'lovi qaytarildi (refund)</b>\n\n"
        f"├ <b>Foydalanuvchi:</b> <code>{user_id}</code>\n"
        f"├ <b>Summa:</b> {refund.total_amount} Stars\n"
        f"├ <b>Natija:</b> {html_escape(what)}\n"
        f"└ <b>Tranzaksiya:</b> <code>{charge_id_safe}</code>"
    )
