import re
import html
import logging
from typing import Optional, Tuple
from aiogram import Router, F
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router(name="comment_moderator_router")

# Blacklisted spam patterns for comments/discussion groups
SPAM_PATTERNS = [
    r'\b(?:1win|mostbet|aviator|melbet|linebet|pinup|1xbet|kazino|casino)\b',
    r'\b(?:kripto|crypto\s*signal|mining|tekin\s*pul|ishlab\s*topish|oson\s*pul)\b',
    r't\.me/\+[a-zA-Z0-9_-]+', # Private invite links
    r'\b(?:kunlik\s*daromad|kuniga\s*\d+\s*ming|sarmoyasiz)\b'
]

PRICE_INQUIRY_PATTERNS = [
    r'\b(?:narxi\s*qancha|necha\s*pul|nech\s*pul|nechpul|qanchadan|narxini\s*ayting|skolko\s*stoit|цена|почем)\b'
]


class CommentModerator:
    @staticmethod
    def is_spam(text: str) -> Tuple[bool, str]:
        if not text:
            return False, ""
        t_lower = text.lower()
        for pat in SPAM_PATTERNS:
            if re.search(pat, t_lower):
                return True, f"Spam/Reklama ({pat})"
        return False, ""

    @staticmethod
    def is_price_inquiry(text: str) -> bool:
        if not text:
            return False
        t_lower = text.lower()
        return any(re.search(pat, t_lower) for pat in PRICE_INQUIRY_PATTERNS)


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def handle_discussion_group_message(message: Message):
    """
    Auto-moderates real estate channel discussion group comments:
    1. Instantly deletes spam, casino links, and illicit referral promos.
    2. Auto-answers frequent buyer inquiries (e.g. price inquiries in comments).
    """
    if not message.text and not message.caption:
        return

    text = message.text or message.caption or ""

    # 1. Anti-Spam Check
    is_spam, reason = CommentModerator.is_spam(text)
    user_id = message.from_user.id if message.from_user else (message.sender_chat.id if message.sender_chat else 0)
    if is_spam:
        # Exempt super admins
        from config.settings import settings
        if user_id in settings.admin_ids:
            is_spam = False
        elif message.from_user and message.chat:
            try:
                member = await message.chat.get_member(message.from_user.id)
                if member.status in ("administrator", "creator"):
                    is_spam = False
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

    if is_spam:
        try:
            await message.delete()
            logger.info(f"Deleted spam comment in group {message.chat.id} from user {user_id}: {reason}")
            return
        except Exception as del_err:
            logger.debug(f"Could not delete spam comment (insufficient bot admin permissions): {del_err}")

    # 2. Smart Auto-Responder to Price Inquiries
    # If a user asks "Narxi qancha?" on a post thread or in reply to a channel forwarded message:
    if CommentModerator.is_price_inquiry(text):
        reply_to = message.reply_to_message
        if reply_to:
            orig_text = reply_to.text or reply_to.caption or ""
            # Extract price from original post
            p_match = re.search(r'(\d+[\s.,]?\d*)\s*(?:\$|dollar|usd|y\.e)', orig_text, re.IGNORECASE)
            if p_match:
                detected_p = p_match.group(0).strip()
                if message.from_user:
                    user_mention = f"@{message.from_user.username}" if message.from_user.username else html.escape(message.from_user.first_name or "Foydalanuvchi")
                elif message.sender_chat:
                    user_mention = f"@{message.sender_chat.username}" if message.sender_chat.username else html.escape(message.sender_chat.title or "Kanal")
                else:
                    user_mention = "Foydalanuvchi"
                safe_price = html.escape(detected_p)
                reply_msg = (
                    f"Assalomu alaykum, {user_mention}!\n"
                    f"📌 Ushbu e'lon bo'yicha ko'rsatilgan narx: <b>{safe_price}</b>\n"
                    f"Batafsil ma'lumot va fotosuratlar yuqoridagi asosiy postda keltirilgan."
                )
                try:
                    await message.reply(reply_msg, parse_mode="HTML")
                except Exception as rep_err:
                    logger.debug(f"Failed to auto-reply to comment: {rep_err}")
