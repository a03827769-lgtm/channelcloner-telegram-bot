from typing import Union
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from config.settings import settings
from database.db_manager import db_manager

def is_admin_user(user_id: int) -> bool:
    """Helper to check if a user ID is an admin"""
    if not user_id:
        return False
    if user_id == settings.PRIMARY_SUPER_ADMIN_ID or user_id in settings.admin_ids:
        return True
    return bool(db_manager.is_admin_sync(user_id))

class IsAdminFilter(Filter):
    """
    Strict security filter for Aiogram 3 routers.
    Guarantees that only authorized admin IDs can trigger admin routers or handlers.
    """
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        if user.id == settings.PRIMARY_SUPER_ADMIN_ID or user.id in settings.admin_ids:
            return True
        return await db_manager.is_admin(user.id)
