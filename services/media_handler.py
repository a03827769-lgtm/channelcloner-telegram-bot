import os
import re
import asyncio
import logging
import uuid
import time
from contextlib import contextmanager
from typing import List, Dict, Optional, Callable, Any, Iterable, Set
from telethon.tl.types import Message
import aiofiles.os

from config.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Files that nobody registered as "in use" are treated as orphans only after this age, whatever max_age a caller
# passes to the periodic cleaner (a 5-minute sweep used to delete files still needed by 429 retries or ffmpeg).
MIN_ORPHAN_AGE_SECONDS = 3600
# Work files of the story / overlay / backup pipelines keep a 24 h grace period
PROTECTED_PREFIXES = ("backup_", "story_", "overlay_", "collage_", "video_story_", "blank_slide_", "broadcast_")
PROTECTED_PREFIX_MAX_AGE = 86400
# Drip-feed media waits for its slot (scheduled at most ~1.5 days ahead). Files referenced by pending queue rows are
# never removed; unreferenced queued files are removed after an hour, and without queue information after 3 days.
QUEUED_PREFIX = "queued_"
QUEUED_MAX_AGE = 3 * 86400
# A hold that was never released (caller bug) stops protecting its file after this long
STALE_HOLD_SECONDS = 86400
# Late album items arriving this long after their album was flushed are logged as split albums
_LATE_ALBUM_WINDOW = 60.0


class MediaGroupBuffer:
    """Collects the messages of one album before cloning it.

    The flush is a sliding debounce: it fires `debounce_delay` seconds after the LAST item arrived, but never later than
    `max_wait` seconds after the first item, so slowly uploaded albums are not split into two posts."""

    def __init__(self, debounce_delay: float = 1.5, max_wait: float = 10.0):
        self.debounce_delay = debounce_delay
        self.max_wait = max_wait
        self._buffers: Dict[Any, List[Message]] = {}
        self._timers: Dict[Any, asyncio.TimerHandle] = {}
        self._first_seen: Dict[Any, float] = {}
        self._flush_tasks: set[asyncio.Task] = set()
        self._recently_flushed: Dict[Any, float] = {}

    def add_message(self, key: Any, message: Message, on_complete: Callable[[Any, List[Message]], Any]):
        now = time.monotonic()
        if key not in self._buffers:
            self._buffers[key] = []
            flushed_at = self._recently_flushed.get(key)
            if flushed_at is not None and now - flushed_at < _LATE_ALBUM_WINDOW:
                logger.warning(
                    f"Album {key}: item {getattr(message, 'id', '?')} arrived {now - flushed_at:.1f}s after the album was "
                    f"flushed; late items are cloned as a separate post."
                )

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

        # Sliding debounce, capped at max_wait after the first item of the album
        remaining_cap = self.max_wait - (now - self._first_seen[key])
        if remaining_cap <= 0:
            if key in self._timers:
                self._timers[key].cancel()
                self._timers.pop(key, None)
            self._spawn_flush(key, on_complete)
            return

        if key in self._timers:
            self._timers[key].cancel()

        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(
            min(self.debounce_delay, remaining_cap),
            lambda k=key, oc=on_complete: self._spawn_flush(k, oc)
        )

    def _spawn_flush(self, key: Any, on_complete: Callable[[Any, List[Message]], Any]):
        task = asyncio.create_task(self._flush_group(key, on_complete))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    def _mark_flushed(self, key: Any):
        now = time.monotonic()
        self._recently_flushed[key] = now
        if len(self._recently_flushed) > 500:
            for old_key, ts in list(self._recently_flushed.items()):
                if now - ts > _LATE_ALBUM_WINDOW:
                    self._recently_flushed.pop(old_key, None)

    async def _flush_group(self, key: Any, on_complete: Callable[[Any, List[Message]], Any]):
        messages = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        self._first_seen.pop(key, None)
        if messages:
            self._mark_flushed(key)
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
            self._first_seen.pop(key, None)
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
    def __init__(self, temp_dir: Optional[str] = None):
        # Absolute by default so every pipeline (clones, stories, watermarks) shares one directory regardless of CWD
        self.temp_dir = temp_dir or os.path.join(str(PROJECT_ROOT), "temp_media")
        os.makedirs(self.temp_dir, exist_ok=True)
        self.album_buffer = MediaGroupBuffer()
        self._cleanup_task: Optional[asyncio.Task] = None
        # In-use registry: path key -> reference count / time of first hold
        self._in_use: Dict[str, int] = {}
        self._in_use_since: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # In-use registry (clone jobs, video watermarking, story pipeline, ...)
    # ------------------------------------------------------------------

    @staticmethod
    def _path_key(path: Any) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def hold_path(self, path: Optional[Any]) -> None:
        """Marks a temp file as in use: the background cleaner never deletes it until release_path() is called.
        Reference counted, so several jobs may hold the same file."""
        if not path:
            return
        key = self._path_key(path)
        self._in_use[key] = self._in_use.get(key, 0) + 1
        self._in_use_since.setdefault(key, time.time())

    def release_path(self, path: Optional[Any]) -> None:
        """Drops one hold on the file (no-op for files that are not held)."""
        if not path:
            return
        key = self._path_key(path)
        count = self._in_use.get(key, 0) - 1
        if count > 0:
            self._in_use[key] = count
        else:
            self._in_use.pop(key, None)
            self._in_use_since.pop(key, None)

    def release_paths(self, paths: Iterable[Optional[Any]]) -> None:
        for path in paths:
            self.release_path(path)

    def is_held(self, path: Any) -> bool:
        key = self._path_key(path)
        if key not in self._in_use:
            return False
        if time.time() - self._in_use_since.get(key, time.time()) > STALE_HOLD_SECONDS:
            return False
        return True

    @contextmanager
    def holding(self, *paths: Optional[Any]):
        """Context manager protecting the given files from the background cleaner while the block runs."""
        held = [p for p in paths if p]
        for p in held:
            self.hold_path(p)
        try:
            yield
        finally:
            self.release_paths(held)

    async def flush_pending_albums(self, on_complete: Optional[Callable[[Any, List[Message]], Any]] = None):
        """Flushes any in-flight media groups immediately"""
        await self.album_buffer.flush_all(on_complete)

    @staticmethod
    def get_original_filename(message: Any) -> Optional[str]:
        """Original file name of a document (safe for use as the upload file name), or None when the source
        message carries no file name."""
        try:
            name = getattr(getattr(message, "file", None), "name", None)
        except Exception:
            name = None
        if not name or not isinstance(name, str):
            return None
        name = os.path.basename(name.replace("\\", "/"))
        name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', name).strip(" .")
        if not name:
            return None
        if len(name) > 120:
            base, ext = os.path.splitext(name)
            name = base[:max(1, 120 - len(ext))] + ext
        return name

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
        The returned file is registered as in use (see hold_path); cleanup_files() releases and deletes it.
        """
        if not message.media:
            return None

        orig_name = getattr(message.file, "name", None) if hasattr(message, "file") else None
        ext = getattr(message.file, "ext", "") if hasattr(message, "file") else ""
        if not isinstance(ext, str):
            ext = ""
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

        if orig_name and isinstance(orig_name, str):
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

        # Protected from the background cleaner while the download is still being written
        self.hold_path(temp_path)
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
                    self.release_path(temp_path)
                    return None
                if self._path_key(downloaded_path) != self._path_key(temp_path):
                    self.hold_path(downloaded_path)
                    self.release_path(temp_path)
                return downloaded_path
            self.release_path(temp_path)
            return downloaded_path
        except Exception as e:
            logger.error(f"Failed to download media for message {message.id}: {e}")
            self.release_path(temp_path)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            return None

    async def cleanup_files(self, file_paths: List[Optional[str]]):
        """Releases the in-use holds of the given temp files and deletes them."""
        for path in file_paths:
            if not path:
                continue
            self.release_path(path)
            if self.is_held(path):
                # Another job still uses the same file
                continue
            if os.path.exists(path):
                try:
                    await aiofiles.os.remove(path)
                except Exception as e:
                    logger.warning(f"Failed to remove temporary file {path}: {e}")

    def start_background_cleanup(self, interval: int = 600, max_age: int = MIN_ORPHAN_AGE_SECONDS):
        """Starts periodic cleanup of orphaned temp files. Untracked files are kept for at least
        MIN_ORPHAN_AGE_SECONDS even when a smaller max_age is requested."""
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

    def _is_expired(self, fpath: str, fname: str, age: float, max_age: float, queued_refs: Optional[Set[str]]) -> bool:
        """Decides whether one temp file may be deleted."""
        if self.is_held(fpath):
            return False
        if queued_refs is not None and self._path_key(fpath) in queued_refs:
            # Referenced by a pending drip-feed row (whatever its name): kept until the row is done
            return False
        if fname.startswith(QUEUED_PREFIX):
            if queued_refs is None:
                return age >= QUEUED_MAX_AGE
            return age >= MIN_ORPHAN_AGE_SECONDS
        if fname.startswith(PROTECTED_PREFIXES) and age < PROTECTED_PREFIX_MAX_AGE:
            return False
        return age >= max_age

    def purge_temp_media(self, max_age: int = 3600) -> int:
        """Immediately cleans up expired or orphaned files in temp_media on shutdown or startup.
        Held files and queued drip-feed media (which must survive a restart) are kept."""
        if not os.path.exists(self.temp_dir):
            return 0
        now = time.time()
        cleaned_count = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if fname.endswith((".db", ".db-wal", ".db-shm")):
                continue
            try:
                age = now - os.path.getmtime(fpath)
            except OSError:
                continue
            effective_max_age = 0 if max_age <= 0 else max_age
            if not self._is_expired(fpath, fname, age, effective_max_age, None):
                continue
            try:
                os.remove(fpath)
                cleaned_count += 1
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return cleaned_count

    async def _queued_media_references(self) -> Optional[Set[str]]:
        """Path keys of media referenced by pending drip-feed rows (None when the queue cannot be read)."""
        try:
            from services.drip_feed_queue import drip_feed_service
            paths = await drip_feed_service.get_pending_media_paths()
        except Exception:
            logger.debug("Could not read pending drip-feed media references", exc_info=True)
            return None
        return {self._path_key(p) for p in paths}

    async def _periodic_cleaner(self, interval: int, max_age: int):
        effective_max_age = max(int(max_age or 0), MIN_ORPHAN_AGE_SECONDS)
        while True:
            try:
                await asyncio.sleep(interval)
                if not os.path.exists(self.temp_dir):
                    continue
                queued_refs = await self._queued_media_references()
                now = time.time()
                for fname in os.listdir(self.temp_dir):
                    fpath = os.path.join(self.temp_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        age = now - os.path.getmtime(fpath)
                    except OSError:
                        continue
                    # Clean up old test DBs after 10 minutes, and never touch other databases
                    if fname.endswith((".db", ".db-wal", ".db-shm")):
                        if fname.startswith("test_") and age > 600:
                            try:
                                os.remove(fpath)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                        continue
                    if self._is_expired(fpath, fname, age, effective_max_age, queued_refs):
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
        # Video stickers and GIFs also carry DocumentAttributeVideo (message.video is truthy for them),
        # so they must be classified before plain videos.
        if message.sticker:
            return "sticker"
        if message.gif or (message.document and getattr(message.document, 'mime_type', None) == 'image/gif'):
            return "animation"
        if message.voice:
            return "voice"
        if message.video_note:
            return "video_note"
        if message.video:
            return "video"
        if message.audio:
            return "audio"
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
