import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config.settings import settings
from database.db_manager import db_manager

from services.cache_manager import cache_manager

logger = logging.getLogger(__name__)

class UserRegistrationMiddleware(BaseMiddleware):
    """
    Ensures every user interacting with the bot is automatically registered
    in the database and has an active subscription record.
    Uses in-memory seen_users_cache to avoid redundant DB writes during active interactions.
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

        if from_user and not getattr(from_user, "is_bot", False):
            cache_key = f"seen_{from_user.id}"
            cached_info = await cache_manager.seen_users_cache.get(cache_key)
            current_info = (from_user.full_name, from_user.username)
            if cached_info != current_info:
                is_admin = True if from_user.id in settings.admin_ids else False
                try:
                    await db_manager.get_or_create_user(
                        user_id=from_user.id,
                        full_name=from_user.full_name,
                        username=from_user.username,
                        is_admin=is_admin
                    )
                    # Ensure subscription record exists
                    await db_manager.get_user_subscription(from_user.id)
                    await cache_manager.seen_users_cache.set(cache_key, current_info)
                except Exception as e:
                    logger.error(f"Error in UserRegistrationMiddleware for {from_user.id}: {e}")

        return await handler(event, data)

