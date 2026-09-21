import pytest
import pytest_asyncio
import asyncio
from database.db_manager import DatabaseManager
from services.telethon_listener import TelethonListener
from services.rate_limiter import SmartDelayEngine
from bot.handlers.settings_menu import is_admin_user
from config.settings import settings

@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_file = str(tmp_path / "test_mtproto_sync.db")
    database = DatabaseManager(db_path=db_file)
    await database.init_db()
    yield database
    try:
        await database.close()
    except Exception:
        pass

@pytest.mark.asyncio
async def test_database_granted_admin_can_add_channel(test_db):
    test_user_id = 9988776655
    await test_db.get_or_create_user(test_user_id, "Test DB Admin", "testdbadmin", is_admin=True)
    assert await test_db.is_admin(test_user_id) is True
    
    can_add, max_allowed, current_count = await test_db.can_user_add_channel(test_user_id)
    assert can_add is True
    assert max_allowed == 999

@pytest.mark.asyncio
async def test_rate_limiter_prune_stale_locks():
    engine = SmartDelayEngine(min_delay=0.01, max_delay=0.02)
    ch_name = "test_channel_stale_check"
    await engine.wait_for_slot(ch_name)
    norm = engine._normalize_channel_key(ch_name)
    assert norm in engine._locks
    assert norm in engine._last_post_time
    
    # Prune with -1.0 idle seconds
    await engine.prune_stale_locks(max_idle_seconds=-1.0)
    assert norm not in engine._locks
    assert norm not in engine._last_post_time

@pytest.mark.asyncio
async def test_telethon_listener_methods():
    listener = TelethonListener()
    assert hasattr(listener, "is_connected")
    assert hasattr(listener, "logout")
    assert hasattr(listener, "get_me")
    assert hasattr(listener, "start_listener")
    assert hasattr(listener, "stop")
    
    # When no client is active
    assert listener.is_connected() is False
    assert await listener.get_me() is None
    
    # Call logout when not running — should execute cleanly
    await listener.logout()
    assert listener.is_connected() is False
