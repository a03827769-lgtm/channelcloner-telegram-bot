import logging
from typing import Any, Dict, Mapping, Optional
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.fsm.state import State
from database.db_manager import DatabaseManager, db_manager

logger = logging.getLogger(__name__)

def _destiny(key: StorageKey) -> str:
    """Business-connection chats get their own FSM namespace (StorageKey.business_connection_id)."""
    destiny = key.destiny or "default"
    bc_id = getattr(key, "business_connection_id", None)
    return f"{destiny}|bc:{bc_id}" if bc_id else destiny


class SQLiteStorage(BaseStorage):
    """
    Production-grade SQLite-backed persistent FSM storage for Aiogram 3.
    Preserves all user conversation states, multi-step wizards, and data
    across application restarts, redeployments, and crashes.
    """
    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or db_manager

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = state.state if isinstance(state, State) else state
        await self.db.set_fsm_state(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id,
            destiny=_destiny(key),
            state=state_str
        )

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return await self.db.get_fsm_state(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id,
            destiny=_destiny(key)
        )

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self.db.set_fsm_data(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id,
            destiny=_destiny(key),
            data=dict(data)
        )

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        return await self.db.get_fsm_data(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id,
            destiny=_destiny(key)
        )

    async def clear(self, key: StorageKey) -> None:
        await self.db.clear_fsm(
            bot_id=key.bot_id,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id=key.thread_id,
            destiny=_destiny(key)
        )

    async def close(self) -> None:
        pass
