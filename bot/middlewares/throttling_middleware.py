import time
import logging
from typing import Callable, Dict, Any, Awaitable, List
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

logger = logging.getLogger(__name__)

class ThrottlingMiddleware(BaseMiddleware):
    """
    In-memory sliding-window anti-flood and rate-limiting middleware.
    Prevents Telegram API FloodWait and database locking by limiting
    update frequency per user.
    """
    def __init__(self, rate_limit: float = 0.5, burst_limit: int = 5, window_seconds: float = 2.0):
        self.rate_limit = rate_limit
        self.burst_limit = burst_limit
        self.window_seconds = window_seconds
        self._user_timestamps: Dict[int, List[float]] = {}
        self._last_msg_notice: Dict[int, float] = {}
        self._last_prune = time.monotonic()

    def _is_throttled(self, user_id: int) -> bool:
        now = time.monotonic()
        # Periodic pruning every 60 seconds
        if now - self._last_prune > 60.0:
            self._prune_stale(now)

        timestamps = self._user_timestamps.setdefault(user_id, [])
        # Remove timestamps outside the sliding window
        window_start = now - self.window_seconds
        self._user_timestamps[user_id] = [t for t in timestamps if t > window_start]
        timestamps = self._user_timestamps[user_id]

        if len(timestamps) >= self.burst_limit:
            return True

        timestamps.append(now)
        return False

    def _prune_stale(self, now: float):
        self._last_prune = now
        cutoff = now - 300.0  # keep up to 5 minutes of data, not just window*2
        stale_keys = [uid for uid, ts in self._user_timestamps.items() if not ts or max(ts) < cutoff]
        for uid in stale_keys:
            self._user_timestamps.pop(uid, None)
        # Prune msg notice dict as well
        stale_notices = [uid for uid, t in self._last_msg_notice.items() if t < cutoff]
        for uid in stale_notices:
            self._last_msg_notice.pop(uid, None)
        # Hard cap: if dict still too large, evict oldest entries to prevent unbounded growth
        if len(self._user_timestamps) > 10000:
            sorted_by_last = sorted(self._user_timestamps.items(), key=lambda x: max(x[1]) if x[1] else 0)
            for uid, _ in sorted_by_last[:len(self._user_timestamps) - 8000]:
                self._user_timestamps.pop(uid, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        from_user = data.get("event_from_user")
        if not from_user and hasattr(event, "from_user"):
            from_user = getattr(event, "from_user")

        if from_user and not getattr(from_user, "is_bot", False):
            user_id = from_user.id
            from config.settings import settings
            if user_id not in settings.admin_ids and self._is_throttled(user_id):
                logger.warning(f"Throttled rapid request from user {user_id}")
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⚠️ Juda tez! Iltimos, biroz kuting...", show_alert=False)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                elif isinstance(event, Message):
                    now = time.monotonic()
                    last_notice = self._last_msg_notice.get(user_id, 0)
                    if now - last_notice > 5.0:
                        self._last_msg_notice[user_id] = now
                        try:
                            await event.answer("⚠️ Juda tez so'rov yubordingiz! Iltimos, biroz kuting...")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                return None

        return await handler(event, data)
