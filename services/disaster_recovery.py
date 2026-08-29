import asyncio
import json
import logging
from typing import Dict, Any, List, Optional
from database.db_manager import db_manager
from aiogram import Bot

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
        db = None
    ):
        """Saves a post into the persistent disaster recovery archive"""
        db_instance = db or db_manager
        try:
            await db_instance.save_channel_backup(
                pair_id=pair_id,
                source_id=source_id,
                message_id=message_id,
                text=text,
                media_type=media_type,
                media_file_id=media_file_id,
                entities_json=entities_json
            )
        except Exception as e:
            logger.error(f"Error archiving post for disaster recovery: {e}")

    async def restore_channel(
        self,
        bot: Bot,
        pair_id: int,
        new_target_channel: str,
        limit: int = 500,
        db = None
    ) -> Dict[str, int]:
        """
        Replays archived posts to a new target channel chronologically.
        """
        db_instance = db or db_manager
        backups = await db_instance.get_channel_backups(pair_id, limit=limit)
        restored = 0
        failed = 0

        logger.info(f"Starting disaster restore for pair #{pair_id} to {new_target_channel} ({len(backups)} posts)...")

        for item in backups:
            try:
                text = item["text"]
                media_type = item["media_type"]
                media_file_id = item["media_file_id"]

                # Safe file_id validation: Telegram file_ids are alphanumeric base64 strings, never pure short numbers
                valid_media = bool(media_file_id and not media_file_id.isdigit())

                if media_type == "photo" and valid_media:
                    await bot.send_photo(chat_id=new_target_channel, photo=media_file_id, caption=text or None, parse_mode="HTML")
                elif media_type == "video" and valid_media:
                    await bot.send_video(chat_id=new_target_channel, video=media_file_id, caption=text or None, parse_mode="HTML")
                elif media_type == "document" and valid_media:
                    await bot.send_document(chat_id=new_target_channel, document=media_file_id, caption=text or None, parse_mode="HTML")
                elif media_type == "audio" and valid_media:
                    await bot.send_audio(chat_id=new_target_channel, audio=media_file_id, caption=text or None, parse_mode="HTML")
                elif media_type == "voice" and valid_media:
                    await bot.send_voice(chat_id=new_target_channel, voice=media_file_id, caption=text or None, parse_mode="HTML")
                elif media_type == "animation" and valid_media:
                    await bot.send_animation(chat_id=new_target_channel, animation=media_file_id, caption=text or None, parse_mode="HTML")
                elif text:
                    await bot.send_message(chat_id=new_target_channel, text=text, parse_mode="HTML")
                
                restored += 1
                await asyncio.sleep(0.5)  # Flood protection rate limiter
            except Exception as e:
                logger.error(f"Failed to restore archived post #{item.get('message_id')}: {e}")
                failed += 1

        return {
            "total_archived": len(backups),
            "restored": restored,
            "failed": failed
        }

disaster_recovery_service = DisasterRecoveryService()
