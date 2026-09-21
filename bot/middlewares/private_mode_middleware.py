import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import (
    TelegramObject, Message, CallbackQuery, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from database.db_manager import db_manager
from services.custom_emojis import (
    LOCK_LOCKED, STARS, ID_STARS, ID_REFRESH, ID_SUPPORT, SHIELD, INFO, SUPPORT, KEY, clean_for_alert
)
from bot.utils import safe_answer

logger = logging.getLogger(__name__)

async def get_private_mode_prompt(support_username: str) -> tuple[str, InlineKeyboardMarkup]:
    clean_support = (support_username or "admin").strip().lstrip("@")
    text = f"""
{LOCK_LOCKED} <b>Bot Shaxsiy (Yopiq) Rejimda</b>
━━━━━━━━━━━━━━━━━━━━━━━

{SHIELD} <i>Ushbu bot faqat ma'muriyat tomonidan tasdiqlangan hamkorlar va ruxsat berilgan a'zolar uchun xizmat ko'rsatadi.</i>

{INFO} <b>Botdan foydalanishni boshlash usullari:</b>

1. {SUPPORT} <b>Admin bilan bog'lanish:</b>
   └ Ma'muriyatga murojaat qilib, botga kirish ruxsatini olish.

2. {STARS} <b>50 Telegram Stars orqali tezkor kirish:</b>
   └ Kutmasdan, darhol {STARS} <b>50 Stars</b> to'lab, <b>14 kunlik to'liq sinov muddati (Free Tier)</b> bilan botni ochish!

━━━━━━━━━━━━━━━━━━━━━━━
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="50 Stars To'lab Boshlash (14 kun)",
                icon_custom_emoji_id=ID_STARS,
                style="success",
                callback_data="private_unlock_stars_50"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Admin bilan bog'lanish (@{clean_support})",
                icon_custom_emoji_id=ID_SUPPORT,
                style="primary",
                url=f"https://t.me/{clean_support}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Ruxsatni qayta tekshirish",
                icon_custom_emoji_id=ID_REFRESH,
                style="primary",
                callback_data="check_private_access"
            )
        ]
    ])
    return text, kb

class PrivateModeGatekeeperMiddleware(BaseMiddleware):
    """
    Guards the bot when in Private Mode:
    - Automatically allows admins, whitelisted users, active subscribers, and users who were already using the bot.
    - Allows Stars payment callbacks & invoices for the 50 Stars unlock.
    - Prompts new unauthorized users with the private access explanation.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        from_user = data.get("event_from_user")
        if not from_user and hasattr(event, "from_user"):
            from_user = getattr(event, "from_user")

        if not from_user or getattr(from_user, "is_bot", False):
            return await handler(event, data)

        # Allow bypass for 50 Stars unlock callbacks & payment events
        if isinstance(event, PreCheckoutQuery):
            return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            if event.data in ("private_unlock_stars_50", "check_private_access") or (event.data and event.data.startswith("buy_plan_")):
                return await handler(event, data)
        elif isinstance(event, Message):
            if getattr(event, "successful_payment", None):
                return await handler(event, data)

        try:
            is_allowed = await db_manager.can_user_access_bot(from_user.id)
        except Exception as e:
            logger.error(f"Error checking bot access permissions for user {from_user.id}: {e}")
            is_allowed = False

        if is_allowed:
            return await handler(event, data)

        # User is unauthorized in private mode: display prompt
        try:
            support_username = await db_manager.get_support_username()
        except Exception:
            support_username = "admin"
        text, kb = await get_private_mode_prompt(support_username)

        if isinstance(event, CallbackQuery):
            await safe_answer(event)
            if event.message:
                try:
                    await event.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    await event.message.answer(text=text, parse_mode="HTML", reply_markup=kb)
            else:
                try:
                    bot = data.get("bot")
                    if bot and event.from_user:
                        await bot.send_message(chat_id=event.from_user.id, text=text, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        elif isinstance(event, Message):
            await event.answer(text=text, parse_mode="HTML", reply_markup=kb)

        return None
