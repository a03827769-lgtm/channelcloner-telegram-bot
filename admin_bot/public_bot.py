"""
Access to the *public* bot from the admin tooling.

Users only ever talk to the public bot, so notifications and broadcasts must be delivered with the
public token even when the command came from the dedicated admin bot. The process already owns a
public Bot instance (run.py hands it to the cloner engine); it is reused when available so no extra
HTTP session is opened. A private instance is created only as a fallback (e.g. in scripts/tests).
"""
import inspect
import logging
from typing import Optional

from aiogram import Bot

from config.settings import settings

logger = logging.getLogger(__name__)

_fallback_public_bot: Optional[Bot] = None


def get_public_bot(current_bot: Optional[Bot] = None) -> Optional[Bot]:
    """Returns a Bot bound to BOT_TOKEN, or None when the public token is not configured."""
    global _fallback_public_bot
    if not settings.BOT_TOKEN:
        return None
    if current_bot is not None and getattr(current_bot, "token", None) == settings.BOT_TOKEN:
        return current_bot
    try:
        from services.cloner_engine import cloner_engine
        engine_bot = cloner_engine.bot
        if engine_bot is not None and getattr(engine_bot, "token", None) == settings.BOT_TOKEN:
            return engine_bot
    except Exception:
        logger.debug("Cloner engine bot unavailable", exc_info=True)
    if _fallback_public_bot is None:
        from bot.bot_instance import create_bot
        _fallback_public_bot = create_bot()
    return _fallback_public_bot


async def close_fallback_public_bot() -> None:
    """Closes the fallback instance (never the shared one owned by run.py)."""
    global _fallback_public_bot
    bot = _fallback_public_bot
    _fallback_public_bot = None
    if bot is not None and getattr(bot, "session", None):
        try:
            close_res = bot.session.close()
            if inspect.isawaitable(close_res):
                await close_res
        except Exception:
            logger.debug("Fallback public bot session close failed", exc_info=True)
