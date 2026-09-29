"""Shared screen rendering for the admin tooling."""
import logging
from typing import Optional, Union

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.utils import edit_or_send

logger = logging.getLogger(__name__)

# Typed instead of a wizard answer, these abort the admin wizard that is waiting for input
CANCEL_WORDS = frozenset({"/cancel", "bekor qilish"})


def is_cancel_text(message: Message) -> bool:
    """True when the admin typed a cancel word instead of the input a wizard step waits for."""
    return (message.text or "").strip().lower() in CANCEL_WORDS


async def show_screen(event: Union[CallbackQuery, Message], text: str,
                      reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Shows an admin screen: in place of the menu message after a button tap, as a reply to a command.

    A menu message that can no longer be edited (too old, deleted, a media message) gets the screen as a
    new message; "message is not modified" is not an error. Any other Bot API refusal is logged, so a
    failed redraw never aborts the action the admin has already performed."""
    try:
        if isinstance(event, CallbackQuery):
            await edit_or_send(event, text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await event.answer(text=text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.warning(f"Admin screen could not be shown: {e}")
