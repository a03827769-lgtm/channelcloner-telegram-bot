import os
import asyncio
import logging
import uuid
import time
from typing import List, Dict, Optional, Callable, Any
from telethon.tl.types import Message
import aiofiles.os

logger = logging.getLogger(__name__)

class MediaGroupBuffer:
    def __init__(self, debounce_delay: float = 2.0):
        self.debounce_delay = debounce_delay
        self._buffers: Dict[Any, List[Message]] = {}
        self._timers: Dict[Any, asyncio.TimerHandle] = {}

    def add_message(self, key: Any, message: Message, on_complete: Callable[[Any, List[Message]], Any]):
        if key not in self._buffers:
            self._buffers[key] = []
        
        # Deduplicate message within the buffer
        if not any(m.id == message.id for m in self._buffers[key]):
            self._buffers[key].append(message)

        if key in self._timers:
            self._timers[key].cancel()

        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(
            self.debounce_delay,
            lambda k=key, oc=on_complete: asyncio.create_task(self._flush_group(k, oc))
        )

    async def _flush_group(self, key: Any, on_complete: Callable[[Any, List[Message]], Any]):
        messages = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if messages:
            try:
                messages.sort(key=lambda m: m.id)
                await on_complete(key, messages)
            except Exception as e:
                logger.error(f"Error flushing media group {key}: {e}", exc_info=True)


class MediaHandler:
    def __init__(self, temp_dir: str = "temp_media"):
        self.temp_dir = temp_dir
        os.makedirs(self.temp_dir, exist_ok=True)
        self.album_buffer = MediaGroupBuffer()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def download_telethon_media(self, message: Message) -> Optional[str]:
        if not message.media:
            return None
        
        filename = f"{uuid.uuid4().hex[:12]}_{message.id}"
        temp_path = os.path.join(self.temp_dir, filename)
        
        try:
            downloaded_path = await message.download_media(file=temp_path)
            return downloaded_path
        except Exception as e:
            logger.error(f"Failed to download media for message {message.id}: {e}")
            return None

    @staticmethod
    async def cleanup_files(file_paths: List[Optional[str]]):
        for path in file_paths:
            if path and os.path.exists(path):
                try:
                    await aiofiles.os.remove(path)
                except Exception as e:
                    logger.warning(f"Failed to remove temporary file {path}: {e}")

    def start_background_cleanup(self, interval: int = 600, max_age: int = 600):
        """Starts periodic cleanup of orphaned temp files"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleaner(interval, max_age))

    async def _periodic_cleaner(self, interval: int, max_age: int):
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.time()
                if os.path.exists(self.temp_dir):
                    for fname in os.listdir(self.temp_dir):
                        fpath = os.path.join(self.temp_dir, fname)
                        if os.path.isfile(fpath):
                            if now - os.path.getmtime(fpath) > max_age:
                                try:
                                    os.remove(fpath)
                                    logger.debug(f"Cleaned orphan temp file: {fpath}")
                                except Exception:
                                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleaner: {e}")

    @staticmethod
    def is_media_group(message: Message) -> bool:
        return bool(message.grouped_id)

    @staticmethod
    def get_media_type(message: Message) -> str:
        if not message.media:
            return "text"
        
        if message.photo:
            return "photo"
        if message.voice:
            return "voice"
        if message.video_note:
            return "video_note"
        if message.video:
            return "video"
        if message.audio:
            return "audio"
        if message.sticker:
            return "sticker"
        if message.gif or (message.document and message.document.mime_type == 'image/gif'):
            return "animation"
        if message.document:
            return "document"
        if message.poll:
            return "poll"
        if message.contact:
            return "contact"
        if message.geo or message.venue:
            return "location"
        
        return "generic_media"

media_handler = MediaHandler()
