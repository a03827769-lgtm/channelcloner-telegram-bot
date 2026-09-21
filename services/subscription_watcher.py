import asyncio
import html
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db_manager import db_manager
from services.custom_emojis import (
    LOADING, STARS, CROWN, STAR_SPARKLE, WARN, VIDEO, ID_STARS, ID_CROWN, ID_SPARKLE, ID_FLASH
)

logger = logging.getLogger(__name__)

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
                logger.info(f"User {user_id} has blocked bot or chat is forbidden. Marking trial notified.")
                await db_manager.mark_trial_notified(user_id)
            except TelegramRetryAfter as e:
                logger.warning(f"FloodWait during trial notifications: sleeping {e.retry_after}s")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as err:
                err_msg = str(err).lower()
                logger.warning(f"Failed to send trial notification to user {user_id}: {err}")
                if any(k in err_msg for k in ["blocked", "not found", "deactivated", "initiate conversation", "forbidden"]):
                    await db_manager.mark_trial_notified(user_id)
            await asyncio.sleep(0.05)

    async def check_and_notify_expiring_paid_subs(self):
        expiring = await db_manager.get_expiring_paid_users_to_notify()
        if not expiring:
            return

        for user_id, full_name, tier, exp_at in expiring:
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"{tier.upper()} Tarifini Uzaytirish",
                            icon_custom_emoji_id=ID_CROWN if tier == "vip" else ID_STARS,
                            style="success",
                            callback_data=f"buy_plan_{tier}"
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
                safe_tier = html.escape(tier.upper())
                safe_exp = html.escape(str(exp_at)[:10])
                text = f"""
{LOADING} <b>Diqqat! Obunangiz muddati tugamoqda!</b>

Hurmatli <b>{safe_name}</b>, sizning <b>{safe_tier}</b> tarifingiz muddati tez orada tugaydi ({safe_exp}).

{WARN} <i>Kanal klonlash to'xtab qolmasligi uchun obunangizni oldindan uzaytirishingiz mumkin:</i>
"""
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb
                )
                await db_manager.mark_paid_sub_notified(user_id)
            except TelegramForbiddenError:
                logger.info(f"User {user_id} has blocked bot. Marking paid sub notified.")
                await db_manager.mark_paid_sub_notified(user_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as err:
                err_msg = str(err).lower()
                if any(k in err_msg for k in ["blocked", "not found", "deactivated", "initiate conversation", "forbidden"]):
                    await db_manager.mark_paid_sub_notified(user_id)
            await asyncio.sleep(0.05)

