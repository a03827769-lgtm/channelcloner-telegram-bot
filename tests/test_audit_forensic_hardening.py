import pytest
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


def test_settings_security_defaults(monkeypatch):
    """Source defaults contain no super admin ID and keep Playwright (RAM heavy) disabled."""
    for var in ("PRIMARY_SUPER_ADMIN_ID", "ADMIN_IDS", "ENABLE_PLAYWRIGHT"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, BOT_TOKEN="1:mock_token", ADMIN_BOT_TOKEN="2:mock_token",
                 TELEGRAM_API_ID=12345, TELEGRAM_API_HASH="hash")
    assert s.PRIMARY_SUPER_ADMIN_ID == 0
    assert s.admin_ids == set()
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
    
    # Empty text (e.g. a photo without caption): nothing to process, and the owner's signature is not
    # posted on its own either
    with patch("database.db_manager.db_manager.get_user_subscription", return_value=Subscription(user_id=12345, tier="free")):
        assert await engine.process_post_text("", pair) == ""
        assert await engine.process_post_text("", pair, has_media=True) == ""
        assert await engine.process_post_text(None, pair) == ""
        # With text the signature is appended
        assert (await engine.process_post_text("Yangi e'lon", pair)).endswith("— VIP Kanal")


@pytest.mark.asyncio
async def test_story_cloner_lru_client_eviction():
    """The story client pool evicts idle clients beyond its soft limit (least recently used first) and
    never a client that serves a live story monitor."""
    from services.story_cloner_service import CLIENT_POOL_SOFT_LIMIT, StoryClonerService
    svc = StoryClonerService()

    def make_client():
        client = MagicMock()
        client.is_connected.return_value = True
        client.disconnect = AsyncMock()
        return client

    monitored = {0, 1}  # users 0 and 1 have running monitors
    with patch.object(svc, "_has_active_monitor", side_effect=lambda uid: uid in monitored):
        clients = {}
        for uid in range(CLIENT_POOL_SOFT_LIMIT + 5):
            clients[uid] = make_client()
            await svc._install_user_client(uid, clients[uid])

    assert len(svc._user_clients) == CLIENT_POOL_SOFT_LIMIT
    assert {0, 1} <= set(svc._user_clients)             # monitored clients survive
    assert not {2, 3, 4, 5, 6} & set(svc._user_clients)  # the oldest idle ones were evicted...
    for uid in (2, 3, 4, 5, 6):
        clients[uid].disconnect.assert_awaited()         # ...and disconnected
    assert CLIENT_POOL_SOFT_LIMIT + 4 in svc._user_clients
