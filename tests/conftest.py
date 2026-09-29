"""
Global pytest configuration.

Isolates the test suite from production state. This module is imported by
pytest before any test module, so the environment overrides below take effect
before `config.settings` (and the `db_manager` singleton) are first imported:

* DB_PATH points to a throw-away SQLite file in a temporary directory, so tests
  can never read or mutate the live `database/cloner.db`. The schema is created
  once per session so tests that use the shared `db_manager` find every table.
* CLONER_DISABLE_FILE_LOG prevents `run.configure_logging()` from attaching the
  rotating file handler to the production `data/app.log`.
* Tests that need live infrastructure (Docker stack, MySQL, real Telegram
  network) are skipped unless RUN_LIVE_TESTS=1 is set explicitly.
* aiosqlite runs every connection in a non-daemon thread; connections left open
  by tests would keep the interpreter alive forever after the run, so they are
  stopped when the session finishes.
* Outside live runs no test can reach the network: Bot API calls, Telethon
  connections and aiohttp requests to non-loopback hosts fail immediately with
  the libraries' own network errors.
* Every test starts from the same global state: `settings` fields changed by a
  test are restored afterwards, the in-memory caches are emptied, and a test that leaves a module-level
  `db_manager` replaced (a patch that was never stopped) is reported as an error
  instead of silently breaking the tests that run after it.
"""
import asyncio
import ipaddress
import os
import shutil
import sys
import tempfile
import threading
import types

import pytest

_TEST_TMP_DIR = tempfile.mkdtemp(prefix="channelcloner_tests_")
os.environ["DB_PATH"] = os.path.join(_TEST_TMP_DIR, "test_cloner.db")
os.environ["VAULT_KEY_PATH"] = os.path.join(_TEST_TMP_DIR, ".vault_key")
os.environ["CLONER_DISABLE_FILE_LOG"] = "1"

if os.getenv("RUN_LIVE_TESTS") != "1":
    # Unit tests never read the production .env (real tokens, admin IDs, URLs): every value they
    # need is given here, everything else keeps its default.
    os.environ["CLONER_ENV_FILE"] = ""
    os.environ["BOT_TOKEN"] = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS"
    os.environ["ADMIN_BOT_TOKEN"] = "987654321:TEST_ADMIN_BOT_TOKEN_FOR_UNIT_TESTS"
    os.environ["TELEGRAM_API_ID"] = "123456"
    os.environ["TELEGRAM_API_HASH"] = "test_telegram_api_hash_for_unit_tests"
    os.environ["TELETHON_SESSION"] = ""
    os.environ["ENCRYPTION_KEY"] = "unit-test-master-encryption-key-not-for-production"
    os.environ["PROXY_URL"] = ""


def _install_network_guard():
    """Makes every outbound network call of the unit tests fail fast (loopback servers stay reachable)."""
    import aiohttp
    import yarl
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.exceptions import TelegramNetworkError
    from telethon.client.telegrambaseclient import TelegramBaseClient

    async def _blocked_make_request(self, bot, method, timeout=None):
        raise TelegramNetworkError(method=method, message="network access is disabled in unit tests")

    async def _blocked_stream_content(self, url, *args, **kwargs):
        raise aiohttp.ClientConnectionError(f"network access is disabled in unit tests ({url})")
        yield b""  # pragma: no cover - makes this an async generator like the original

    async def _blocked_connect(self):
        raise ConnectionError("Telegram MTProto connections are disabled in unit tests")

    original_request = aiohttp.ClientSession._request

    def _is_loopback(host):
        if not host:
            return False
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            return False

    async def _guarded_request(self, method, str_or_url, **kwargs):
        url = yarl.URL(str(str_or_url))
        base_url = getattr(self, "_base_url", None)
        host = url.host or (base_url.host if base_url is not None else None)
        if not _is_loopback(host):
            raise aiohttp.ClientConnectionError(f"network access is disabled in unit tests ({url})")
        return await original_request(self, method, str_or_url, **kwargs)

    AiohttpSession.make_request = _blocked_make_request
    AiohttpSession.stream_content = _blocked_stream_content
    TelegramBaseClient.connect = _blocked_connect
    aiohttp.ClientSession._request = _guarded_request


if os.getenv("RUN_LIVE_TESTS") != "1":
    _install_network_guard()

# Modules that exercise real external infrastructure instead of the code under test.
LIVE_TEST_MODULES = {
    # Probe the public Mini App edge / the real Telegram menu button
    "test_miniapp_and_tunnel_sync.py",
}


_CANONICAL = {}


def pytest_sessionstart(session):
    from database.db_manager import db_manager

    # The one DatabaseManager every module must reference once a test is over
    _CANONICAL["db_manager"] = db_manager

    async def _create_schema():
        await db_manager.init_db()
        await db_manager.close()

    asyncio.run(_create_schema())


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live infrastructure test (set RUN_LIVE_TESTS=1 to run)")
    for item in items:
        if os.path.basename(str(item.fspath)) in LIVE_TEST_MODULES:
            item.add_marker(skip_live)


_PROJECT_PACKAGES = ("admin_bot", "bot", "config", "database", "services", "run")


def _restore_db_manager_references():
    """Points every loaded project module back to the session's DatabaseManager; returns the modules
    that referenced another object. That happens when a patch was never stopped, and also when a module
    was imported for the first time while `database.db_manager.db_manager` was patched: its
    `from database.db_manager import db_manager` then bound the test's object for good."""
    canonical = _CANONICAL.get("db_manager")
    if canonical is None:
        return []
    leaked = []
    for name, module in list(sys.modules.items()):
        if module is None or name.split(".", 1)[0] not in _PROJECT_PACKAGES:
            continue
        namespace = getattr(module, "__dict__", {})
        obj = namespace.get("db_manager")
        if obj is None or obj is canonical or isinstance(obj, types.ModuleType):
            continue
        namespace["db_manager"] = canonical
        leaked.append(name)
    return leaked


def _reset_process_caches():
    """Empties the in-memory caches shared by every test (settings such as private mode, subscriptions,
    seen users, dedup keys): a value cached by one test must never leak into the next one."""
    try:
        from services.cache_manager import cache_manager
    except Exception:
        return
    for cache in (cache_manager.sub_cache, cache_manager.settings_cache,
                  cache_manager.seen_users_cache, cache_manager.dedup_cache):
        data = getattr(cache, "_data", None)
        if data is not None:
            data.clear()


def _detach_routers_from_dispatchers():
    """The handler modules keep their routers as module-level singletons, and a router can belong to one
    dispatcher only: routers a test attached to a Dispatcher are released again, so the next
    create_dispatcher() works. Router-in-router links made at import time are kept."""
    try:
        from aiogram import Dispatcher, Router
    except Exception:
        return
    for name, module in list(sys.modules.items()):
        if module is None or name.split(".", 1)[0] not in ("bot", "admin_bot"):
            continue
        for value in list(getattr(module, "__dict__", {}).values()):
            if not isinstance(value, Router) or isinstance(value, Dispatcher):
                continue
            parent = value.parent_router
            if isinstance(parent, Dispatcher):
                if value in parent.sub_routers:
                    parent.sub_routers.remove(value)
                value._parent_router = None


@pytest.fixture(autouse=True)
def _isolate_global_state():
    from config.settings import settings

    settings_before = dict(settings.__dict__)
    yield
    if settings.__dict__ != settings_before:
        settings.__dict__.clear()
        settings.__dict__.update(settings_before)
    _reset_process_caches()
    _detach_routers_from_dispatchers()

    leaked = _restore_db_manager_references()
    if leaked:
        pytest.fail(
            "test left module-level db_manager replaced (a patch was never stopped) in: " + ", ".join(sorted(leaked)),
            pytrace=False,
        )


def _stop_leftover_sqlite_threads():
    try:
        from aiosqlite import core as aiosqlite_core
    except Exception:
        return
    sentinel = getattr(aiosqlite_core, "_STOP_RUNNING_SENTINEL", None)
    if sentinel is None:
        return
    for thread in threading.enumerate():
        if isinstance(thread, aiosqlite_core.Connection) and thread.is_alive():
            try:
                thread._tx.put_nowait(sentinel)
            except Exception:
                pass


def pytest_sessionfinish(session, exitstatus):
    try:
        from database.db_manager import db_manager
        asyncio.run(db_manager.close())
    except Exception:
        pass
    _stop_leftover_sqlite_threads()
    shutil.rmtree(_TEST_TMP_DIR, ignore_errors=True)
