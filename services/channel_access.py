"""
Authorization checks for destination chats.

A channel pair (or a disaster-recovery restore) publishes content into its target chat through the bot.
The bot being an administrator of that chat is not enough: the bot is an admin in *every* customer's
target channel, so without verifying the requesting user as well, any user could clone content into
another customer's channel. Both the bot UI and the Mini App API use these helpers.
"""
import logging
from typing import Any, Optional, Tuple

from aiogram import Bot

logger = logging.getLogger(__name__)

POSTABLE_CHAT_TYPES = ("channel", "supergroup")


def _status(member: Any) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status) or "").lower()


def _member_can_post(member: Any, chat_type: str) -> bool:
    """True for the chat creator, or an administrator allowed to publish in this chat type."""
    status = _status(member)
    if status == "creator":
        return True
    if status != "administrator":
        return False
    if chat_type == "channel":
        return getattr(member, "can_post_messages", False) is True
    # Supergroups: any administrator may post; managing the chat is the meaningful ownership signal
    return True


async def verify_destination_access(bot: Bot, chat_ref: Any, user_id: int) -> Tuple[bool, Optional[Any], str]:
    """Checks that `chat_ref` is a channel/supergroup where BOTH the bot and `user_id` may publish.

    Returns (ok, chat, error_code). error_code is one of:
      "not_found"      — the bot cannot see the chat (not a member, wrong id/username)
      "wrong_type"     — private chat or basic group
      "bot_not_admin"  — the bot is not an administrator allowed to post
      "user_not_admin" — the requesting user is not the creator/an administrator allowed to post
    """
    try:
        chat = await bot.get_chat(chat_ref)
    except Exception as e:
        logger.info(f"Destination chat {chat_ref!r} not accessible for the bot: {e}")
        return False, None, "not_found"

    chat_type = str(getattr(getattr(chat, "type", ""), "value", getattr(chat, "type", ""))).lower()
    if chat_type not in POSTABLE_CHAT_TYPES:
        return False, chat, "wrong_type"

    try:
        bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
    except Exception as e:
        logger.info(f"Could not read bot membership in {chat.id}: {e}")
        return False, chat, "bot_not_admin"
    if not _member_can_post(bot_member, chat_type):
        return False, chat, "bot_not_admin"

    try:
        user_member = await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
    except Exception as e:
        logger.info(f"Could not read membership of user {user_id} in {chat.id}: {e}")
        return False, chat, "user_not_admin"
    if not _member_can_post(user_member, chat_type):
        return False, chat, "user_not_admin"

    return True, chat, ""


DESTINATION_ERROR_TEXTS = {
    "not_found": "Bot maqsadli kanalni topa olmadi. Botni kanalga administrator qilib qo'shing va qaytadan urinib ko'ring.",
    "wrong_type": "Maqsad faqat kanal yoki superguruh bo'lishi mumkin (shaxsiy chat yoki oddiy guruh emas).",
    "bot_not_admin": "Bot ushbu kanalda administrator emas yoki unga xabar joylash (Post Messages) huquqi berilmagan.",
    "user_not_admin": "Siz ushbu kanalning egasi yoki xabar joylash huquqiga ega administratori emassiz. Faqat o'zingiz boshqaradigan kanalni ulashingiz mumkin.",
}
