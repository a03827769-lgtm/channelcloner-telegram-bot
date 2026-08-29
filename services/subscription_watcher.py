import asyncio
import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db_manager import db_manager
from services.custom_emojis import (
    LOADING, STARS, CROWN, STAR_SPARKLE, ID_STARS, ID_CROWN, ID_SPARKLE
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
                            callback_data="buy_plan_pro",
                            style="primary",
                            icon_custom_emoji_id=ID_STARS
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="VIP Cheksiz — 300 Stars (1 oy)",
                            callback_data="buy_plan_vip",
                            style="success",
                            icon_custom_emoji_id=ID_CROWN
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Barcha Tariflar",
                            callback_data="menu_stars",
                            style="primary",
                            icon_custom_emoji_id=ID_SPARKLE
                        )
                    ]
                ])

                text = f"""
{LOADING} <b>14 Kunlik Bepul Sinov Muddati Yakunlandi!</b>

Hurmatli <b>{full_name or 'Foydalanuvchi'}</b>, botimizdan 14 kunlik bepul sinov muddatingiz o'z nihoyasiga yetdi.

{STARS} <b>Kanal klonlashni to'xtovsiz davom ettirish uchun tariflardan birini tanlang:</b>

├ {STARS} <b>Pro Tarif (100 Stars / oy):</b> 5 ta kanal, Avto-Tarjima, Referal link almashtirgich, Tarixni ko'chirish
└ {CROWN} <b>VIP Cheksiz (300 Stars / oy):</b> Cheksiz kanallar, {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar</b>, Watermark va eng yuqori tezlik!

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
            except Exception as err:
                err_msg = str(err).lower()
                logger.warning(f"Failed to send trial notification to user {user_id}: {err}")
                if "blocked" in err_msg or "not found" in err_msg or "deactivated" in err_msg or "user is deactivated" in err_msg:
                    await db_manager.mark_trial_notified(user_id)

