"""
Role checks for the admin tooling.

Two roles exist:

* Super admins — the IDs configured in the environment (PRIMARY_SUPER_ADMIN_ID / ADMIN_IDS).
  They own the deployment and are the only ones allowed to touch the central MTProto account,
  run broadcasts, download database backups, change the bot access mode / support contact and
  grant or revoke paid plans.
* Delegated admins — users promoted from the admin bot (users.is_admin = 1). They can monitor
  the system, browse users and manage the whitelist, but not perform the operations above.
"""
import logging
from typing import Any, Union

from aiogram.types import CallbackQuery, Message

from bot.utils import safe_answer
from config.settings import settings

logger = logging.getLogger(__name__)

SUPER_ADMIN_ONLY_TEXT = "Bu amal faqat Super Admin uchun ruxsat etilgan!"


def is_super_admin(user_id: Any) -> bool:
    """True only for the environment-configured super admins (never for DB-promoted admins)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid > 0 and uid in settings.admin_ids


async def ensure_super_admin(event: Union[CallbackQuery, Message]) -> bool:
    """Returns True when the event author is a super admin; otherwise tells them why the action was refused."""
    user = getattr(event, "from_user", None)
    if user and is_super_admin(user.id):
        return True
    logger.warning(f"Super-admin-only action refused for user_id={getattr(user, 'id', None)}")
    if isinstance(event, CallbackQuery):
        await safe_answer(event, SUPER_ADMIN_ONLY_TEXT, show_alert=True)
    elif isinstance(event, Message):
        try:
            await event.answer(f"⛔️ {SUPER_ADMIN_ONLY_TEXT}")
        except Exception:
            logger.debug("Could not deliver super-admin refusal", exc_info=True)
    return False
