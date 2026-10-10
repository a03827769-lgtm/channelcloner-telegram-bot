import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

USER_ERROR_TEXT = "⚠️ Kutilmagan xatolik yuz berdi. Iltimos, birozdan so'ng qayta urinib ko'ring."

# Telegram rejects these edits/answers when the user acts faster than the bot or on an old message;
# nothing is broken, so they are not worth an error log or a message to the user
_BENIGN_BAD_REQUESTS = (
    "message is not modified",
    "query is too old",
    "query id is invalid",
    "message to edit not found",
    "message to delete not found",
    "message can't be deleted",
    "message can't be edited",
)


def is_benign_bad_request(exc: BaseException) -> bool:
    if not isinstance(exc, TelegramBadRequest):
        return False
    text = str(getattr(exc, "message", "") or exc).lower()
    return any(marker in text for marker in _BENIGN_BAD_REQUESTS)


async def handle_update_error(event: ErrorEvent) -> bool:
    """Last-resort handler for exceptions raised while processing an update.

    Without it aiogram logs a full traceback for every user who blocked the bot mid-dialog, and the user
    who hit a real bug gets no answer at all (a callback button keeps spinning)."""
    exc = event.exception
    update = event.update
    update_id = getattr(update, "update_id", None)

    if isinstance(exc, TelegramForbiddenError):
        logger.info(f"Update {update_id}: the user blocked the bot or the chat is not reachable ({exc})")
        return True
    if is_benign_bad_request(exc):
        logger.debug(f"Update {update_id}: ignored Telegram notice: {exc}")
        return True
    if isinstance(exc, TelegramNetworkError):
        # The reply itself could not be sent; another attempt would fail the same way
        logger.warning(f"Update {update_id}: network error while answering: {exc}")
        return True

    logger.error(f"Unhandled error while processing update {update_id}", exc_info=exc)

    callback = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)
    try:
        if callback is not None:
            await callback.answer(USER_ERROR_TEXT, show_alert=True)
        elif message is not None and getattr(getattr(message, "chat", None), "type", None) == "private":
            # Never post the notice into groups or discussion chats the bot moderates
            await message.answer(USER_ERROR_TEXT)
    except Exception:
        logger.debug("Could not tell the user about the error", exc_info=True)
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.errors.register(handle_update_error)
