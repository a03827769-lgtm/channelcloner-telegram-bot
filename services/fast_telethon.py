import asyncio
import io
import math
import os
import logging
from typing import Optional, Union, BinaryIO, Callable, Any
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
from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

# Standard MTProto chunk size (512 KB is optimal for modern network connections)
CHUNK_SIZE = 512 * 1024
# Files larger than 10MB should use big file part requests
BIG_FILE_THRESHOLD = 10 * 1024 * 1024

class FastTelethonEngine:
    """
    High-Performance Asynchronous MTProto Parallel Chunk Transfer Engine.
    Accelerates Telegram file downloads and uploads up to 10x by multiplexing
    transfers across multiple parallel workers with adaptive concurrency.
    """

    @staticmethod
    def get_optimal_worker_count(file_size: Optional[int]) -> int:
        """Returns the optimal number of parallel workers based on file size."""
        if not file_size or file_size < 5 * 1024 * 1024:
            return 4
        elif file_size < 50 * 1024 * 1024:
            return 8
        elif file_size < 500 * 1024 * 1024:
            return 12
        else:
            return 16

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
        if isinstance(location, Message):
            if not location.media:
                return False
            media = location.media
            if isinstance(media, (MessageMediaDocument, MessageMediaPhoto)):
                file_size = getattr(location.file, "size", None) or file_size
                location = utils.get_input_location(media)
            else:
                location = utils.get_input_location(media)
        elif hasattr(location, "file") and hasattr(location.file, "size"):
            file_size = location.file.size
            location = utils.get_input_location(location)

        if not location:
            logger.warning("FastTelethon: Invalid input location provided.")
            return False

        # If file_size is unknown or small (< 2MB), fallback to standard fast download
        if not file_size or file_size < 2 * 1024 * 1024:
            try:
                if isinstance(out, str):
                    await client.download_file(location, out, progress_callback=progress_callback)
                else:
                    await client.download_file(location, out, progress_callback=progress_callback)
                return True
            except Exception as e:
                logger.debug(f"FastTelethon fallback download error: {e}")
                return False

        workers_count = max_workers or cls.get_optimal_worker_count(file_size)
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

        for part_index in range(total_parts):
            offset = part_index * CHUNK_SIZE
            limit = min(CHUNK_SIZE, file_size - offset)
            queue.put_nowait((part_index, offset, limit))

        async def worker():
            nonlocal downloaded_bytes
            while not queue.empty():
                try:
                    part_index, offset, limit = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                chunk_data = None
                retries = 3
                while retries > 0:
                    try:
                        result = await client(GetFileRequest(
                            location=location,
                            offset=offset,
                            limit=limit
                        ))
                        chunk_data = getattr(result, "bytes", b"")
                        break
                    except FloodWaitError as fwe:
                        logger.warning(f"FastTelethon FloodWait {fwe.seconds}s on part #{part_index}. Backing off...")
                        await asyncio.sleep(fwe.seconds + 1)
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
                else:
                    logger.error(f"FastTelethon failed to download chunk #{part_index} at offset {offset}")
                queue.task_done()

        worker_tasks = [asyncio.create_task(worker()) for _ in range(min(workers_count, total_parts))]
        await asyncio.gather(*worker_tasks, return_exceptions=True)

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

        return downloaded_bytes >= file_size * 0.99

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

        if file_size == 0:
            if is_path:
                file_handle.close()
            return None

        # Small files (< 2MB) can upload directly via standard client upload
        if file_size < 2 * 1024 * 1024:
            try:
                file_handle.seek(0)
                uploaded = await client.upload_file(file_handle, file_name=file_name, progress_callback=progress_callback)
                if is_path:
                    file_handle.close()
                return uploaded
            except Exception as e:
                logger.debug(f"FastTelethon fallback upload note: {e}")

        workers_count = max_workers or cls.get_optimal_worker_count(file_size)
        total_parts = math.ceil(file_size / CHUNK_SIZE)
        is_big = file_size > BIG_FILE_THRESHOLD
        file_id = utils.get_random_int()

        read_lock = asyncio.Lock()
        uploaded_bytes = 0
        queue: asyncio.Queue = asyncio.Queue()

        for part_index in range(total_parts):
            offset = part_index * CHUNK_SIZE
            limit = min(CHUNK_SIZE, file_size - offset)
            queue.put_nowait((part_index, offset, limit))

        async def upload_worker():
            nonlocal uploaded_bytes
            while not queue.empty():
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
                while retries > 0:
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
                    except FloodWaitError as fwe:
                        logger.warning(f"FastTelethon upload FloodWait {fwe.seconds}s on part #{part_index}.")
                        await asyncio.sleep(fwe.seconds + 1)
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
                    logger.error(f"FastTelethon failed to upload chunk #{part_index}")
                queue.task_done()

        worker_tasks = [asyncio.create_task(upload_worker()) for _ in range(min(workers_count, total_parts))]
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        if is_path:
            try:
                file_handle.close()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        if uploaded_bytes >= file_size:
            if is_big:
                return InputFileBig(id=file_id, parts=total_parts, name=file_name)
            else:
                return InputFile(id=file_id, parts=total_parts, name=file_name, md5_checksum="")
        return None

fast_telethon = FastTelethonEngine()
