import os
import asyncio
import logging
import uuid
import time
from typing import List, Dict, Optional, Callable, Any, Tuple
from telethon.tl.types import Message
import aiofiles.os

logger = logging.getLogger(__name__)

import re
from services.cache_manager import cache_manager

class MediaGroupBuffer:
    def __init__(self, debounce_delay: float = 1.2, max_wait: float = 4.0):
        self.debounce_delay = debounce_delay
        self.max_wait = max_wait
        self._buffers: Dict[Any, List[Message]] = {}
        self._timers: Dict[Any, asyncio.TimerHandle] = {}
        self._first_seen: Dict[Any, float] = {}
        self._flush_tasks: set[asyncio.Task] = set()

    def add_message(self, key: Any, message: Message, on_complete: Callable[[Any, List[Message]], Any]):
        if key not in self._buffers:
            self._buffers[key] = []
        
        now = time.monotonic()
        if key not in self._first_seen:
            self._first_seen[key] = now

        # Deduplicate message within the buffer
        if not any(m.id == message.id for m in self._buffers[key]):
            self._buffers[key].append(message)

        # Telegram albums have a hard limit of 10 items; flush immediately when reached
        if len(self._buffers[key]) >= 10:
            if key in self._timers:
                self._timers[key].cancel()
                self._timers.pop(key, None)
            self._spawn_flush(key, on_complete)
            return

        # If already waiting longer than max_wait, do not postpone timer further
        if now - self._first_seen[key] >= self.max_wait:
            if key not in self._timers:
                loop = asyncio.get_running_loop()
                self._timers[key] = loop.call_later(0.1, lambda k=key, oc=on_complete: self._spawn_flush(k, oc))
            return

        if key in self._timers:
            self._timers[key].cancel()

        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(
            self.debounce_delay,
            lambda k=key, oc=on_complete: self._spawn_flush(k, oc)
        )

    def _spawn_flush(self, key: Any, on_complete: Callable[[Any, List[Message]], Any]):
        task = asyncio.create_task(self._flush_group(key, on_complete))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush_group(self, key: Any, on_complete: Callable[[Any, List[Message]], Any]):
        messages = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        self._first_seen.pop(key, None)
        if messages:
            try:
                messages.sort(key=lambda m: m.id)
                await on_complete(key, messages)
            except Exception as e:
                logger.error(f"Error flushing media group {key}: {e}", exc_info=True)

    async def flush_all(self, on_complete: Optional[Callable[[Any, List[Message]], Any]] = None):
        """Immediately flushes all pending in-flight buffered media groups on shutdown"""
        keys = list(self._buffers.keys())
        for key in keys:
            timer = self._timers.pop(key, None)
            if timer:
                timer.cancel()
            messages = self._buffers.pop(key, [])
            if messages and on_complete:
                try:
                    messages.sort(key=lambda m: m.id)
                    await on_complete(key, messages)
                except Exception as e:
                    logger.error(f"Error flushing pending media group {key} on shutdown: {e}")
        if self._flush_tasks:
            await asyncio.gather(*list(self._flush_tasks), return_exceptions=True)


class MediaHandler:
    def __init__(self, temp_dir: str = "temp_media"):
        self.temp_dir = temp_dir
        os.makedirs(self.temp_dir, exist_ok=True)
        self.album_buffer = MediaGroupBuffer(debounce_delay=1.2)
        self._cleanup_task: Optional[asyncio.Task] = None

    async def flush_pending_albums(self, on_complete: Optional[Callable[[Any, List[Message]], Any]] = None):
        """Flushes any in-flight media groups immediately"""
        await self.album_buffer.flush_all(on_complete)

    async def download_telethon_media_bytes(
        self,
        message: Message,
        max_size_bytes: int = 20 * 1024 * 1024,
        max_bytes: Optional[int] = None
    ) -> Optional[bytes]:
        """
        Downloads media directly into an in-memory BytesIO buffer without touching disk.
        Returns raw bytes directly.
        Only intended for files <= max_size_bytes (default 20MB) to conserve RAM.
        """
        effective_max = max_bytes if max_bytes is not None else max_size_bytes
        if not getattr(message, "media", None):
            return None

        msg_file = getattr(message, "file", None)
        file_size = getattr(msg_file, "size", None)
        if isinstance(file_size, (int, float)) and file_size > effective_max:
            return None

        import io
        stream = io.BytesIO()
        try:
            from services.fast_telethon import fast_telethon
            client = getattr(message, "client", None)
            downloaded = False
            if client and isinstance(file_size, (int, float)) and file_size > 2 * 1024 * 1024:
                downloaded = await fast_telethon.download_file_parallel(
                    client=client,
                    location=message,
                    out=stream,
                    file_size=file_size
                )

            if not downloaded:
                stream.seek(0)
                stream.truncate(0)
                res = await message.download_media(file=stream)
                downloaded = bool(res is not None)

            if downloaded:
                data = stream.getvalue()
                if len(data) > 0:
                    return data
            return None
        except Exception as e:
            logger.debug(f"In-memory media download notice for message {message.id}: {e}")
            return None

    async def download_telethon_media(
        self,
        message: Message,
        max_file_size: int = 2000 * 1024 * 1024
    ) -> Optional[str]:
        """
        Downloads media to temporary disk storage.
        Uses FastTelethon parallel chunk downloading for files > 2MB (up to 2GB).
        """
        if not message.media:
            return None
        
        orig_name = getattr(message.file, "name", None) if hasattr(message, "file") else None
        ext = getattr(message.file, "ext", "") if hasattr(message, "file") else ""
        if not ext:
            if message.photo:
                ext = ".jpg"
            elif message.video or message.video_note:
                ext = ".mp4"
            elif message.voice:
                ext = ".ogg"
            elif message.audio:
                ext = ".mp3"
            elif message.sticker:
                ext = ".webp"
            elif message.gif:
                ext = ".mp4"
            elif message.document and hasattr(message.document, "mime_type"):
                mime = message.document.mime_type or ""
                if "pdf" in mime:
                    ext = ".pdf"
                elif "zip" in mime:
                    ext = ".zip"
                elif "image" in mime:
                    ext = ".jpg"
                elif "video" in mime:
                    ext = ".mp4"
                elif "audio" in mime:
                    ext = ".mp3"
                else:
                    ext = ".dat"
            else:
                ext = ".jpg" if message.photo else ".dat"

        if not ext.startswith("."):
            ext = f".{ext}"

        if orig_name:
            clean_name = re.sub(r'[^\w\.-]', '_', str(orig_name))
            base_name, _ = os.path.splitext(clean_name)
            base_name = base_name[:50]
            clean_name = f"{base_name}{ext}"
            filename = f"{uuid.uuid4().hex[:8]}_{clean_name}"
        else:
            filename = f"{uuid.uuid4().hex[:12]}_{message.id}{ext}"
        
        temp_path = os.path.join(self.temp_dir, filename)
        
        # Support up to 2GB files via MTProto
        msg_file = getattr(message, "file", None)
        file_size = getattr(msg_file, "size", None)
        if isinstance(file_size, (int, float)) and file_size > 0:
            if file_size > max_file_size:
                logger.warning(f"Skipping media download for message {message.id}: file size ({file_size / (1024*1024):.1f} MB) exceeds limit of {max_file_size / (1024*1024):.0f} MB.")
                return None

        try:
            # 1. Try FastTelethon parallel chunk download if client and file size available
            client = getattr(message, "client", None)
            if client and isinstance(file_size, (int, float)) and file_size > 2 * 1024 * 1024:
                from services.fast_telethon import fast_telethon
                success = await fast_telethon.download_file_parallel(
                    client=client,
                    location=message,
                    out=temp_path,
                    file_size=file_size
                )
                if success and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    return temp_path

            # 2. Fallback to standard download_media
            downloaded_path = await message.download_media(file=temp_path)
            if downloaded_path and os.path.exists(downloaded_path):
                if os.path.getsize(downloaded_path) == 0:
                    logger.warning(f"Downloaded media for message {message.id} is 0 bytes. Discarding corrupt file.")
                    try:
                        os.remove(downloaded_path)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    return None
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

    async def stop_background_cleanup(self):
        """Stops the periodic temp media cleanup task cleanly"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    def purge_temp_media(self, max_age: int = 3600) -> int:
        """Immediately cleans up expired or orphaned files in temp_media on shutdown or startup"""
        if not os.path.exists(self.temp_dir):
            return 0
        now = time.time()
        cleaned_count = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            if not os.path.isfile(fpath):
                continue
            story_prefixes = ("queued_", "backup_", "story_", "overlay_", "collage_", "video_story_", "blank_slide_")
            if fname.startswith(story_prefixes) and (now - os.path.getmtime(fpath) < 86400):
                continue
            if fname.endswith((".db", ".db-wal", ".db-shm")):
                continue
            if max_age <= 0 or (now - os.path.getmtime(fpath) >= max_age):
                try:
                    os.remove(fpath)
                    cleaned_count += 1
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        return cleaned_count

    async def _periodic_cleaner(self, interval: int, max_age: int):
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.time()
                if os.path.exists(self.temp_dir):
                    for fname in os.listdir(self.temp_dir):
                        fpath = os.path.join(self.temp_dir, fname)
                        if os.path.isfile(fpath):
                            # Clean up old test DBs after 10 minutes, and general orphan DBs after 24h
                            if fname.endswith((".db", ".db-wal", ".db-shm")):
                                if fname.startswith("test_") and (now - os.path.getmtime(fpath) > 600):
                                    try:
                                        os.remove(fpath)
                                    except Exception:
                                        logger.debug("Ignored exception", exc_info=True)
                                continue
                            # Protect active queued messages, story assets, and database backups for up to 24 hours
                            story_prefixes = ("queued_", "backup_", "story_", "overlay_", "collage_", "video_story_", "blank_slide_")
                            if fname.startswith(story_prefixes) and (now - os.path.getmtime(fpath) < 86400):
                                continue
                            if now - os.path.getmtime(fpath) > max_age:
                                try:
                                    os.remove(fpath)
                                    logger.debug(f"Cleaned orphan temp file: {fpath}")
                                except Exception:
                                    logger.debug("Ignored exception", exc_info=True)
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
        
        media_cls = message.media.__class__.__name__
        if "WebPage" in media_cls or "Empty" in media_cls:
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
        if message.gif or (message.document and getattr(message.document, 'mime_type', None) == 'image/gif'):
            return "animation"
        if message.document:
            return "document"
        if message.poll:
            return "poll"
        if message.contact:
            return "contact"
        if message.geo or message.venue:
            return "location"
        
        if getattr(message, 'message', None) or getattr(message, 'text', None):
            return "text"

        return "generic_media"

media_handler = MediaHandler()
