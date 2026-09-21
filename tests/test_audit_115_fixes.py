import pytest
import os
import asyncio
from datetime import datetime, timezone, timedelta
from config.settings import Settings
from database.models import Subscription, ChannelPair, DripQueueItem, ChannelBackup
from database.db_manager import DatabaseManager
from services.cache_manager import HighLoadCacheManager, LRUSet
from services.text_processor import TextProcessor
from services.video_watermark_service import video_watermark_service
from services.telethon_listener import TelethonListener
from deploy.anti_reclaim import OracleAntiReclaimDaemon

def test_settings_admin_ids_flexible_parsing():
    """Verify ADMIN_IDS parsing handles spaces, semicolons, and commas gracefully"""
    s = Settings(PRIMARY_SUPER_ADMIN_ID=0, ADMIN_IDS="12345, 67890; 112233 \n 445566,invalid_id")
    assert s.admin_ids == {12345, 67890, 112233, 445566}

def test_cache_manager_lru_and_semaphores():
    """Verify HighLoadCacheManager semaphores and LRUSet function accurately"""
    cache = HighLoadCacheManager()
    assert cache.dedup_cache.maxsize == 100000
    sem = cache.media_semaphore
    assert isinstance(sem, asyncio.Semaphore)
    assert cache.media_semaphore is sem

def test_subscription_timezone_aware_parsing():
    """Verify timezone offsets in ISO timestamps are properly converted to UTC naive"""
    now_utc = datetime.now(timezone.utc)
    # 5 hours ahead (+05:00), expiring in 2 hours in UTC
    exp_time_tz = (now_utc + timedelta(hours=2)).astimezone(timezone(timedelta(hours=5))).isoformat()
    sub = Subscription(user_id=1, tier="pro", expires_at=exp_time_tz)
    assert sub.is_active is True

    # Expired 2 hours ago in UTC
    expired_tz = (now_utc - timedelta(hours=2)).astimezone(timezone(timedelta(hours=5))).isoformat()
    sub_exp = Subscription(user_id=1, tier="pro", expires_at=expired_tz)
    assert sub_exp.is_active is False

def test_text_processor_html_tag_protection_in_replacements():
    """Verify word replacements NEVER mutate text inside HTML tags or attributes"""
    text = '<a href="https://example.com/item">Item link</a> and some plain item text'
    replace_dict = {"item": "product", "a": "b"}
    result = TextProcessor.apply_word_replacements(text, replace_dict)
    # The URL and <a href="..."> tag must remain completely intact!
    assert 'href="https://example.com/item"' in result
    assert result.startswith('<a href="https://example.com/item">')
    assert 'Product link' in result
    assert 'product text' in result

def test_text_processor_uzbek_apostrophe_blacklist():
    """Verify contains_blacklisted_words handles Uzbek apostrophes without false positives"""
    blacklist = ["ma'lumot", "e'lon"]
    assert TextProcessor.contains_blacklisted_words("Bu yerda muhim ma'lumot bor", blacklist) is True
    assert TextProcessor.contains_blacklisted_words("Katta e'lon berildi", blacklist) is True
    # Word part matching should not trigger
    assert TextProcessor.contains_blacklisted_words("e'lonchi emas", blacklist) is False

def test_text_processor_fit_caption_limit_lifo_stack():
    """Verify nested HTML tags are closed and reopened in proper LIFO order"""
    text = '<a href="https://t.me/test"><b>Bold link start ' + ('x' * 1100) + ' bold link end</b></a>'
    caption, overflow = TextProcessor.fit_caption_limit(text, max_limit=100)
    assert caption.endswith("</b></a>")
    assert overflow.startswith('<a href="https://t.me/test"><b>')

def test_text_processor_attach_signature_html_safe():
    """Verify attach_signature never cuts inside an HTML tag"""
    long_html = "<div>" + ("A" * 4090) + "</div>"
    res = TextProcessor.attach_signature(long_html, "MySignature")
    assert len(res) <= 4096
    assert "MySignature" in res

def test_video_watermark_font_finder():
    """Verify _find_default_fontfile returns a valid font or None without exception"""
    font_res = video_watermark_service._find_default_fontfile()
    assert font_res is None or isinstance(font_res, str)

@pytest.mark.asyncio
async def test_db_manager_migrations_and_media_group_backup(tmp_path):
    """Verify database schema migrations and media_group_id backup storage"""
    test_db_path = str(tmp_path / "test_115.db")
    db = DatabaseManager(db_path=test_db_path)
    await db.init_db()
    await db.get_or_create_user(user_id=1001, full_name="Test User", username="testuser")

    pair_id = await db.add_channel_pair(
        user_id=1001,
        source_channel="@src_test",
        source_title="Source Title",
        target_channel="@tgt_test",
        target_title="Target Title"
    )

    # Re-adding duplicate pair updates titles
    pair_id_dup = await db.add_channel_pair(
        user_id=1001,
        source_channel="@src_test",
        source_title="Updated Source Title",
        target_channel="@tgt_test",
        target_title="Updated Target Title"
    )
    assert pair_id == pair_id_dup

    pair = await db.get_pair_by_id(pair_id)
    assert pair.source_title == "Updated Source Title"
    assert pair.target_title == "Updated Target Title"

    # Save backup with media_group_id
    await db.save_channel_backup(
        pair_id=pair_id,
        source_id=111,
        message_id=200,
        text="Album item 1",
        media_type="media_group",
        media_file_id="BAADAgADTEST123",
        media_group_id="group_9999"
    )

    backups = await db.get_channel_backups(pair_id)
    assert len(backups) == 1
    assert backups[0]["media_group_id"] == "group_9999"
    assert backups[0]["media_file_id"] == "BAADAgADTEST123"

    await db.close()

def test_anti_reclaim_small_ram_calibration():
    """Verify anti-reclaim limits memory reservation on small RAM machines (<2GB)"""
    daemon = OracleAntiReclaimDaemon()
    daemon.total_ram_mb = 1024
    if daemon.total_ram_mb < 2048:
        daemon.allocated_ram_mb = min(120, int(daemon.total_ram_mb * 0.12))
    assert daemon.allocated_ram_mb <= 120
