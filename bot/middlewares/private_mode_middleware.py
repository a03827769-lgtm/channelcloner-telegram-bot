import re
import time
import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import (
    TelegramObject, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from database.db_manager import db_manager
from services.custom_emojis import (
    LOCK_LOCKED, STARS, ID_STARS, ID_REFRESH, ID_SUPPORT, SHIELD, INFO, SUPPORT
)
from bot.utils import edit_or_send, safe_answer, html_escape, is_non_private_chat_event, is_payment_event

logger = logging.getLogger(__name__)

# Telegram usernames: 5-32 characters, starting with a letter
_SUPPORT_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")

# Callbacks a locked-out user may still use: the 50 Stars unlock, plan purchases and the access re-check
_ALLOWED_CALLBACKS = ("private_unlock_stars_50", "check_private_access")

async def get_private_mode_prompt(support_username: str) -> tuple[str, InlineKeyboardMarkup]:
    clean_support = (support_username or "admin").strip().lstrip("@")
    # The support username is configured by admins; only a well-formed one becomes a t.me link
    valid_support = bool(_SUPPORT_USERNAME_RE.fullmatch(clean_support))
    support_line = f" (<code>@{html_escape(clean_support)}</code>)" if valid_support else ""
    text = f"""
{LOCK_LOCKED} <b>Bot Shaxsiy (Yopiq) Rejimda</b>
━━━━━━━━━━━━━━━━━━━━━━━

{SHIELD} <i>Ushbu bot faqat ma'muriyat tomonidan tasdiqlangan hamkorlar va ruxsat berilgan a'zolar uchun xizmat ko'rsatadi.</i>

{INFO} <b>Botdan foydalanishni boshlash usullari:</b>

1. {SUPPORT} <b>Admin bilan bog'lanish{support_line}:</b>
   └ Ma'muriyatga murojaat qilib, botga kirish ruxsatini olish.

2. {STARS} <b>50 Telegram Stars orqali tezkor kirish:</b>
   └ Kutmasdan, darhol {STARS} <b>50 Stars</b> to'lab, <b>14 kunlik to'liq sinov muddati (Free Tier)</b> bilan botni ochish!

━━━━━━━━━━━━━━━━━━━━━━━
"""
    rows = [
        [
            InlineKeyboardButton(
                text="50 Stars To'lab Boshlash (14 kun)",
                icon_custom_emoji_id=ID_STARS,
                style="success",
                callback_data="private_unlock_stars_50"
            )
        ]
    ]
    if valid_support:
        rows.append([
            InlineKeyboardButton(
                text=f"Admin bilan bog'lanish (@{clean_support})",
                icon_custom_emoji_id=ID_SUPPORT,
                style="primary",
                url=f"https://t.me/{clean_support}"
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="Ruxsatni qayta tekshirish",
            icon_custom_emoji_id=ID_REFRESH,
            style="primary",
            callback_data="check_private_access"
        )
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

class PrivateModeGatekeeperMiddleware(BaseMiddleware):
    """
    Guards the bot when in Private Mode:
    - Automatically allows admins, whitelisted users, active subscribers, and users who were already using the bot.
    - Allows Stars payment callbacks, invoices and payment service messages (successful/refunded payments)
      for the 50 Stars unlock. Checkout queries never pass this middleware (it is registered for messages
      and callback queries only), so payments are never blocked.
    - Prompts new unauthorized users with the private access explanation — at most once per
      PROMPT_COOLDOWN_SECONDS per user, so repeated messages do not flood the chat with prompts.
    - Group/channel traffic (discussion-group moderation) is passed through untouched.
    """
    PROMPT_COOLDOWN_SECONDS = 30.0

    def __init__(self):
        self._last_prompt: Dict[int, float] = {}

    def _prompt_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        if len(self._last_prompt) > 10000:
            cutoff = now - self.PROMPT_COOLDOWN_SECONDS
            self._last_prompt = {uid: t for uid, t in self._last_prompt.items() if t > cutoff}
        last = self._last_prompt.get(user_id)
        if last is not None and now - last < self.PROMPT_COOLDOWN_SECONDS:
            return False
        self._last_prompt[user_id] = now
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if is_payment_event(event) or is_non_private_chat_event(event, data):
            return await handler(event, data)

        from_user = data.get("event_from_user")
        if not from_user and hasattr(event, "from_user"):
            from_user = getattr(event, "from_user")

        if not from_user or getattr(from_user, "is_bot", False):
            return await handler(event, data)

        # Allow bypass for 50 Stars unlock / plan purchase callbacks
        if isinstance(event, CallbackQuery):
            if event.data in _ALLOWED_CALLBACKS or (event.data and event.data.startswith("buy_plan_")):
                return await handler(event, data)

        try:
            is_allowed = await db_manager.can_user_access_bot(from_user.id)
        except Exception as e:
            logger.error(f"Error checking bot access permissions for user {from_user.id}: {e}")
            is_allowed = False

        if is_allowed:
            return await handler(event, data)

        # User is unauthorized in private mode: display prompt (rate-limited per user)
        show_prompt = self._prompt_allowed(from_user.id)
        if isinstance(event, CallbackQuery):
            if not show_prompt:
                await safe_answer(event, "Bot yopiq rejimda. Kirish uchun yuqoridagi tugmalardan foydalaning.", show_alert=True)
                return None
            await safe_answer(event)

        if not show_prompt:
            return None

        try:
            support_username = await db_manager.get_support_username()
        except Exception:
            support_username = "admin"
        text, kb = await get_private_mode_prompt(support_username)

        try:
            if isinstance(event, CallbackQuery):
                await edit_or_send(event, text, parse_mode="HTML", reply_markup=kb)
            elif isinstance(event, Message):
                await event.answer(text=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("Could not deliver the private mode prompt", exc_info=True)

        return None
