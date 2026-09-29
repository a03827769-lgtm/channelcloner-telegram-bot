import asyncio
import io
import math
import os
import time
import logging
from typing import Optional, Union, BinaryIO, Callable, Any, Dict, Tuple
from telethon import TelegramClient, utils
from telethon.tl.types import (
    InputFile,
    InputFileBig,
    TypeInputFileLocation,
    Document,
    Photo,
    MessageMediaDocument,
    MessageMediaPhoto,
    Message
)
from telethon.tl.functions.upload import (
    GetFileRequest,
    SaveFilePartRequest,
    SaveBigFilePartRequest
)
from telethon.errors import FloodWaitError, FloodPremiumWaitError

logger = logging.getLogger(__name__)

# Standard MTProto chunk size (512 KB is optimal for modern network connections)
CHUNK_SIZE = 512 * 1024
# Files larger than 10MB should use big file part requests
BIG_FILE_THRESHOLD = 10 * 1024 * 1024
# Flood waits up to this long are waited out inside a worker; longer ones abort the parallel transfer at once so
# the caller (who usually holds the shared media semaphore) falls back to the regular single-stream transfer
# instead of sleeping for minutes.
SHORT_FLOOD_WAIT_SECONDS = 3
# Non-premium accounts are throttled server-side (FLOOD_PREMIUM_WAIT); many parallel streams only trigger it
NON_PREMIUM_MAX_WORKERS = 3
_PREMIUM_CACHE_TTL = 3600.0


class _AbortTransfer(Exception):
    """Raised inside a transfer worker to stop the whole parallel transfer."""


class FastTelethonEngine:
    """
    High-Performance Asynchronous MTProto Parallel Chunk Transfer Engine.
    Accelerates Telegram file downloads and uploads up to 10x by multiplexing
    transfers across multiple parallel workers with adaptive concurrency.
    """

    _premium_cache: Dict[int, Tuple[float, Optional[bool]]] = {}

    @staticmethod
    def get_optimal_worker_count(file_size: Optional[int], premium: Optional[bool] = None) -> int:
        """Returns the optimal number of parallel workers based on file size (fewer for non-premium accounts)."""
        if not file_size or file_size < 5 * 1024 * 1024:
            count = 4
        elif file_size < 50 * 1024 * 1024:
            count = 8
        elif file_size < 500 * 1024 * 1024:
            count = 12
        else:
            count = 16
        if premium is False:
            count = min(count, NON_PREMIUM_MAX_WORKERS)
        return count

    @classmethod
    async def _client_is_premium(cls, client: Any) -> Optional[bool]:
        """Premium flag of the logged-in account (cached for an hour); None when unknown."""
        key = id(client)
        cached = cls._premium_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < _PREMIUM_CACHE_TTL:
            return cached[1]
        premium: Optional[bool] = None
        try:
            me = await client.get_me()
            value = getattr(me, "premium", None)
            premium = value if isinstance(value, bool) else None
        except Exception:
            logger.debug("FastTelethon: could not determine premium status", exc_info=True)
        cls._premium_cache[key] = (now, premium)
        return premium

    @staticmethod
    async def _handle_flood(err: Exception, part_index: int, direction: str) -> None:
        seconds = int(getattr(err, "seconds", 0) or 0)
        if seconds <= SHORT_FLOOD_WAIT_SECONDS:
            await asyncio.sleep(seconds + 1)
            return
        logger.warning(
            f"FastTelethon {direction} FloodWait {seconds}s on part #{part_index}; aborting the parallel transfer "
            f"(falling back to the standard single-stream transfer)."
        )
        raise _AbortTransfer()

    @classmethod
    async def download_file_parallel(
        cls,
        client: TelegramClient,
        location: Union[Message, TypeInputFileLocation, Document, Photo, Any],
        out: Union[str, BinaryIO, io.BytesIO],
        file_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], Any]] = None,
        max_workers: Optional[int] = None
    ) -> bool:
        """
        Downloads a media file in parallel chunks directly into memory or disk.
        """
        dc_id = None
        try:
            if isinstance(location, Message):
                if not location.media:
                    return False
                file_size = getattr(location.file, "size", None) or file_size
                dc_id, location = utils.get_input_location(location.media)
            elif hasattr(location, "file") and hasattr(location.file, "size"):
                file_size = location.file.size
                dc_id, location = utils.get_input_location(location)
            elif isinstance(location, (Document, Photo, MessageMediaDocument, MessageMediaPhoto)):
                dc_id, location = utils.get_input_location(location)
        except Exception:
            logger.debug("FastTelethon: could not resolve input file location", exc_info=True)
            return False

        if not location:
            logger.warning("FastTelethon: Invalid input location provided.")
            return False

        # Raw GetFileRequest calls only work against the client's home DC. Files stored in another
        # DC are delegated to Telethon's own downloader, which transparently handles DC migration.
        home_dc = getattr(getattr(client, "session", None), "dc_id", None)
        if dc_id is not None and home_dc is not None and dc_id != home_dc:
            return False

        # If file_size is unknown or small (< 2MB), fallback to standard fast download
        if not file_size or file_size < 2 * 1024 * 1024:
            try:
                await client.download_file(location, out, progress_callback=progress_callback)
                return True
            except Exception as e:
                logger.debug(f"FastTelethon fallback download error: {e}")
                return False

        premium = await cls._client_is_premium(client) if max_workers is None else None
        workers_count = max_workers or cls.get_optimal_worker_count(file_size, premium=premium)
        total_parts = math.ceil(file_size / CHUNK_SIZE)

        is_path = isinstance(out, str)
        if is_path:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            file_handle = open(out, "wb+")
        else:
            file_handle = out

        write_lock = asyncio.Lock()
        downloaded_bytes = 0
        queue: asyncio.Queue = asyncio.Queue()

        failed_parts = 0
        for part_index in range(total_parts):
            # MTProto requires offset % limit == 0, limit % 4096 == 0 and 1 MiB % limit == 0, so every
            # request (including the last one) asks for a full chunk; the server returns the remainder.
            queue.put_nowait((part_index, part_index * CHUNK_SIZE, CHUNK_SIZE))

        async def worker():
            nonlocal downloaded_bytes, failed_parts
            while not queue.empty() and failed_parts == 0:
                try:
                    part_index, offset, limit = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                chunk_data = None
                retries = 3
                while retries > 0 and failed_parts == 0:
                    try:
                        result = await client(GetFileRequest(
                            location=location,
                            offset=offset,
                            limit=limit
                        ))
                        chunk_data = getattr(result, "bytes", b"")
                        break
                    except (FloodWaitError, FloodPremiumWaitError) as fwe:
                        try:
                            await cls._handle_flood(fwe, part_index, "download")
                        except _AbortTransfer:
                            failed_parts += 1
                            break
                        retries -= 1
                    except Exception as req_err:
                        logger.debug(f"FastTelethon part #{part_index} retry notice: {req_err}")
                        retries -= 1
                        await asyncio.sleep(0.5)

                if chunk_data is not None:
                    async with write_lock:
                        def _sync_write(f=file_handle, off=offset, data=chunk_data):
                            f.seek(off)
                            f.write(data)
                        await asyncio.to_thread(_sync_write)
                        downloaded_bytes += len(chunk_data)
                        if progress_callback:
                            try:
                                progress_callback(downloaded_bytes, file_size)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                elif failed_parts == 0:
                    failed_parts += 1
                    logger.warning(f"FastTelethon failed to download chunk #{part_index} at offset {offset}; falling back to standard download")
                queue.task_done()

        try:
            worker_tasks = [asyncio.create_task(worker()) for _ in range(min(workers_count, total_parts))]
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        finally:
            if is_path:
                try:
                    file_handle.close()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            else:
                try:
                    file_handle.seek(0)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

        # A partially downloaded file is corrupt media; only an exact, complete download counts as success
        return failed_parts == 0 and downloaded_bytes >= file_size

    @classmethod
    async def upload_file_parallel(
        cls,
        client: TelegramClient,
        file_input: Union[str, BinaryIO, io.BytesIO, bytes],
        file_name: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int], Any]] = None,
        max_workers: Optional[int] = None
    ) -> Optional[Union[InputFile, InputFileBig]]:
        """
        Uploads a file in parallel chunks up to 2GB via MTProto.

        Returns a bare InputFile / InputFileBig whose `name` is file_name. It carries no media metadata: callers must
        pass the document attributes (duration, dimensions, voice flag, file name), mime type and thumb to
        send_file() themselves, otherwise Telegram shows videos as 1x1 / 0:00 and audio as a plain file.
        """
        is_path = isinstance(file_input, str)
        if is_path:
            file_size = os.path.getsize(file_input)
            file_name = file_name or os.path.basename(file_input)
            file_handle = open(file_input, "rb")
        elif isinstance(file_input, bytes):
            file_size = len(file_input)
            file_handle = io.BytesIO(file_input)
            file_name = file_name or "file.dat"
        else:
            file_handle = file_input
            file_handle.seek(0, os.SEEK_END)
            file_size = file_handle.tell()
            file_handle.seek(0)
            file_name = file_name or "file.dat"

        try:
            if file_size == 0:
                return None

            # Small files (< 2MB) can upload directly via standard client upload
            if file_size < 2 * 1024 * 1024:
                try:
                    file_handle.seek(0)
                    return await client.upload_file(file_handle, file_name=file_name, progress_callback=progress_callback)
                except Exception as e:
                    logger.debug(f"FastTelethon fallback upload note: {e}")
                    return None

            premium = await cls._client_is_premium(client) if max_workers is None else None
            workers_count = max_workers or cls.get_optimal_worker_count(file_size, premium=premium)
            total_parts = math.ceil(file_size / CHUNK_SIZE)
            is_big = file_size > BIG_FILE_THRESHOLD
            file_id = utils.get_random_int()

            read_lock = asyncio.Lock()
            uploaded_bytes = 0
            failed_parts = 0
            queue: asyncio.Queue = asyncio.Queue()

            for part_index in range(total_parts):
                offset = part_index * CHUNK_SIZE
                limit = min(CHUNK_SIZE, file_size - offset)
                queue.put_nowait((part_index, offset, limit))

            async def upload_worker():
                nonlocal uploaded_bytes, failed_parts
                while not queue.empty() and failed_parts == 0:
                    try:
                        part_index, offset, limit = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    async with read_lock:
                        def _sync_read(f=file_handle, off=offset, lim=limit):
                            f.seek(off)
                            return f.read(lim)
                        chunk_bytes = await asyncio.to_thread(_sync_read)

                    retries = 3
                    success = False
                    while retries > 0 and failed_parts == 0:
                        try:
                            if is_big:
                                req = SaveBigFilePartRequest(
                                    file_id=file_id,
                                    file_part=part_index,
                                    file_total_parts=total_parts,
                                    bytes=chunk_bytes
                                )
                            else:
                                req = SaveFilePartRequest(
                                    file_id=file_id,
                                    file_part=part_index,
                                    bytes=chunk_bytes
                                )
                            await client(req)
                            success = True
                            break
                        except (FloodWaitError, FloodPremiumWaitError) as fwe:
                            try:
                                await cls._handle_flood(fwe, part_index, "upload")
                            except _AbortTransfer:
                                break
                            retries -= 1
                        except Exception as req_err:
                            logger.debug(f"FastTelethon upload part #{part_index} retry notice: {req_err}")
                            retries -= 1
                            await asyncio.sleep(0.5)

                    if success:
                        uploaded_bytes += len(chunk_bytes)
                        if progress_callback:
                            try:
                                progress_callback(uploaded_bytes, file_size)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                    else:
                        failed_parts += 1
                        logger.warning(f"FastTelethon failed to upload chunk #{part_index}; falling back to standard upload")
                    queue.task_done()

            worker_tasks = [asyncio.create_task(upload_worker()) for _ in range(min(workers_count, total_parts))]
            await asyncio.gather(*worker_tasks, return_exceptions=True)

            if failed_parts == 0 and uploaded_bytes >= file_size:
                if is_big:
                    return InputFileBig(id=file_id, parts=total_parts, name=file_name)
                return InputFile(id=file_id, parts=total_parts, name=file_name, md5_checksum="")
            return None
        finally:
            if is_path:
                try:
                    file_handle.close()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

fast_telethon = FastTelethonEngine()
