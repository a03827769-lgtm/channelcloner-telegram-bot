from typing import Union
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from config.settings import settings

def is_admin_user(user_id: int) -> bool:
    """Helper to check if a user ID is an admin"""
    return bool(user_id and user_id in settings.admin_ids)

class IsAdminFilter(Filter):
    """
    Strict security filter for Aiogram 3 routers.
    Guarantees that only authorized admin IDs can trigger admin routers or handlers.
    """
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        return is_admin_user(user.id)
