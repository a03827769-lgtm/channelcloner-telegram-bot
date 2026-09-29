import asyncio
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from database.db_manager import db_manager
from aiogram import Bot
from services.channel_access import verify_destination_access
from services.text_processor import TextProcessor
from aiogram.types import InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], Awaitable[Any]]


class RestoreAborted(Exception):
    """The destination stopped accepting posts (bot removed, chat deleted): the restore cannot go on."""


class DisasterRecoveryService:
    """
    Channel Disaster Recovery & 1-Click Instant Mirror Service.
    Maintains an encrypted archive of every cloned message and provides instant rebuilding of channels.

    A restore publishes the archive of one pair into a destination chat. Only one restore may run per
    pair and per destination at a time, the requesting user must be allowed to post in the destination
    (checked again here, not only in the bot UI), the archive is streamed page by page instead of being
    loaded as a whole, and posts are paced to Telegram's per-chat limit (about 20 messages a minute).
    """
    # Minimum pause between two sends into the destination chat (seconds per message)
    SEND_INTERVAL_SECONDS = 3.0
    # Archive rows fetched per database round trip
    PAGE_SIZE = 100
    # Longest single FloodWait slept through while retrying one send
    MAX_RETRY_AFTER_SECONDS = 600

    def __init__(self):
        self._active_pairs: Set[int] = set()
        self._active_targets: Set[Any] = set()

    def is_restore_running(self, pair_id: int) -> bool:
        return pair_id in self._active_pairs

    async def archive_message(
        self,
        pair_id: int,
        source_id: Optional[int],
        message_id: int,
        text: str = "",
        media_type: str = "none",
        media_file_id: Optional[str] = None,
        entities_json: str = "",
        media_group_id: Optional[str] = None,
        db = None
    ):
        """Saves a post into the persistent disaster recovery archive"""
        db_instance = db or db_manager
        return await db_instance.save_channel_backup(
            pair_id=pair_id,
            source_id=source_id,
            message_id=message_id,
            text=text,
            media_type=media_type,
            media_file_id=media_file_id,
            entities_json=entities_json,
            media_group_id=media_group_id
        )

    async def _safe_send(self, send_coro_func, *args, **kwargs):
        """Executes a Telegram send call with automatic FloodWait backoff and HTML entity parse fallback"""
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                return await send_coro_func(*args, **kwargs)
            except TelegramRetryAfter as e_retry:
                wait_secs = min(int(getattr(e_retry, "retry_after", 5) or 5) + 1, self.MAX_RETRY_AFTER_SECONDS)
                logger.warning(f"Telegram FloodWait during restore (attempt {attempt + 1}/{max_attempts}): sleeping {wait_secs}s...")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(wait_secs)
                else:
                    raise
            except TelegramBadRequest as e_bad:
                if "can't parse entities" in str(e_bad).lower() and kwargs.get("parse_mode"):
                    kwargs["parse_mode"] = None
                    continue
                raise

    @staticmethod
    def _result(total: int = 0, restored: int = 0, failed: int = 0, error: Optional[str] = None) -> Dict[str, Any]:
        res: Dict[str, Any] = {"total_archived": total, "restored": restored, "failed": failed}
        if error:
            res["error"] = error
        return res

    @staticmethod
    def _normalize_target(new_target_channel: Any) -> Any:
        tgt_str = str(new_target_channel).strip()
        if tgt_str.startswith("-100"):
            return int(tgt_str)
        if tgt_str.lstrip("-").isdigit():
            clean_digits = tgt_str.lstrip("-")
            if len(clean_digits) >= 7 and not tgt_str.startswith("-"):
                return int(f"-100{clean_digits}")
            return int(tgt_str)
        return f"@{tgt_str.lstrip('@')}" if not tgt_str.startswith("@") and not tgt_str.startswith("-") else tgt_str

    async def _iter_backups(self, db_instance, pair_id: int, limit: Optional[int]) -> AsyncIterator[Dict[str, Any]]:
        """Archive rows in message order, read PAGE_SIZE rows at a time (keyset pagination on message_id)."""
        last_id: Optional[int] = None
        remaining = limit
        while remaining is None or remaining > 0:
            size = self.PAGE_SIZE if remaining is None else min(self.PAGE_SIZE, remaining)
            rows = await db_instance.get_channel_backups(pair_id, limit=size, after_message_id=last_id)
            for row in rows:
                yield row
            if len(rows) < size:
                return
            last_id = rows[-1]["message_id"]
            if remaining is not None:
                remaining -= len(rows)

    async def _iter_groups(self, db_instance, pair_id: int, limit: Optional[int]) -> AsyncIterator[Tuple[Optional[str], List[Dict[str, Any]]]]:
        """Archive rows with consecutive items of one album yielded together as (media_group_id, [rows])."""
        current_group_id = None
        current_group: List[Dict[str, Any]] = []
        async for item in self._iter_backups(db_instance, pair_id, limit):
            mg_id = item.get("media_group_id")
            if mg_id and item.get("media_type") in ("photo", "video", "media_group"):
                if current_group_id == mg_id:
                    current_group.append(item)
                    continue
                if current_group:
                    yield current_group_id, current_group
                current_group_id, current_group = mg_id, [item]
            else:
                if current_group:
                    yield current_group_id, current_group
                    current_group_id, current_group = None, []
                yield None, [item]
        if current_group:
            yield current_group_id, current_group

    async def restore_channel(
        self,
        bot: Bot,
        pair_id: int,
        new_target_channel: str,
        limit: Optional[int] = None,
        db = None,
        requester_id: Optional[int] = None,
        progress_callback: Optional[ProgressCallback] = None
    ) -> Dict[str, Any]:
        """
        Restores the archived posts of `pair_id` into `new_target_channel` on behalf of `requester_id`.

        Returns {"total_archived", "restored", "failed"} plus "error" when the restore was refused or
        aborted. Refusal codes: requester_required, pair_not_found, forbidden, private_target,
        already_running, target_busy and the destination codes of verify_destination_access
        (not_found, wrong_type, bot_not_admin, user_not_admin); "aborted" when the destination stopped
        accepting posts midway.
        """
        db_instance = db or db_manager

        if not requester_id:
            logger.error(f"Refusing restore of pair #{pair_id}: no requesting user given")
            return self._result(error="requester_required")

        pair = await db_instance.get_pair_by_id(pair_id)
        if not pair:
            return self._result(error="pair_not_found")
        if pair.user_id != requester_id and not await db_instance.is_admin(requester_id):
            logger.warning(f"SECURITY: user {requester_id} tried to restore pair #{pair_id} owned by {pair.user_id}")
            return self._result(error="forbidden")

        tgt_chat = self._normalize_target(new_target_channel)
        # SECURITY: Refuse restoring full channel post archives to private users
        if isinstance(tgt_chat, int) and tgt_chat > 0:
            logger.error(f"SECURITY ALERT: Refusing to restore channel archive to private user chat {tgt_chat}")
            return self._result(error="private_target")

        if pair_id in self._active_pairs:
            return self._result(error="already_running")

        # Re-verified here, not only in the bot UI: the requester must be allowed to post in the destination
        ok, chat, code = await verify_destination_access(bot, tgt_chat, requester_id)
        if not ok:
            logger.warning(f"Restore of pair #{pair_id} to {tgt_chat!r} refused for user {requester_id}: {code}")
            return self._result(error=code)
        tgt_chat = chat.id

        # Checked again after the awaits above, so two simultaneous requests cannot both start
        if pair_id in self._active_pairs:
            return self._result(error="already_running")
        if tgt_chat in self._active_targets:
            return self._result(error="target_busy")
        self._active_pairs.add(pair_id)
        self._active_targets.add(tgt_chat)
        try:
            return await self._run_restore(bot, db_instance, pair_id, tgt_chat, limit, progress_callback)
        finally:
            self._active_pairs.discard(pair_id)
            self._active_targets.discard(tgt_chat)

    async def _run_restore(self, bot: Bot, db_instance, pair_id: int, tgt_chat: Any, limit: Optional[int],
                           progress_callback: Optional[ProgressCallback]) -> Dict[str, Any]:
        total = await db_instance.get_channel_backup_count(pair_id)
        if limit is not None:
            total = min(total, max(0, int(limit)))
        restored = 0
        failed = 0
        next_send_at = 0.0

        logger.info(f"Starting disaster restore for pair #{pair_id} to {tgt_chat} ({total} posts)...")

        async def send(send_func, weight: int = 1, **kwargs):
            """One send into the destination chat, paced to SEND_INTERVAL_SECONDS per message."""
            nonlocal next_send_at
            wait = next_send_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._safe_send(send_func, chat_id=tgt_chat, **kwargs)
            except TelegramForbiddenError as e:
                raise RestoreAborted(str(e)) from e
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    raise RestoreAborted(str(e)) from e
                raise
            finally:
                next_send_at = time.monotonic() + self.SEND_INTERVAL_SECONDS * max(1, weight)

        async def report(status: str):
            if progress_callback:
                try:
                    await progress_callback(restored + failed, total, status)
                except Exception:
                    logger.debug("Restore progress callback failed", exc_info=True)

        async def restore_album(items: List[Dict[str, Any]]) -> bool:
            """Sends an album as one media group; False when it has to be restored item by item."""
            media_list = []
            for idx, itm in enumerate(items):
                raw_t = itm.get("text") or ""
                cap, _ = TextProcessor.fit_caption_limit(raw_t, max_limit=1020) if idx == 0 else (None, None)
                m_type = itm.get("media_type")
                fid = itm.get("media_file_id")
                if fid and not fid.isdigit():
                    if m_type in ("video", "video_note"):
                        media_list.append(InputMediaVideo(media=fid, caption=cap, parse_mode="HTML"))
                    else:
                        media_list.append(InputMediaPhoto(media=fid, caption=cap, parse_mode="HTML"))
            if len(media_list) < 2:
                return False
            try:
                await send(bot.send_media_group, weight=len(media_list), media=media_list)
                return True
            except RestoreAborted:
                raise
            except Exception as e_album:
                logger.warning(f"Media group restore failed ({e_album}), falling back to single items...")
                return False

        async def restore_item(item: Dict[str, Any]):
            raw_text = item.get("text") or ""
            media_type = item.get("media_type")
            media_file_id = item.get("media_file_id")

            valid_media = bool(media_file_id and not media_file_id.isdigit())
            caption, overflow = TextProcessor.fit_caption_limit(raw_text, max_limit=1020)

            if media_type == "photo" and valid_media:
                await send(bot.send_photo, photo=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type in ("video", "video_note") and valid_media:
                await send(bot.send_video, video=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type == "media_group" and valid_media:
                try:
                    await send(bot.send_photo, photo=media_file_id, caption=caption or None, parse_mode="HTML")
                except RestoreAborted:
                    raise
                except Exception as e_photo:
                    if "wrong type" not in str(e_photo).lower() and "file_id" not in str(e_photo).lower():
                        raise
                    try:
                        await send(bot.send_video, video=media_file_id, caption=caption or None, parse_mode="HTML")
                    except RestoreAborted:
                        raise
                    except Exception:
                        await send(bot.send_document, document=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type == "document" and valid_media:
                await send(bot.send_document, document=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type == "audio" and valid_media:
                await send(bot.send_audio, audio=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type == "voice" and valid_media:
                await send(bot.send_voice, voice=media_file_id, caption=caption or None, parse_mode="HTML")
            elif media_type == "animation" and valid_media:
                await send(bot.send_animation, animation=media_file_id, caption=caption or None, parse_mode="HTML")
            elif raw_text:
                for chunk in TextProcessor.fit_text_limit(raw_text, max_limit=4096):
                    await send(bot.send_message, text=chunk, parse_mode="HTML")

            if overflow and valid_media and TextProcessor.get_visible_text_length(overflow) > 0:
                try:
                    await send(bot.send_message, text=overflow, parse_mode="HTML")
                except RestoreAborted:
                    raise
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

        try:
            async for mg_id, items in self._iter_groups(db_instance, pair_id, limit):
                if mg_id and len(items) > 1 and await restore_album(items):
                    restored += len(items)
                else:
                    for item in items:
                        try:
                            await restore_item(item)
                            restored += 1
                        except RestoreAborted:
                            raise
                        except Exception as e:
                            logger.error(f"Failed to restore archived post #{item.get('message_id')}: {e}")
                            failed += 1
                await report("running")
        except RestoreAborted as e:
            logger.error(f"Restore of pair #{pair_id} into {tgt_chat} aborted: {e}")
            failed = max(failed, total - restored)
            await report("aborted")
            return self._result(total, restored, failed, "aborted")

        await report("completed")
        return self._result(total, restored, failed)

disaster_recovery_service = DisasterRecoveryService()
