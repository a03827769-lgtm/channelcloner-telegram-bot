import asyncio
import html
import logging
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import ChatMemberUpdated

from config.settings import settings
from database.db_manager import db_manager
from database.models import ChannelPair

logger = logging.getLogger(__name__)

router = Router(name="channel_membership_router")

_ADMIN_STATUSES = {"administrator", "creator"}
SEND_TIMEOUT = 15

PROMOTED_TEXT = (
    "✅ Bot <b>{title}</b> kanalida administrator qilindi.\n\n"
    "Endi kanalni ulang: <b>Kanal Kloner</b> → <b>Yangi kanal ulash</b>. "
    "Faqat botni admin qilishning o'zi yetarli emas — juftlik bot ichida yaratilishi kerak."
)
NO_POST_RIGHT_TEXT = (
    "\n\n⚠️ Botda <b>xabar joylash</b> huquqi yo'q. Kanal sozlamalarida botga "
    "«Post messages» huquqini bering, aks holda postlar tashlanmaydi."
)
UNKNOWN_OWNER_ALERT = (
    "⚠️ <b>Botni kanalga admin qilishdi, lekin egasi botga /start bosmagan</b>\n\n"
    "Kanal: {channel}\n"
    "Foydalanuvchi: {user}\n\n"
    "Bot unga yoza olmaydi. Mijoz bilan bog'lanib, botga /start bosib kanalni bot ichida ulashini ayting."
)
ACCESS_LOST_TEXT = (
    "⛔️ Bot <b>{title}</b> kanalida endi administrator emas.\n\n"
    "Shu kanalga ulangan juftlik(lar) #{pairs} postlarni tashlay olmaydi. "
    "Botni kanalga qayta administrator qilib, «Post messages» huquqini bering."
)


def _is_admin(status: str) -> bool:
    return str(status) in _ADMIN_STATUSES


def _describe_channel(chat) -> str:
    title = html.escape(chat.title or "")
    username = f" (@{html.escape(chat.username)})" if getattr(chat, "username", None) else ""
    return f"<b>{title}</b>{username} — <code>{chat.id}</code>"


def _describe_user(user) -> str:
    name = html.escape(user.full_name or "")
    username = f" (@{html.escape(user.username)})" if user.username else ""
    return f"{name}{username} — <code>{user.id}</code>"


def _pairs_targeting(pairs: List[ChannelPair], chat) -> List[ChannelPair]:
    keys = {str(chat.id)}
    if getattr(chat, "username", None):
        keys.add(f"@{chat.username}".lower())
    return [
        p for p in pairs
        if p.target_id == chat.id or str(p.target_channel or "").strip().lower() in keys
    ]


async def _send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await asyncio.wait_for(bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML"), timeout=SEND_TIMEOUT)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.info(f"Could not message {chat_id}: {e!r}")
        return False


async def _alert_admins(bot: Bot, text: str) -> None:
    admin_ids = sorted(settings.admin_ids)
    delivered = [admin_id for admin_id in admin_ids if await _send(bot, admin_id, text)]
    if not delivered:
        logger.error(f"Channel membership alert reached no admin: {text}")


@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_bot_channel_status(event: ChatMemberUpdated, bot: Bot) -> None:
    """Owners often only make the bot a channel admin and wait. Without this handler the bot ignored that
    silently, so neither the owner nor the support team learned that the pair was never created."""
    chat = event.chat
    user = event.from_user
    was_admin = _is_admin(event.old_chat_member.status)
    is_admin = _is_admin(event.new_chat_member.status)

    if is_admin and not was_admin:
        can_post: Optional[bool] = getattr(event.new_chat_member, "can_post_messages", None)
        logger.info(
            f"Bot was made admin of channel {chat.id} ({chat.title!r}) by user {getattr(user, 'id', None)}"
            f" (can_post_messages={can_post})"
        )
        if user is None or user.is_bot:
            return
        text = PROMOTED_TEXT.format(title=html.escape(chat.title or str(chat.id)))
        if can_post is False:
            text += NO_POST_RIGHT_TEXT
        if not await _send(bot, user.id, text):
            # Telegram lets a bot write only to users who started it
            logger.warning(f"User {user.id} made the bot admin of channel {chat.id} but never started the bot")
            await _alert_admins(bot, UNKNOWN_OWNER_ALERT.format(
                channel=_describe_channel(chat), user=_describe_user(user)
            ))
        return

    if was_admin and not is_admin:
        try:
            affected = _pairs_targeting(await db_manager.get_all_active_pairs(), chat)
        except Exception as e:
            logger.error(f"Could not look up pairs for channel {chat.id} after the bot lost admin rights: {e}")
            return
        logger.warning(
            f"Bot is no longer admin of channel {chat.id} ({chat.title!r}); "
            f"status={event.new_chat_member.status}, affected pairs={[p.id for p in affected]}"
        )
        owners = {}
        for pair in affected:
            owners.setdefault(pair.user_id, []).append(str(pair.id))
        title = html.escape(chat.title or str(chat.id))
        for owner_id, pair_ids in owners.items():
            await _send(bot, owner_id, ACCESS_LOST_TEXT.format(title=title, pairs=", #".join(pair_ids)))
