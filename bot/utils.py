import asyncio
import logging
import html as _html
import re
from typing import Any, Dict, Optional

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InaccessibleMessage, Message, PreCheckoutQuery,
    RefundedPayment, SuccessfulPayment, TelegramObject
)
from services.custom_emojis import clean_for_alert

logger = logging.getLogger(__name__)

# Long user-provided values (signature, replacements, blacklist...) are shown shortened in menus
PREVIEW_MAX_CHARS = 200

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Edit failures no retry can fix: the content is delivered as a new message instead
_EDIT_IMPOSSIBLE_MARKERS = (
    "message can't be edited",
    "message to edit not found",
    "there is no text in the message to edit",
    "message_id_invalid",
)


async def safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    """Safely answer a callback query, silently suppressing Telegram API errors while honoring cancellation."""
    try:
        await callback.answer(text=clean_for_alert(text), show_alert=show_alert)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


def html_escape(value: str) -> str:
    """Escape a string for safe insertion into an HTML-mode Telegram message."""
    return _html.escape(str(value))


def parse_callback_id(data: str, index: int = -1) -> int:
    """
    Safely extracts an integer ID from underscore-separated callback_data.
    Returns 0 if parsing fails, preventing unhandled IndexError or ValueError.
    """
    if not data:
        return 0
    try:
        parts = data.split("_")
        return int(parts[index])
    except (IndexError, ValueError, TypeError):
        return 0


def format_uz_date(iso_str: str) -> str:
    """Formats ISO datetime string (YYYY-MM-DD... or DD.MM.YYYY) into Uzbek standard date: DD-Oy, YYYY-yil"""
    if not iso_str:
        return "N/A"
    months_uz = [
        "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
        "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"
    ]
    try:
        dt_part = iso_str.split("T")[0] if "T" in iso_str else iso_str.split(" ")[0]
        if "." in dt_part and "-" not in dt_part:
            parts = dt_part.split(".")
            if len(parts) == 3:
                day, month, year = parts
                m_idx = int(month) - 1
                if 0 <= m_idx < 12:
                    return f"{int(day)}-{months_uz[m_idx]}, {year}-yil"
        year, month, day = dt_part.split("-")
        m_idx = int(month) - 1
        if 0 <= m_idx < 12:
            return f"{int(day)}-{months_uz[m_idx]}, {year}-yil"
        return dt_part
    except Exception:
        return iso_str[:10] if len(iso_str) >= 10 else iso_str


def strip_html(value: Optional[str]) -> str:
    """Visible text of an HTML-formatted value: tags removed, entities decoded."""
    return _html.unescape(_HTML_TAG_RE.sub("", value or ""))


def preview(value: Optional[str], limit: int = PREVIEW_MAX_CHARS, strip_tags: bool = False) -> str:
    """HTML-escaped, length-limited rendering of a user-provided value for menus and receipts."""
    text = (strip_html(value) if strip_tags else (value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return html_escape(text)


def is_not_modified_error(error: BaseException) -> bool:
    return "message is not modified" in str(error).lower()


def is_too_long_error(error: BaseException) -> bool:
    text = str(error).lower()
    return "too long" in text or "message_too_long" in text


async def show_in_place(message: Any, text: str, **kwargs: Any) -> Any:
    """Shows `text` in place of `message`.

    The message is edited while it is accessible; when it is inaccessible (too old or deleted) or can no
    longer be edited, the content is sent as a new message to the same chat, so a button never dead-ends.
    "Message is not modified" counts as success; other errors (e.g. text too long) propagate."""
    if isinstance(message, InaccessibleMessage):
        return await message.answer(text=text, **kwargs)
    try:
        edited = await message.edit_text(text=text, **kwargs)
        return edited if isinstance(edited, Message) else message
    except TelegramBadRequest as e:
        if is_not_modified_error(e):
            return message
        if any(marker in str(e).lower() for marker in _EDIT_IMPOSSIBLE_MARKERS):
            return await message.answer(text=text, **kwargs)
        raise


async def edit_or_send(callback: CallbackQuery, text: str, **kwargs: Any) -> Any:
    """Shows `text` in place of the menu message the callback came from (see show_in_place)."""
    if callback.message is None:
        return await callback.bot.send_message(chat_id=callback.from_user.id, text=text, **kwargs)
    return await show_in_place(callback.message, text, **kwargs)


def event_chat_type(event: TelegramObject, data: Dict[str, Any]) -> Optional[str]:
    """Type of the chat an update belongs to ("private", "group", ...), or None when unknown."""
    chat = data.get("event_chat")
    if chat is None:
        if isinstance(event, CallbackQuery):
            chat = getattr(getattr(event, "message", None), "chat", None)
        elif isinstance(event, Message):
            chat = getattr(event, "chat", None)
    chat_type = getattr(chat, "type", None)
    return chat_type if isinstance(chat_type, str) else None


def is_non_private_chat_event(event: TelegramObject, data: Dict[str, Any]) -> bool:
    """True for updates from groups, supergroups and channels; never for private or unknown chats."""
    chat_type = event_chat_type(event, data)
    return chat_type is not None and chat_type != ChatType.PRIVATE


def is_payment_event(event: TelegramObject) -> bool:
    """Checkout queries and successful/refunded payment service messages. They must always reach their
    handlers: dropping one would leave a charged user without the plan (or a refunded one with it)."""
    if isinstance(event, PreCheckoutQuery):
        return True
    if isinstance(event, Message):
        return (isinstance(getattr(event, "successful_payment", None), SuccessfulPayment)
                or isinstance(getattr(event, "refunded_payment", None), RefundedPayment))
    return False


async def is_repeated_album_part(message: Message, state: FSMContext) -> bool:
    """True for the 2nd..nth item of an album the current wizard step already handled.

    Forwarding an album delivers one update per item; only the first item is processed, so the rest
    cannot be taken as the answer to the next wizard step."""
    group_id = getattr(message, "media_group_id", None)
    if not isinstance(group_id, str) or not group_id:
        return False
    data = await state.get_data()
    if data.get("last_media_group_id") == group_id:
        return True
    await state.update_data(last_media_group_id=group_id)
    return False
