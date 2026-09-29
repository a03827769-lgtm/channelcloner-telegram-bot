import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from database.db_manager import DatabaseManager
from bot.filters.admin_filter import IsAdminFilter
from services.text_processor import TextProcessor
from services.media_handler import MediaHandler

@pytest.mark.asyncio
async def test_pair_toggle_and_update_cache_invalidation(tmp_path):
    """Verify that pair toggle and update methods call _notify_pair_cache_invalidated"""
    db = DatabaseManager(db_path=str(tmp_path / "test_cache_inv.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=555, full_name="Tester", username="tester")
    pair_id = await db.add_channel_pair(
        user_id=555,
        source_channel="@src_test",
        source_title="Source",
        target_channel="@tgt_test",
        target_title="Target"
    )

    invocations = 0
    def mock_notify():
        nonlocal invocations
        invocations += 1

    db._notify_pair_cache_invalidated = mock_notify

    await db.toggle_clean_links(pair_id)
    assert invocations == 1

    await db.toggle_auto_translate(pair_id, "ru")
    assert invocations == 2

    await db.update_watermark_settings(pair_id, "text", "MyWatermark", "center")
    assert invocations == 3

    await db.toggle_protected_mode(pair_id)
    assert invocations == 4

    await db.toggle_remove_signature(pair_id)
    assert invocations == 5

    await db.update_affiliate_rules(pair_id, "example.com=>ref=123")
    assert invocations == 6

    await db.update_pair_signature(pair_id, "New Sig")
    assert invocations == 7

    await db.update_blacklist(pair_id, "spam,ads")
    assert invocations == 8

    await db.update_replace_words(pair_id, "foo=>bar")
    assert invocations == 9

    await db.update_pair_ids(pair_id, source_id=-10012345, target_id=-10067890)
    assert invocations == 10

    await db.toggle_premium_emojis(pair_id)
    assert invocations == 11

    await db.update_video_watermark_settings(pair_id, "text", "VidWM", "top_left")
    assert invocations == 12

    await db.update_drip_settings(pair_id, 15, "buffer")
    assert invocations == 13

    await db.update_ai_paraphrase_settings(pair_id, "short")
    assert invocations == 14

    await db.toggle_auto_cta_buttons(pair_id)
    assert invocations == 15

    await db.toggle_backup_enabled(pair_id)
    assert invocations == 16

    await db.close()

@pytest.mark.asyncio
async def test_activate_subscription_resets_trial_notified(tmp_path):
    """Verify that activate_subscription resets trial_notified to 0 for recurring renewals"""
    db = DatabaseManager(db_path=str(tmp_path / "test_trial_notify.db"))
    await db.init_db()
    await db.get_or_create_user(user_id=777, full_name="Renew User", username="renewer")

    # Mark user as notified (e.g. trial ended)
    await db.mark_trial_notified(777)
    sub = await db.get_user_subscription(777)
    assert sub.trial_notified == 1

    # User buys Pro subscription
    await db.activate_subscription(user_id=777, tier="pro", stars=100, charge_id="test_charge_1")
    sub2 = await db.get_user_subscription(777)
    assert sub2.trial_notified == 0
    assert sub2.tier == "pro"

    await db.close()

@pytest.mark.asyncio
async def test_is_admin_filter_dynamic_db_check():
    """IsAdminFilter recognises database-granted admins through the in-memory admin cache (filled at startup
    and on every grant/revoke), so filtering the updates of regular users costs no database query"""
    flt = IsAdminFilter()
    mock_event = MagicMock()
    mock_event.from_user.id = 99999

    with patch("bot.filters.admin_filter.db_manager.is_admin_sync", return_value=True) as sync_check, \
         patch("bot.filters.admin_filter.db_manager.is_admin", new_callable=AsyncMock) as db_check:
        assert await flt(mock_event) is True
        sync_check.assert_called_once_with(99999)
        db_check.assert_not_called()

    with patch("bot.filters.admin_filter.db_manager.is_admin_sync", return_value=False):
        assert await flt(mock_event) is False

def test_clean_links_preserves_html_attribute_usernames():
    """Verify that usernames inside HTML attributes are not corrupted by regex"""
    html_text = "<a href=\"https://example.com/checkout?aff=@superaffiliate\">Kanalga kirish</a> va @public_channel_to_remove obuna bo'ling"
    cleaned = TextProcessor.clean_links_and_usernames(html_text)
    # The anchor (and the @ inside its href) is kept intact; only the bare @username in the text is removed
    assert '<a href="https://example.com/checkout?aff=@superaffiliate">Kanalga kirish</a>' in cleaned
    assert "@public_channel_to_remove" not in cleaned
    assert cleaned.endswith("obuna bo'ling")

@pytest.mark.asyncio
async def test_media_handler_flush_pending_albums():
    """Verify flush_pending_albums executes without error and clears buffers"""
    mh = MediaHandler()
    mock_complete = AsyncMock()
    msg = MagicMock()
    msg.id = 123
    mh.album_buffer.add_message("group_1", msg, mock_complete)
    assert "group_1" in mh.album_buffer._buffers

    await mh.flush_pending_albums(on_complete=mock_complete)
    assert "group_1" not in mh.album_buffer._buffers
    mock_complete.assert_called_once()
