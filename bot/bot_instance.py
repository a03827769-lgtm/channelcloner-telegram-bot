import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseEventIsolation, StorageKey
from aiohttp import ClientSession
from database.fsm_storage import SQLiteStorage
from config.settings import settings
from bot.handlers.start import router as start_router
from bot.handlers.stars_billing import router as stars_billing_router
from bot.handlers.cloner_menu import router as cloner_menu_router
from bot.handlers.settings_menu import router as settings_menu_router
from bot.handlers.history_clone import router as history_clone_router
from bot.handlers.help_guide import router as help_guide_router
from bot.handlers.story_menu import router as story_menu_router
from bot.handlers.inline_search import router as inline_search_router
from bot.handlers.comment_moderator import router as comment_moderator_router
from bot.middlewares.user_registration_middleware import UserRegistrationMiddleware
from bot.middlewares.throttling_middleware import ThrottlingMiddleware
from bot.middlewares.private_mode_middleware import PrivateModeGatekeeperMiddleware

logger = logging.getLogger(__name__)


class ResilientAiohttpSession(AiohttpSession):
    """Aiogram AiohttpSession subclass that safely manages connection pooling and proxy sanitization"""
    async def create_session(self) -> ClientSession:
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            # Strip broken or container-injected proxy variables
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                v = os.environ.get(k, "")
                if "8888" in v or "192.168.65.254" in v or "dozzle" in v.lower():
                    os.environ.pop(k, None)

            has_explicit_proxy = bool(getattr(settings, "PROXY_URL", None) or getattr(settings, "HTTPS_PROXY", None))
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                trust_env=has_explicit_proxy
            )
            self._should_reset_connector = False
        return self._session


class PerUserEventIsolation(BaseEventIsolation):
    """Processes updates that share an FSM key (the same user in the same chat) one at a time.

    Same guarantee as aiogram's SimpleEventIsolation — wizard steps, album parts and double taps can
    never interleave or lose FSM updates — but a key's lock is dropped as soon as nobody holds or waits
    for it. The bot sees every message of the discussion groups it moderates, so locks that are never
    released (SimpleEventIsolation keeps them forever) would grow without bound."""

    def __init__(self) -> None:
        # key -> [lock, number of updates holding or waiting for it]
        self._locks: Dict[StorageKey, List] = {}

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(key) is entry:
                del self._locks[key]

    async def close(self) -> None:
        self._locks.clear()


def create_bot() -> Bot:
    """Creates high-performance Aiogram Bot instance for public users"""
    session = ResilientAiohttpSession()
    return Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    )

def create_dispatcher(storage=None) -> Dispatcher:
    fsm_storage = storage if storage is not None else SQLiteStorage()
    # Updates of one user in one chat are handled strictly one after another (see PerUserEventIsolation);
    # long jobs (backfill, restore, catch-up) run as background tasks so they never hold this lock
    dp = Dispatcher(storage=fsm_storage, events_isolation=PerUserEventIsolation())

    # 1. Anti-flood / Throttling rate limiting middleware
    throttling_mw = ThrottlingMiddleware(rate_limit=0.5, burst_limit=5, window_seconds=2.0)
    dp.message.middleware(throttling_mw)
    dp.callback_query.middleware(throttling_mw)

    # 2. Attach automatic user registration middleware for all users
    user_reg_mw = UserRegistrationMiddleware()
    dp.message.middleware(user_reg_mw)
    dp.callback_query.middleware(user_reg_mw)

    # 3. Private Mode gatekeeper middleware (restricts unauthorized new users when private mode is active)
    private_gate_mw = PrivateModeGatekeeperMiddleware()
    dp.message.middleware(private_gate_mw)
    dp.callback_query.middleware(private_gate_mw)

    # Register routers for client features
    dp.include_router(start_router)
    dp.include_router(stars_billing_router)
    dp.include_router(cloner_menu_router)
    dp.include_router(settings_menu_router)
    dp.include_router(history_clone_router)
    dp.include_router(help_guide_router)
    dp.include_router(story_menu_router)
    dp.include_router(inline_search_router)
    dp.include_router(comment_moderator_router)

    # Embedded admin tools come last so the public handlers win on shared entry points (admin callbacks
    # are unique "admin_*"/"adm_*" and still reach it). The two shared ones defer to it explicitly:
    # "menu_admin" and /catchup are answered by the public routers only for non-admins.
    from admin_bot.bot_instance import create_admin_router
    dp.include_router(create_admin_router(name="main_admin_router", is_dedicated=False))

    return dp
