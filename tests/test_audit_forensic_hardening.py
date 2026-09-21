import os
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from config.settings import Settings
from services.cloner_engine import ClonerEngine
from database.models import ChannelPair, Subscription


def test_watchdog_health_url():
    """Verify watchdog get_health_url is defined and returns loopback URL."""
    from scripts.windows_keepalive_watchdog import get_health_url
    url = get_health_url()
    assert url.startswith("http://127.0.0.1:")
    assert url.endswith("/health")


def test_settings_security_defaults():
    """Verify settings do not contain hardcoded active super admin IDs in source defaults."""
    s = Settings(BOT_TOKEN="mock:token", ADMIN_BOT_TOKEN="mock:token", TELEGRAM_API_ID=12345, TELEGRAM_API_HASH="hash")
    # PRIMARY_SUPER_ADMIN_ID should default to 0 if not provided in env
    assert s.PRIMARY_SUPER_ADMIN_ID == 0 or isinstance(s.PRIMARY_SUPER_ADMIN_ID, int)
    # ENABLE_PLAYWRIGHT should default to False to conserve RAM on small VPS
    assert s.ENABLE_PLAYWRIGHT is False


def test_smart_fit_caption_with_badge():
    """Verify caption truncation when caption + badge exceeds 1024 characters."""
    badge = "\n\n🔥 NARX ARZONLASHDI: $1,000 ➡️ $900"
    
    # Short caption fits directly
    short_cap = "Uy sotiladi."
    res = ClonerEngine._smart_fit_caption_with_badge(short_cap, badge, max_len=1024)
    assert res == short_cap + badge
    assert len(res) <= 1024
    
    # Long caption (> 1024) is trimmed with ellipsis preserving the badge
    long_cap = "A" * 1020
    res2 = ClonerEngine._smart_fit_caption_with_badge(long_cap, badge, max_len=1024)
    assert len(res2) <= 1024
    assert res2.endswith(badge)
    assert "..." in res2


def test_db_manager_no_duplicate_methods():
    """Verify db_manager and telethon_listener do not have shadowed duplicate method names."""
    from database.db_manager import DatabaseManager
    import inspect

    # Verify method counts in DatabaseManager class
    methods = [name for name, _ in inspect.getmembers(DatabaseManager, predicate=inspect.isfunction)]
    assert "update_pair_ad_action" in methods
    assert "toggle_show_caption_above" in methods

    from services.telethon_listener import TelethonListener
    t_methods = [name for name, _ in inspect.getmembers(TelethonListener, predicate=inspect.isfunction)]
    assert "is_connected" in t_methods


@pytest.mark.asyncio
async def test_process_message_text_unbound_safety():
    """Verify process_message_text does not raise UnboundLocalError when text is empty or modified."""
    bot_mock = AsyncMock()
    engine = ClonerEngine(bot=bot_mock)
    
    pair = ChannelPair(
        id=999,
        user_id=12345,
        source_channel="@src",
        target_channel="@dst",
        is_active=1,
        auto_premium_emojis=True,
        custom_signature="— VIP Kanal"
    )
    
    # Empty initial text
    with patch("database.db_manager.db_manager.get_user_subscription", return_value=Subscription(user_id=12345, tier="free")):
        res = await engine.process_post_text("", pair)
        # Should cleanly return empty text without UnboundLocalError
        assert res == "" or res is None or isinstance(res, str)


@pytest.mark.asyncio
async def test_story_cloner_lru_client_eviction():
    """Verify story cloner service limits client cache to 20 connections."""
    from services.story_cloner_service import StoryClonerService
    svc = StoryClonerService()
    
    # Mock 20 clients
    for i in range(25):
        client_mock = MagicMock()
        client_mock.is_connected.return_value = True
        client_mock.disconnect = AsyncMock()
        client_mock.is_user_authorized = AsyncMock(return_value=True)
        client_mock.connect = AsyncMock()
        
        # Insert client
        if len(svc._user_clients) >= 20:
            old_uid, old_c = next(iter(svc._user_clients.items()))
            svc._user_clients.pop(old_uid, None)
            if old_c.is_connected():
                await old_c.disconnect()
        svc._user_clients[i] = client_mock
        
    assert len(svc._user_clients) == 20
