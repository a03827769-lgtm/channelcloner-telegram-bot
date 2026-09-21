import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config.settings import settings

logger = logging.getLogger(__name__)

from services.custom_emojis import ERROR

from database.db_manager import db_manager

class AdminStrictAuthMiddleware(BaseMiddleware):
    """
    Strict security middleware for Admin Bot:
    Blocks and drops any interaction from non-admin users immediately.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return None

        is_authorized = (user.id == settings.PRIMARY_SUPER_ADMIN_ID or user.id in settings.admin_ids) or await db_manager.is_admin(user.id)
        if not is_authorized:
            logger.warning(f"UNAUTHORIZED access attempt on Admin Bot by user_id={user.id} ({user.full_name}, @{user.username})")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Ruxsat berilmagan! Ushbu bot faqat Super Adminlar uchun!", show_alert=True)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            # Silently drop unauthorized messages to protect dedicated admin bot from FloodWait attacks
            return None

        return await handler(event, data)
