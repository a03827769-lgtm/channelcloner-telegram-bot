import time
import logging
from typing import Callable, Dict, Any, Awaitable, List, Tuple
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from bot.filters.admin_filter import is_admin_user
from bot.utils import is_non_private_chat_event, is_payment_event

logger = logging.getLogger(__name__)

class ThrottlingMiddleware(BaseMiddleware):
    """
    In-memory sliding-window anti-flood and rate-limiting middleware.
    Prevents Telegram API FloodWait and database locking by limiting
    update frequency per user.

    Never throttled: payment events (checkout queries, successful/refunded payment service messages —
    dropping one would leave a charged user without the purchased plan), group/channel traffic, which
    only reaches the comment moderator and must not trigger warnings inside groups, and administrators.
    A forwarded album arrives as up to ten messages at once; it counts as a single update.
    """
    ALBUM_TRACKING_SECONDS = 30.0

    def __init__(self, rate_limit: float = 0.5, burst_limit: int = 5, window_seconds: float = 2.0):
        self.rate_limit = rate_limit
        self.burst_limit = burst_limit
        self.window_seconds = window_seconds
        self._user_timestamps: Dict[int, List[float]] = {}
        self._last_msg_notice: Dict[int, float] = {}
        # user id -> (media group id of the last album seen, when it was first seen)
        self._last_album: Dict[int, Tuple[str, float]] = {}
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

    def _is_later_album_part(self, user_id: int, event: TelegramObject) -> bool:
        """True for the 2nd..nth message of an album whose first message was already counted."""
        group_id = getattr(event, "media_group_id", None) if isinstance(event, Message) else None
        if not isinstance(group_id, str) or not group_id:
            return False
        now = time.monotonic()
        last = self._last_album.get(user_id)
        if last and last[0] == group_id and now - last[1] < self.ALBUM_TRACKING_SECONDS:
            return True
        self._last_album[user_id] = (group_id, now)
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
        album_cutoff = now - self.ALBUM_TRACKING_SECONDS
        for uid in [uid for uid, (_, seen) in self._last_album.items() if seen < album_cutoff]:
            self._last_album.pop(uid, None)
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
        if is_payment_event(event) or is_non_private_chat_event(event, data):
            return await handler(event, data)

        from_user = data.get("event_from_user")
        if not from_user and hasattr(event, "from_user"):
            from_user = getattr(event, "from_user")

        if from_user and not getattr(from_user, "is_bot", False):
            user_id = from_user.id
            if is_admin_user(user_id) or self._is_later_album_part(user_id, event):
                return await handler(event, data)
            if self._is_throttled(user_id):
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
