import os
import gc
import asyncio
import logging

logger = logging.getLogger(__name__)

async def safe_cleanup_db(db_path: str, db_instance=None, retries: int = 6, delay: float = 0.05):
    """
    Safely closes a DatabaseManager instance and deletes the SQLite DB, WAL, and SHM files.
    Specifically handles Windows OS file-lock delays via GC cycles and retry backoff.
    """
    if db_instance:
        try:
            await db_instance.close()
        except Exception as e:
            logger.debug(f"Error closing DB instance in safe_cleanup_db: {e}")

    gc.collect()

    targets = [db_path, f"{db_path}-wal", f"{db_path}-shm"]
    
    for _ in range(retries):
        all_cleaned = True
        for target in targets:
            if os.path.exists(target):
                try:
                    os.remove(target)
                except (PermissionError, OSError):
                    all_cleaned = False
                except Exception:
                    pass
        if all_cleaned:
            break
        await asyncio.sleep(delay)
        gc.collect()
