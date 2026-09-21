import asyncio
import json
import logging
from typing import Dict, Any, List, Optional
from database.db_manager import db_manager
from aiogram import Bot
from services.text_processor import TextProcessor
from aiogram.types import InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

logger = logging.getLogger(__name__)

class DisasterRecoveryService:
    """
    Channel Disaster Recovery & 1-Click Instant Mirror Service.
    Maintains an encrypted archive of every cloned message and provides instant rebuilding of channels.
    """

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
                wait_secs = getattr(e_retry, "retry_after", 5) + 1
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
            except Exception:
                raise

    async def restore_channel(
        self,
        bot: Bot,
        pair_id: int,
        new_target_channel: str,
        limit: Optional[int] = None,
        db = None
    ) -> Dict[str, Any]:
        """
        Instantly clones all archived messages into a new destination channel with rate limit resilience.
        """
        db_instance = db or db_manager
        backups = await db_instance.get_channel_backups(pair_id, limit=limit)
        restored = 0
        failed = 0

        tgt_str = str(new_target_channel).strip()
        if tgt_str.startswith("-100"):
            tgt_chat = int(tgt_str)
        elif tgt_str.lstrip("-").isdigit():
            clean_digits = tgt_str.lstrip("-")
            if len(clean_digits) >= 7 and not tgt_str.startswith("-"):
                tgt_chat = int(f"-100{clean_digits}")
            else:
                tgt_chat = int(tgt_str)
        else:
            tgt_chat = f"@{tgt_str.lstrip('@')}" if not tgt_str.startswith("@") and not tgt_str.startswith("-") else tgt_str

        # SECURITY: Refuse restoring full channel post archives to private users
        if isinstance(tgt_chat, int) and tgt_chat > 0:
            logger.error(f"SECURITY ALERT: Refusing to restore channel archive to private user chat {tgt_chat}")
            return {"total_archived": len(backups), "restored": 0, "failed": len(backups), "error": "Private user chat target not permitted"}

        logger.info(f"Starting disaster restore for pair #{pair_id} to {tgt_chat} ({len(backups)} posts)...")

        # Group consecutive items by media_group_id if they belong to an album
        grouped_entries = []
        current_group_id = None
        current_group = []

        for item in backups:
            mg_id = item.get("media_group_id")
            if mg_id and item.get("media_type") in ("photo", "video", "media_group"):
                if current_group_id == mg_id:
                    current_group.append(item)
                else:
                    if current_group:
                        grouped_entries.append((current_group_id, current_group))
                    current_group_id = mg_id
                    current_group = [item]
            else:
                if current_group:
                    grouped_entries.append((current_group_id, current_group))
                    current_group_id = None
                    current_group = []
                grouped_entries.append((None, [item]))

        if current_group:
            grouped_entries.append((current_group_id, current_group))

        for mg_id, items in grouped_entries:
            try:
                # 1. Attempt sending as media group if multiple album items exist
                if mg_id and len(items) > 1:
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
                    if len(media_list) > 1:
                        try:
                            await self._safe_send(bot.send_media_group, chat_id=tgt_chat, media=media_list)
                            restored += len(items)
                            await asyncio.sleep(0.5)
                            continue
                        except Exception as e_album:
                            logger.warning(f"Media group restore failed ({e_album}), falling back to single items...")

                # 2. Single item restore (or fallback from failed album)
                for item in items:
                    raw_text = item.get("text") or ""
                    media_type = item.get("media_type")
                    media_file_id = item.get("media_file_id")

                    valid_media = bool(media_file_id and not media_file_id.isdigit())
                    caption, overflow = TextProcessor.fit_caption_limit(raw_text, max_limit=1020)

                    if media_type == "photo" and valid_media:
                        await self._safe_send(bot.send_photo, chat_id=tgt_chat, photo=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif media_type in ("video", "video_note") and valid_media:
                        await self._safe_send(bot.send_video, chat_id=tgt_chat, video=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif media_type == "media_group" and valid_media:
                        try:
                            await self._safe_send(bot.send_photo, chat_id=tgt_chat, photo=media_file_id, caption=caption or None, parse_mode="HTML")
                        except Exception as e_photo:
                            if "wrong type" in str(e_photo).lower() or "file_id" in str(e_photo).lower():
                                try:
                                    await self._safe_send(bot.send_video, chat_id=tgt_chat, video=media_file_id, caption=caption or None, parse_mode="HTML")
                                except Exception:
                                    await self._safe_send(bot.send_document, chat_id=tgt_chat, document=media_file_id, caption=caption or None, parse_mode="HTML")
                            else:
                                raise
                    elif media_type == "document" and valid_media:
                        await self._safe_send(bot.send_document, chat_id=tgt_chat, document=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif media_type == "audio" and valid_media:
                        await self._safe_send(bot.send_audio, chat_id=tgt_chat, audio=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif media_type == "voice" and valid_media:
                        await self._safe_send(bot.send_voice, chat_id=tgt_chat, voice=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif media_type == "animation" and valid_media:
                        await self._safe_send(bot.send_animation, chat_id=tgt_chat, animation=media_file_id, caption=caption or None, parse_mode="HTML")
                    elif raw_text:
                        chunks = TextProcessor.fit_text_limit(raw_text, max_limit=4096)
                        for chunk in chunks:
                            await self._safe_send(bot.send_message, chat_id=tgt_chat, text=chunk, parse_mode="HTML")

                    if overflow and valid_media and TextProcessor.get_visible_text_length(overflow) > 0:
                        try:
                            await self._safe_send(bot.send_message, chat_id=tgt_chat, text=overflow, parse_mode="HTML")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                    restored += 1
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Failed to restore archived post group: {e}")
                failed += len(items)

        return {
            "total_archived": len(backups),
            "restored": restored,
            "failed": failed
        }

disaster_recovery_service = DisasterRecoveryService()
