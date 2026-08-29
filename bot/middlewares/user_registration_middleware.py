import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from config.settings import settings
from database.db_manager import db_manager

logger = logging.getLogger(__name__)

class UserRegistrationMiddleware(BaseMiddleware):
    """
    Ensures every user interacting with the bot is automatically registered
    in the database and has an active subscription record.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        from_user = None
        if isinstance(event, Message) and event.from_user:
            from_user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            from_user = event.from_user

        if from_user and not from_user.is_bot:
            is_admin = from_user.id in settings.admin_ids
            try:
                await db_manager.get_or_create_user(
                    user_id=from_user.id,
                    full_name=from_user.full_name,
                    username=from_user.username,
                    is_admin=is_admin
                )
                # Ensure subscription record exists
                await db_manager.get_user_subscription(from_user.id)
            except Exception as e:
                logger.error(f"Error in UserRegistrationMiddleware for {from_user.id}: {e}")

        return await handler(event, data)
