import asyncio
import time
import logging
from typing import Dict, List, Optional
from telethon import TelegramClient

logger = logging.getLogger(__name__)

class SessionPoolManager:
    """
    Multi-Account MTProto Session Failover Pool.
    Manages multiple Telegram accounts, balancing requests and instantly
    switching to standby sessions when any account hits FLOOD_WAIT limits.
    """

    def __init__(self):
        self._clients: Dict[str, TelegramClient] = {}
        self._cooldowns: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def register_client(self, session_name: str, client: TelegramClient):
        """Registers a live connected Telethon client into the pool"""
        self._clients[session_name] = client
        logger.info(f"Registered client '{session_name}' into MTProto Session Pool.")

    def record_flood_wait(self, session_name: str, seconds: int):
        """Marks a session as temporarily on cooldown due to FloodWait"""
        cooldown_until = time.time() + seconds + 1
        self._cooldowns[session_name] = cooldown_until
        logger.warning(f"⚠️ Session '{session_name}' entered FloodWait cooldown for {seconds}s (until {time.strftime('%H:%M:%S', time.localtime(cooldown_until))}).")

    def is_available(self, session_name: str) -> bool:
        """Returns True if the session client is connected and not in FloodWait"""
        client = self._clients.get(session_name)
        if not client or not client.is_connected():
            return False
        cd = self._cooldowns.get(session_name, 0.0)
        return time.time() >= cd

    def get_available_client(self) -> Optional[TelegramClient]:
        """
        Returns the first ready, non-cooldown client from the pool.
        """
        now = time.time()
        for name, client in self._clients.items():
            if client.is_connected() and now >= self._cooldowns.get(name, 0.0):
                return client
        return None

    def get_all_clients(self) -> List[TelegramClient]:
        """Returns all registered clients"""
        return list(self._clients.values())

    async def disconnect_all(self):
        """Gracefully disconnects all clients in the pool"""
        async with self._lock:
            for name, client in list(self._clients.items()):
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
            self._clients.clear()
            self._cooldowns.clear()

session_pool = SessionPoolManager()
