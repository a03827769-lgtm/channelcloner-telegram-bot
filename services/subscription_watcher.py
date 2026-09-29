import asyncio
import functools
import html
import logging
import math
from datetime import datetime, timezone
from typing import Optional
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db_manager import db_manager
from database.models import Subscription
from services.custom_emojis import (
    LOADING, STARS, CROWN, STAR_SPARKLE, WARN, VIDEO, ID_STARS, ID_CROWN, ID_FLASH
)

logger = logging.getLogger(__name__)

# Errors meaning the user can never receive our messages again (bot blocked, account deleted, ...)
_UNREACHABLE_MARKERS = ("blocked", "not found", "deactivated", "initiate conversation", "forbidden")


def paid_plan_days_left(expires_at: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """Whole days until a paid plan ends (rounded up, at least 1 while it is still running); 0 once it has
    ended; None when the expiry date cannot be read. Stored dates are naive UTC ISO strings."""
    exp = Subscription._parse_iso_to_utc_naive(expires_at)
    if exp is None:
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    remaining = (exp - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining / 86400))


class SubscriptionWatcher:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self._task = None

    def start(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._watch_loop())
            logger.info("Subscription & 14-day trial watcher started.")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            logger.info("Subscription watcher stopped.")

    async def aclose(self, timeout: float = 5.0):
        """Stops the watcher and waits (bounded) until its loop has exited."""
        task = self._task
        self.stop()
        if task and not task.done():
            await asyncio.wait([task], timeout=timeout)

    async def _watch_loop(self):
        while self.is_running:
            try:
                await self.check_and_notify_expired_trials()
                await self.check_and_notify_expiring_paid_subs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in subscription watcher loop: {e}", exc_info=True)

            # Check every 5 minutes
            await asyncio.sleep(300)

    async def _handle_unreachable(self, user_id: int, mark_notified) -> None:
        """The user blocked the bot or deleted the account: stop notifying and exclude them from mailings."""
        try:
            await db_manager.mark_user_blocked(user_id)
        except Exception:
            logger.debug(f"Could not mark user {user_id} as blocked", exc_info=True)
        await mark_notified(user_id)

    async def check_and_notify_expired_trials(self):
        expired_users = await db_manager.get_expired_trial_users_to_notify()
        if not expired_users:
            return

        logger.info(f"Found {len(expired_users)} users with expired 14-day trial to notify.")

        for user_id, full_name in expired_users:
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Pro Tarif — 100 Stars (1 oy)",
                            icon_custom_emoji_id=ID_STARS,
                            style="primary",
                            callback_data="buy_plan_pro"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="VIP Cheksiz — 300 Stars (1 oy)",
                            icon_custom_emoji_id=ID_CROWN,
                            style="success",
                            callback_data="buy_plan_vip"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Barcha Tariflar",
                            icon_custom_emoji_id=ID_FLASH,
                            style="primary",
                            callback_data="menu_stars"
                        )
                    ]
                ])

                safe_name = html.escape(full_name or 'Foydalanuvchi')
                text = f"""
{LOADING} <b>14 Kunlik Bepul Sinov Muddati Yakunlandi!</b>

Hurmatli <b>{safe_name}</b>, botimizdan 14 kunlik bepul sinov muddatingiz o'z nihoyasiga yetdi.

{STARS} <b>Kanal klonlashni to'xtovsiz davom ettirish uchun tariflardan birini tanlang:</b>

├ {STARS} <b>Pro Tarif (100 Stars / oy):</b> 5 ta kanal, Avto-Tarjima, Referal link almashtirgich, Tarixni ko'chirish
└ {CROWN} <b>VIP Cheksiz (300 Stars / oy):</b> {VIDEO} <b>Real Estate Auto-Story Cloner ($700+)</b>, Cheksiz kanallar, {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar</b>, Watermark va eng yuqori tezlik!

<i>Tarifni darhol faollashtirish uchun pastdagi tugmani bosing:</i>
"""
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb
                )
                await db_manager.mark_trial_notified(user_id)
                logger.info(f"Successfully sent 14-day trial expiry notification to user {user_id}")
            except TelegramForbiddenError:
                logger.info(f"User {user_id} has blocked the bot. Marking as blocked and trial notified.")
                await self._handle_unreachable(user_id, db_manager.mark_trial_notified)
            except TelegramRetryAfter as e:
                logger.warning(f"FloodWait during trial notifications: sleeping {e.retry_after}s")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as err:
                err_msg = str(err).lower()
                logger.warning(f"Failed to send trial notification to user {user_id}: {err}")
                if any(k in err_msg for k in _UNREACHABLE_MARKERS):
                    await self._handle_unreachable(user_id, db_manager.mark_trial_notified)
            await asyncio.sleep(0.05)

    async def check_and_notify_expiring_paid_subs(self):
        """PRO/VIP plans ending within 3 days get a reminder; plans that already ended without one (bot was
        offline, very short grants) get an "expired" notice instead. One notification per paid period."""
        expiring = await db_manager.get_expiring_paid_users_to_notify()
        if not expiring:
            return

        for user_id, full_name, tier, exp_at in expiring:
            days_left = paid_plan_days_left(exp_at)
            # Only the notice that was due is recorded for an unreachable user: one who unblocks the bot
            # before the plan ends still gets the "expired" notice later
            mark_unreachable = functools.partial(db_manager.mark_paid_sub_notified, expired=(days_left == 0))
            try:
                tier_code = str(tier or "").lower()
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"{tier_code.upper()} Tarifini Uzaytirish",
                            icon_custom_emoji_id=ID_CROWN if tier_code == "vip" else ID_STARS,
                            style="success",
                            callback_data=f"buy_plan_{tier_code}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Barcha Tariflar",
                            icon_custom_emoji_id=ID_FLASH,
                            style="primary",
                            callback_data="menu_stars"
                        )
                    ]
                ])
                safe_name = html.escape(full_name or 'Foydalanuvchi')
                safe_tier = html.escape(tier_code.upper())
                safe_exp = html.escape(str(exp_at)[:10])
                if days_left == 0:
                    text = f"""
{WARN} <b>Obunangiz muddati tugadi!</b>

Hurmatli <b>{safe_name}</b>, sizning <b>{safe_tier}</b> tarifingiz muddati tugadi ({safe_exp}). Kanallaringizni klonlash to'xtatildi.

{STARS} <i>Klonlashni davom ettirish uchun tarifni qayta faollashtiring:</i>
"""
                else:
                    when = f"<b>{days_left} kundan keyin</b> tugaydi" if days_left else "tez orada tugaydi"
                    text = f"""
{LOADING} <b>Diqqat! Obunangiz muddati tugamoqda!</b>

Hurmatli <b>{safe_name}</b>, sizning <b>{safe_tier}</b> tarifingiz {when} ({safe_exp}).

{WARN} <i>Kanal klonlash to'xtab qolmasligi uchun obunangizni oldindan uzaytirishingiz mumkin:</i>
"""
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb
                )
                await db_manager.mark_paid_sub_notified(user_id, expired=(days_left == 0))
            except TelegramForbiddenError:
                logger.info(f"User {user_id} has blocked the bot. Marking as blocked and paid sub notified.")
                await self._handle_unreachable(user_id, mark_unreachable)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as err:
                err_msg = str(err).lower()
                logger.warning(f"Failed to send subscription notification to user {user_id}: {err}")
                if any(k in err_msg for k in _UNREACHABLE_MARKERS):
                    await self._handle_unreachable(user_id, mark_unreachable)
            await asyncio.sleep(0.05)
