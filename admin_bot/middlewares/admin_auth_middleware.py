import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery
from config.settings import settings
from database.db_manager import db_manager

logger = logging.getLogger(__name__)


class AdminStrictAuthMiddleware(BaseMiddleware):
    """
    Strict security middleware for the dedicated admin bot:
    drops every interaction from users who are neither super admins (environment) nor
    delegated admins (promoted from the admin bot). Super-admin-only actions are checked
    additionally inside the individual handlers.
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

        is_authorized = user.id in settings.admin_ids or await db_manager.is_admin(user.id)
        if not is_authorized:
            logger.warning(f"UNAUTHORIZED access attempt on Admin Bot by user_id={user.id}")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Ruxsat berilmagan! Ushbu bot faqat administratorlar uchun.", show_alert=True)
                except Exception:
                    logger.debug("Could not answer unauthorized callback", exc_info=True)
            # Messages from strangers are dropped silently so the admin bot cannot be used to trigger FloodWait
            return None

        return await handler(event, data)
