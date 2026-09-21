import asyncio
import logging
import html as _html
from aiogram.types import CallbackQuery
from services.custom_emojis import clean_for_alert

logger = logging.getLogger(__name__)


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
