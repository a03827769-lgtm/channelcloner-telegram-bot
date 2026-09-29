"""Authorization helpers shared by the public bot handlers.

Both checks are answered from memory (environment super admins plus the cached delegated admins kept
up to date by DatabaseManager on startup and on every grant/revoke), so they cost no database query.
"""
from typing import Any

from bot.filters.admin_filter import is_admin_user


def can_manage_pair(pair: Any, user_id: int) -> bool:
    """The owner of a channel pair and bot administrators may view and change it."""
    if not pair or not user_id:
        return False
    return pair.user_id == user_id or is_admin_user(user_id)


def has_admin_side(user_id: int, pair: Any) -> bool:
    """True when the acting user or the pair's owner is an administrator (plan limits do not apply)."""
    return is_admin_user(user_id) or bool(pair and is_admin_user(pair.user_id))


__all__ = ["can_manage_pair", "has_admin_side", "is_admin_user"]
