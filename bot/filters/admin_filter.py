from typing import Union
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from database.db_manager import db_manager

def is_admin_user(user_id: int) -> bool:
    """Admin check for filters: environment super admins and delegated (database-granted) admins, answered
    from memory without a database query."""
    if not user_id:
        return False
    return bool(db_manager.is_admin_sync(user_id))

class IsAdminFilter(Filter):
    """
    Strict security filter for Aiogram 3 routers.
    Guarantees that only authorized admin IDs can trigger admin routers or handlers.
    Uses the in-memory admin cache (filled at startup and on every grant/revoke), so the filter adds no
    database query to updates from regular users.
    """
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        return is_admin_user(user.id)
